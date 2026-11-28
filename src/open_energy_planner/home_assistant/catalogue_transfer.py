"""Assemble one bounded discovery transfer without exposing partial source membership."""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta

CATALOGUE_TRANSFER_BYTES = 32 * 1024 * 1024
CATALOGUE_TRANSFER_TIMEOUT_SECONDS = 300
CATALOGUE_CLOCK_SKEW_SECONDS = 30
CATALOGUE_REQUEST_BYTES = 1024 * 1024
TRANSFER_IDENTITY_FIELDS = ("id", "revision", "captured_at")
TRANSFER_FIELDS = {*TRANSFER_IDENTITY_FIELDS, "index", "complete"}
COLLECTION_PATHS = {
    ("entities",),
    ("areas",),
    ("history",),
    *(
        ("suggestions", field)
        for field in (
            "terms",
            "soc",
            "solar_sources",
            "batteries",
            "consumers",
            "grid_power_entities",
            "solar_configurations",
        )
    ),
    ("suggestions", "settlement", "imports"),
    ("suggestions", "settlement", "exports"),
}


def _transfer_capture_time(transfer, now):
    """Validate the page identity and return its bounded, aware capture time."""
    if not isinstance(transfer, dict) or set(transfer) != TRANSFER_FIELDS:
        raise ValueError("Invalid transfer identity")
    if not all(
        isinstance(transfer[field], str) and re.fullmatch(pattern, transfer[field])
        for field, pattern in (("id", r"[A-Za-z0-9_-]{1,64}"), ("revision", r"[0-9a-f]{64}"))
    ):
        raise ValueError("Invalid transfer fields")
    if (
        not isinstance(transfer["captured_at"], str)
        or type(transfer["index"]) is not int
        or transfer["index"] < 0
        or type(transfer["complete"]) is not bool
    ):
        raise ValueError("Invalid transfer fields")
    captured = datetime.fromisoformat(transfer["captured_at"])
    if captured.tzinfo is None or not (
        -timedelta(seconds=CATALOGUE_CLOCK_SKEW_SECONDS)
        <= now - captured
        <= timedelta(seconds=CATALOGUE_TRANSFER_TIMEOUT_SECONDS)
    ):
        raise ValueError("Expired discovery snapshot")
    return captured


def _verified_snapshot(body, completed, revision):
    """Recognize a completion page only when the assembled payload matches its digest."""
    if set(body) != {"transfer"}:
        raise ValueError("Invalid discovery completion")
    digest = hashlib.sha256(
        json.dumps(completed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    if digest != revision:
        raise ValueError("Discovery content mismatch")
    return completed


def _append_fragment(body, snapshot):
    """Append one validated collection fragment to the pending snapshot."""
    if set(body) != {"transfer", "path", "items"} or not isinstance(body["path"], list):
        raise ValueError("Invalid discovery fragment")
    path, items = body["path"], body["items"]
    if not all(isinstance(field, str) for field in path) or tuple(path) not in COLLECTION_PATHS:
        raise ValueError("Invalid discovery collection")
    if not isinstance(items, list) or not items:
        raise ValueError("Empty discovery fragment")
    target = snapshot
    for field in path:
        target = target[field]
    if not isinstance(target, list):
        raise TypeError("Invalid discovery collection type")
    target.extend(deepcopy(items))


class CatalogueTransfer:
    """Keep at most one expiring pending snapshot and reject delayed or mixed fragments."""

    def __init__(self):
        """Start without pending memory or a previously observed capture timestamp."""
        self.pending = None
        self.latest_capture = None

    def receive(self, body, now):
        """Assemble a snapshot without publishing partial catalogue membership.

        The caller serializes access with the application configuration lock.
        Ordered pages mutate pending assembly state. A new start must have a
        capture time newer than latest_capture. An unpaged snapshot clears the
        pending transfer and sets latest_capture to the supplied receipt time.
        Completion clears pending state after verifying the content digest.
        The catalogue owner still validates domain content before publication.

        Args:
            body: Decoded discovery snapshot or transfer message.
            now: Timezone-aware receipt time used for age and clock-skew checks.

        Returns:
            dict | None: A complete unpaged or assembled snapshot, or None while
                the ordered transfer remains incomplete.

        Raises:
            ValueError: The message violates transfer shape, byte budgets,
                capture age/order, sequence or digest requirements. Invalid
                input discards pending state without rolling back latest_capture.
        """
        try:
            return self._receive(body, now)
        except (ValueError, TypeError, KeyError, OverflowError):
            self.pending = None
            raise ValueError("Invalid catalogue transfer") from None

    def _receive(self, body, now):
        """Admit the request envelope and route the page by its sequence position."""
        if not isinstance(body, dict):
            raise TypeError("Expected discovery object")
        size = len(json.dumps(body, allow_nan=False).encode())
        if size >= CATALOGUE_REQUEST_BYTES:
            raise ValueError("Discovery request exceeds byte budget")
        if "transfer" not in body:
            self.pending = None
            self.latest_capture = now
            return body
        transfer = body["transfer"]
        captured = _transfer_capture_time(transfer, now)
        identity = {field: transfer[field] for field in TRANSFER_IDENTITY_FIELDS}
        if transfer["index"] == 0:
            self._start(body, identity, captured, size)
            return None
        return self._advance(body, transfer, identity, size)

    def _start(self, body, identity, captured, size):
        """Begin assembly only after accepting the header and its capture order."""
        if set(body) != {"transfer", "header"} or not isinstance(body["header"], dict):
            raise ValueError("Invalid discovery start")
        if body["transfer"]["complete"] or (
            self.latest_capture is not None and captured <= self.latest_capture
        ):
            raise ValueError("Invalid discovery start")
        self.latest_capture = captured
        self.pending = {
            "identity": identity,
            "next": 1,
            "body": deepcopy(body["header"]),
            "bytes": size,
        }

    def _advance(self, body, transfer, identity, size):
        """Apply the expected page within the budget or return a verified snapshot."""
        pending = self.pending
        if (
            pending is None
            or identity != pending["identity"]
            or transfer["index"] != pending["next"]
        ):
            raise ValueError("Unordered discovery fragment")
        pending["bytes"] += size
        if pending["bytes"] > CATALOGUE_TRANSFER_BYTES:
            raise ValueError("Discovery transfer exceeds memory budget")
        if transfer["complete"]:
            completed = _verified_snapshot(body, pending["body"], transfer["revision"])
            self.pending = None
            return completed
        _append_fragment(body, pending["body"])
        pending["next"] += 1
        return None
