"""Daily capacity evidence stays compact, restart-safe and portable on all supported engines."""

import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from open_energy_planner.storage.adapters.lifecycle.transactions import lifecycle_transaction
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
        store.sample(lambda: datetime(2026, 10, 25, 2, 59, tzinfo=UTC), "Europe/Amsterdam")
        store.sample(lambda: datetime(2026, 10, 25, 4, tzinfo=UTC), None)
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
            store.sample(lambda: now, "Europe/Amsterdam")
        assert "private-database-password" not in "".join(traceback.format_exception(failure.value))
        assert store.read(None, "2026-10-25") == []
        monkeypatch.setattr(
            "open_energy_planner.storage.adapters.usage_history.storage_usage", probe
        )

        def reject_insert(_connection, _cursor, statement, _parameters, _context, _many):
            """Fail the daily INSERT transaction after its size probe and statement succeed."""
            if statement.startswith("INSERT INTO oep_storage_samples"):
                raise OperationalError(
                    "private-insert-password", {}, Exception("private-insert-password")
                )

        event.listen(engine, "after_cursor_execute", reject_insert)
        try:
            with pytest.raises(OSError) as failure:
                store.sample(lambda: now, "Europe/Amsterdam")
        finally:
            event.remove(engine, "after_cursor_execute", reject_insert)
        assert len(calls) == 1
        assert store.read(None, "2026-10-25") == []
        assert "private-insert-password" not in "".join(traceback.format_exception(failure.value))
        calls.clear()
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(
                workers.map(
                    lambda _: StorageHistory(engine).sample(lambda: now, "Europe/Amsterdam"),
                    range(2),
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
        store.sample(lambda: datetime(2026, 10, 28, 14, tzinfo=UTC), "Europe/Amsterdam")
        store.sample(lambda: datetime(2026, 10, 28, 15, tzinfo=UTC), "Europe/Amsterdam")
        repository.close()
        repository = SqlPlanRepository(tmp_path, engine=engine)
        rows = StorageHistory(engine).read(None, "2026-10-28")
        assert [row["day"] for row in rows] == ["2026-10-25", "2026-10-28"]
        assert rows[-1]["database_bytes"] is None
        assert store.read("2026-10-26", "2026-10-27") == []
        monkeypatch.setattr(
            "open_energy_planner.storage.adapters.usage_history.storage_usage", probe
        )
        store.sample(lambda: datetime(2026, 3, 29, 1, 59, tzinfo=UTC), "Europe/Amsterdam")
        assert len(calls) == 1
        store.sample(lambda: datetime(2026, 3, 29, 2, tzinfo=UTC), "Europe/Amsterdam")
        assert len(calls) == 2
        assert len(store.read("2026-03-29", "2026-03-29")) == 1
    finally:
        repository.close()
        SCHEMA.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("backend,url", BACKENDS, ids=[name for name, _ in BACKENDS])
def test_engine_guard_wait_cannot_backdate_a_daily_measurement(backend, url, tmp_path, monkeypatch):
    """Crossing midnight while another writer owns the engine defers measurement until the new day's 04:00."""
    engine = archive_engine(tmp_path, url)
    SCHEMA.drop_all(engine)
    repository = SqlPlanRepository(tmp_path, engine=engine)
    store = StorageHistory(engine)
    started, clock_read = Event(), Event()
    now = datetime(2026, 11, 17, 23, 59, tzinfo=UTC)
    monkeypatch.setattr(
        "open_energy_planner.storage.adapters.usage_history.storage_usage",
        lambda _: StorageUsage(backend, database_bytes=8192),
    )

    def clock():
        """Observe the actual clock-read boundary independently of the adapter's date calculation."""
        clock_read.set()
        return now

    def collect():
        """Queue a real measurement behind an already-held shared engine guard."""
        started.set()
        store.sample(clock, "UTC")

    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            with lifecycle_transaction(engine):
                pending = workers.submit(collect)
                assert started.wait(2)
                assert not clock_read.wait(0.1)
                now = datetime(2026, 11, 18, 0, 1, tzinfo=UTC)
            pending.result(timeout=2)
        assert clock_read.is_set()
        assert store.read(None, "2026-11-18") == []
        now = datetime(2026, 11, 18, 4, tzinfo=UTC)
        store.sample(clock, "UTC")
        rows = store.read(None, "2026-11-18")
        assert len(rows) == 1 and rows[0]["day"] == "2026-11-18"
        assert rows[0]["observed_at"] == "2026-11-18T04:00:00.000000+00:00"
    finally:
        repository.close()
        SCHEMA.drop_all(engine)
        engine.dispose()
