"""Serialize one complete discovery snapshot into ordered, bounded local HTTP messages."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

CATALOGUE_PAGE_BYTES = 192 * 1024


def catalogue_pages(body):
    """Prepare and yield a complete snapshot within the default JSON byte budget.

    A small snapshot is yielded unchanged without a transfer envelope. Larger
    snapshots yield a header, ordered collection pages and a final completion
    message. Every message includes its envelope in CATALOGUE_PAGE_BYTES, using
    the default JSON escaping and UTF-8 encoding. The caller sends messages in
    order and requires acceptance of the final reply before using membership.
    Iteration prepares all pages before yielding the first one. An optional solar
    configuration group that cannot fit its actual envelope becomes explicitly
    incomplete, without importing only some of its planes. The digest covers
    this normalized snapshot. Preparation never mutates the caller's snapshot.

    Yields:
        dict: The original small snapshot or the next bounded transfer message.

    Raises:
        ValueError: JSON contains nonfinite numbers, or a header or individual
            mandatory record or incomplete solar group exceeds the byte budget.
            Preparation fails before any page is yielded.
    """
    encoded = json.dumps(body, allow_nan=False).encode()
    if len(encoded) <= CATALOGUE_PAGE_BYTES:
        yield body
        return
    transfer = {
        "id": uuid4().hex,
        "revision": hashlib.sha256(b"").hexdigest(),
        "captured_at": datetime.now(UTC).isoformat(),
        "index": 0,
        "complete": False,
    }
    header = deepcopy(body)
    collections = []
    for path in (
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
    ):
        owner = header
        for field in path[:-1]:
            owner = owner.get(field, {})
        if path[-1] in owner:
            collections.append((path, owner[path[-1]]))
            owner[path[-1]] = []

    def message(**payload):
        """Account for JSON escaping and protocol metadata inside the page byte budget."""
        result = {"transfer": dict(transfer), **payload}
        if len(json.dumps(result, allow_nan=False).encode()) > CATALOGUE_PAGE_BYTES:
            raise ValueError("Catalogue record exceeds the discovery page budget")
        return result

    pages = [message(header=header)]
    transfer["index"] += 1
    for path, items in collections:
        batch = []
        size = len(json.dumps({"transfer": dict(transfer), "path": path, "items": []}).encode())
        for item_index, item in enumerate(items):
            item_bytes = len(json.dumps(item, allow_nan=False).encode())
            added_bytes = item_bytes + (len(", ") if batch else 0)
            if batch and size + added_bytes > CATALOGUE_PAGE_BYTES:
                pages.append(message(path=path, items=batch))
                transfer["index"] += 1
                batch = []
                size = len(
                    json.dumps({"transfer": dict(transfer), "path": path, "items": []}).encode()
                )
                added_bytes = item_bytes
            if size + added_bytes > CATALOGUE_PAGE_BYTES and path == (
                "suggestions",
                "solar_configurations",
            ):
                item = {**item, "providers": [], "issue": "solar_configuration_incomplete"}
                items[item_index] = item
                added_bytes = len(json.dumps(item, allow_nan=False).encode())
            if size + added_bytes > CATALOGUE_PAGE_BYTES:
                raise ValueError("Catalogue record exceeds the discovery page budget")
            batch.append(item)
            size += added_bytes
        if batch:
            pages.append(message(path=path, items=batch))
            transfer["index"] += 1
    transfer["complete"] = True
    pages.append(message())
    snapshot = deepcopy(header)
    for path, items in collections:
        owner = snapshot
        for field in path[:-1]:
            owner = owner[field]
        owner[path[-1]] = items
    revision = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    for page in pages:
        page["transfer"]["revision"] = revision
        yield page
