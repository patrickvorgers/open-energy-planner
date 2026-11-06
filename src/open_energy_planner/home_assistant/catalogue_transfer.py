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
COLLECTION_PATHS = {
    ("entities",),
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


class CatalogueTransfer:
    """Keep at most one expiring pending snapshot and reject delayed or mixed fragments."""

    def __init__(self):
        """Start without pending memory or a previously observed capture timestamp."""
        self.pending = None
        self.latest_capture = None

    def receive(self, body, now):
        """Return only complete snapshots, leaving final domain validation to the catalogue owner."""
        try:
            return self._receive(body, now)
        except (ValueError, TypeError, KeyError, OverflowError):
            self.pending = None
            raise ValueError("Invalid catalogue transfer") from None

    def _receive(self, body, now):
        """Check byte budgets, capture order, whitelisted paths, sequence and the final content digest."""
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
        if not isinstance(transfer, dict) or set(transfer) != {
            "id",
            "revision",
            "captured_at",
            "index",
            "complete",
        }:
            raise ValueError("Invalid transfer identity")
        if (
            not isinstance(transfer["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", transfer["id"])
            or not isinstance(transfer["revision"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", transfer["revision"])
            or not isinstance(transfer["captured_at"], str)
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
        identity = {field: transfer[field] for field in ("id", "revision", "captured_at")}
        if transfer["index"] == 0:
            if (
                set(body) != {"transfer", "header"}
                or transfer["complete"]
                or not isinstance(body["header"], dict)
                or (self.latest_capture is not None and captured <= self.latest_capture)
            ):
                raise ValueError("Invalid discovery start")
            self.latest_capture = captured
            self.pending = {
                "identity": identity,
                "next": 1,
                "body": deepcopy(body["header"]),
                "bytes": size,
            }
            return None
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
            if set(body) != {"transfer"}:
                raise ValueError("Invalid discovery completion")
            completed = pending["body"]
            digest = hashlib.sha256(
                json.dumps(
                    completed, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest()
            if digest != transfer["revision"]:
                raise ValueError("Discovery content mismatch")
            self.pending = None
            return completed
        if set(body) != {"transfer", "path", "items"} or not isinstance(body["path"], list):
            raise ValueError("Invalid discovery fragment")
        if (
            not all(isinstance(field, str) for field in body["path"])
            or tuple(body["path"]) not in COLLECTION_PATHS
        ):
            raise ValueError("Invalid discovery collection")
        if not isinstance(body["items"], list) or not body["items"]:
            raise ValueError("Empty discovery fragment")
        target = pending["body"]
        for field in body["path"]:
            target = target[field]
        if not isinstance(target, list):
            raise TypeError("Invalid discovery collection type")
        target.extend(deepcopy(body["items"]))
        pending["next"] += 1
        return None
