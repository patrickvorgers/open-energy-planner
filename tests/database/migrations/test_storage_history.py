"""Capacity observations transfer with all app-owned tables and need no old backend afterward."""

from datetime import UTC, datetime

import pytest

from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from open_energy_planner.storage.adapters.usage import StorageUsage
from open_energy_planner.storage.adapters.usage_history import StorageHistory
from tests.database.support.backends import BACKENDS


@pytest.mark.parametrize("backend,url", BACKENDS, ids=[name for name, _ in BACKENDS])
def test_transfer_retains_daily_history_and_resumes_on_destination(
    backend, url, tmp_path, monkeypatch
):
    """Round-trip real SQLite and each real server backend, preserving original sampling provenance."""
    source = archive_engine(tmp_path / "source")
    target = archive_engine(tmp_path / "target", url)
    restored = archive_engine(tmp_path / "restored")
    SCHEMA.drop_all(target)
    source_repository = SqlPlanRepository(tmp_path / "source", engine=source)
    monkeypatch.setattr(
        "open_energy_planner.storage.adapters.usage_history.storage_usage",
        lambda engine: StorageUsage(
            engine.dialect.name,
            database_bytes=8589934592,
            database_estimated=engine.dialect.name in ("mysql", "mariadb"),
        ),
    )
    try:
        StorageHistory(source).sample(
            lambda: datetime(2026, 10, 24, 2, tzinfo=UTC), "Europe/Amsterdam"
        )
        original = StorageHistory(source).read(None, "2026-10-24")
        assert migrate_archive(source, target, overwrite_target=True)["oep_storage_samples"] == 1
        source_repository.close()
        SCHEMA.drop_all(source)
        target_repository = SqlPlanRepository(tmp_path / "target", engine=target)
        try:
            store = StorageHistory(target)
            assert store.read(None, "2026-10-24") == original
            store.sample(lambda: datetime(2026, 10, 25, 3, tzinfo=UTC), "Europe/Amsterdam")
            expected = store.read(None, "2026-10-25")
            assert [row["backend"] for row in expected] == ["sqlite", backend]
            assert (
                migrate_archive(target, restored, overwrite_target=True)["oep_storage_samples"] == 2
            )
        finally:
            target_repository.close()
        SCHEMA.drop_all(target)
        restored_repository = SqlPlanRepository(tmp_path / "restored", engine=restored)
        try:
            assert StorageHistory(restored).read(None, "2026-10-25") == expected
        finally:
            restored_repository.close()
    finally:
        source_repository.close()
        for engine in (source, target, restored):
            SCHEMA.drop_all(engine)
            engine.dispose()
