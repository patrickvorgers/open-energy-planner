"""Serialize one complete discovery snapshot into ordered, bounded local HTTP messages."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

CATALOGUE_PAGE_BYTES = 192 * 1024


def catalogue_pages(body):
    """Keep small discoveries compatible and transfer larger snapshots without dropping records."""
    encoded = json.dumps(body, allow_nan=False).encode()
    if len(encoded) <= CATALOGUE_PAGE_BYTES:
        yield body
        return
    transfer = {
        "id": uuid4().hex,
        "revision": hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
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

    yield message(header=header)
    transfer["index"] += 1
    for path, items in collections:
        batch = []
        size = len(json.dumps({"transfer": dict(transfer), "path": path, "items": []}).encode())
        for item in items:
            item_bytes = len(json.dumps(item, allow_nan=False).encode())
            added_bytes = item_bytes + (len(", ") if batch else 0)
            if batch and size + added_bytes > CATALOGUE_PAGE_BYTES:
                yield message(path=path, items=batch)
                transfer["index"] += 1
                batch = []
                size = len(
                    json.dumps({"transfer": dict(transfer), "path": path, "items": []}).encode()
                )
                added_bytes = item_bytes
            if size + added_bytes > CATALOGUE_PAGE_BYTES:
                raise ValueError("Catalogue record exceeds the discovery page budget")
            batch.append(item)
            size += added_bytes
        if batch:
            yield message(path=path, items=batch)
            transfer["index"] += 1
    transfer["complete"] = True
    yield message()
