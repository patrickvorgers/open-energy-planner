"""Synthetic archive ages and bounded reference-index setup shared by database contracts."""

from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.storage.adapters.lifecycle.document_retention import maintain_documents
from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENT_METADATA

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)


def archived(documents, kind, identity, value, *, age_days=31):
    """Write real indexed evidence, then assign its independently specified synthetic storage age."""
    name = f"{kind}-{identity}.json"
    documents.write(name, value)
    with documents.engine.begin() as connection:
        connection.execute(
            DOCUMENT_METADATA.update()
            .where(
                DOCUMENT_METADATA.c.namespace == documents.namespace,
                DOCUMENT_METADATA.c.name == name,
            )
            .values(created_at=(NOW - timedelta(days=age_days)).isoformat(timespec="microseconds"))
        )
    return name


def finish_index(documents, now=NOW):
    """Resume maintenance until both legacy owner classes have been indexed without expiring data."""
    for _attempt in range(64):
        assert maintain_documents(documents, now) == 0
        if documents.read("archive-retention.json")["stage"] == "complete":
            return
    pytest.fail("Synthetic reference-index backfill did not finish")
