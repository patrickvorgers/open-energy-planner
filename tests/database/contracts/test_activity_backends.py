"""Activity retention and cross-backend transfer preserve execution evidence on every backend."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from open_energy_planner.storage.adapters.activities import ActivityStore
from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from tests.database.support.backends import BACKENDS

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


@pytest.mark.parametrize("backend", BACKENDS, ids=[name for name, _ in BACKENDS])
def test_activity_restart_paging_retention_and_backend_transfer(tmp_path, backend):
    """Transfer full payloads and indexes, then operate without the old database."""
    name, url = backend
    engine = archive_engine(tmp_path / "source") if name == "sqlite" else create_engine(url)
    SCHEMA.drop_all(engine)
    repository = SqlPlanRepository(tmp_path / "source", engine=engine)
    target = archive_engine(tmp_path / "target")
    try:
        store = ActivityStore(engine)
        for index, state in enumerate(("completed", "running", "failed")):
            row = {
                "id": f"{index:032x}",
                "started_at": NOW.isoformat(),
                "finished_at": None,
                "owner": "prices:nord_pool",
                "kind": "prices",
                "state": state,
                "title": "Fetch published prices",
                "detail": "Market: NL. No private provider payload.",
                "recovered": False,
                "usable": state == "completed",
                "context": {
                    "label": "Nord Pool",
                    "start": "2026-10-25T00:00:00+00:00",
                    "end": "2026-10-26T00:00:00+00:00",
                },
            }
            store.write(row, NOW)
        assert store.read(NOW - timedelta(days=1), NOW + timedelta(days=1), kind="weather") == []
        original = store.read(NOW - timedelta(days=1), NOW + timedelta(days=1))
        counts = migrate_archive(engine, target, overwrite_target=True)
        assert counts["oep_activities"] == 3
        transferred = ActivityStore(target)
        assert transferred.read(NOW - timedelta(days=1), NOW + timedelta(days=1)) == original
        assert transferred.restart(NOW + timedelta(minutes=1))[0] == {
            ("prices:nord_pool", "prices")
        }
        rows = transferred.read(NOW - timedelta(days=1), NOW + timedelta(days=1), limit=1)
        remaining = transferred.read(
            NOW - timedelta(days=1), NOW + timedelta(days=1), (rows[0]["started_at"], rows[0]["id"])
        )
        assert len({row["id"] for row in rows + remaining}) == 3
        assert all(row["context"] == original[0]["context"] for row in rows + remaining)
        assert sorted(row["state"] for row in rows + remaining) == [
            "completed",
            "failed",
            "interrupted",
        ]
        transferred.restart(NOW + timedelta(days=8))
        assert transferred.read(NOW - timedelta(days=1), NOW + timedelta(days=9)) == []
        assert store.read(NOW - timedelta(days=1), NOW + timedelta(days=1)) == original
    finally:
        repository.close()
        SCHEMA.drop_all(engine)
        engine.dispose()
        target.dispose()
