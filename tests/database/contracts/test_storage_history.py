"""Daily capacity evidence stays compact, restart-safe and portable on all supported engines."""

import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import OperationalError

from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from open_energy_planner.storage.adapters.usage import StorageUsage
from open_energy_planner.storage.adapters.usage_history import StorageHistory
from tests.database.support.backends import BACKENDS


@pytest.mark.parametrize("backend,url", BACKENDS, ids=[name for name, _ in BACKENDS])
def test_daily_measurement_calendar_restart_gaps_and_large_bytes(
    backend, url, tmp_path, monkeypatch
):
    """Measure only after local 04:00, once across concurrent restarts, preserving missing and >32-bit bytes."""
    engine = archive_engine(tmp_path, url)
    SCHEMA.drop_all(engine)
    repository = SqlPlanRepository(tmp_path, engine=engine)
    calls = []

    def probe(_engine):
        """Return independent large synthetic measurements without touching production data."""
        calls.append(True)
        return StorageUsage(
            backend, database_bytes=5 * 1024**3, database_estimated=backend in ("mysql", "mariadb")
        )

    monkeypatch.setattr("open_energy_planner.storage.adapters.usage_history.storage_usage", probe)
    store = StorageHistory(engine)
    try:
        # Amsterdam's DST fall-back day reaches 04:00 at 03:00 UTC, only once.
        store.sample(datetime(2026, 10, 25, 2, 59, tzinfo=UTC), "Europe/Amsterdam")
        store.sample(datetime(2026, 10, 25, 4, tzinfo=UTC), None)
        assert calls == []
        now = datetime(2026, 10, 25, 3, tzinfo=UTC)

        def failed(_engine):
            """Reject a transaction with private driver details, leaving the day available for retry."""
            raise OperationalError(
                "private-database-password", {}, Exception("private-database-password")
            )

        monkeypatch.setattr(
            "open_energy_planner.storage.adapters.usage_history.storage_usage", failed
        )
        with pytest.raises(OSError) as failure:
            store.sample(now, "Europe/Amsterdam")
        assert "private-database-password" not in "".join(traceback.format_exception(failure.value))
        assert store.read(None, "2026-10-25") == []
        monkeypatch.setattr(
            "open_energy_planner.storage.adapters.usage_history.storage_usage", probe
        )
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(
                workers.map(
                    lambda _: StorageHistory(engine).sample(now, "Europe/Amsterdam"), range(2)
                )
            )
        assert len(calls) == 1
        rows = store.read("2026-10-25", "2026-10-25")
        assert len(rows) == 1
        assert rows[0]["database_bytes"] == 5368709120
        assert rows[0]["day"] == "2026-10-25"
        assert rows[0]["observed_at"] == "2026-10-25T03:00:00.000000+00:00"
        assert rows[0]["timezone"] == "Europe/Amsterdam"
        assert rows[0]["database_estimated"] == (backend in ("mysql", "mariadb"))
        monkeypatch.setattr(
            "open_energy_planner.storage.adapters.usage_history.storage_usage",
            lambda _: StorageUsage(backend, measurement_failed=True),
        )
        store.sample(datetime(2026, 10, 28, 14, tzinfo=UTC), "Europe/Amsterdam")
        store.sample(datetime(2026, 10, 28, 15, tzinfo=UTC), "Europe/Amsterdam")
        repository.close()
        repository = SqlPlanRepository(tmp_path, engine=engine)
        rows = StorageHistory(engine).read(None, "2026-10-28")
        assert [row["day"] for row in rows] == ["2026-10-25", "2026-10-28"]
        assert rows[-1]["database_bytes"] is None
        assert store.read("2026-10-26", "2026-10-27") == []
        monkeypatch.setattr(
            "open_energy_planner.storage.adapters.usage_history.storage_usage", probe
        )
        store.sample(datetime(2026, 3, 29, 1, 59, tzinfo=UTC), "Europe/Amsterdam")
        assert len(calls) == 1
        store.sample(datetime(2026, 3, 29, 2, tzinfo=UTC), "Europe/Amsterdam")
        assert len(calls) == 2
        assert len(store.read("2026-03-29", "2026-03-29")) == 1
    finally:
        repository.close()
        SCHEMA.drop_all(engine)
        engine.dispose()
