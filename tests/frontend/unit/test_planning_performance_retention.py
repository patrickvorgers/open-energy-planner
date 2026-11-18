"""Calculation diagnostics expire by HA calendar date, independently of plan and financial history."""

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.optimization import performance
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from open_energy_planner.synthetic import capture


def freeze_clock(monkeypatch, instant):
    """Control the history owner's wall clock while preserving normal aware datetime parsing."""
    clock = {"now": instant}

    class Clock(datetime):
        """Return the chosen instant in the requested timezone."""

        @classmethod
        def now(cls, tz=None):
            """Expose a mutable test clock without changing application clocks elsewhere."""
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setattr(performance, "datetime", Clock)
    return clock


@pytest.mark.parametrize(
    "instant,timezone,expected",
    [
        ("2026-11-18T12:00:00+00:00", "Europe/Amsterdam", ("2026-11-05", "2026-11-18")),
        ("2026-11-18T23:30:00+00:00", "Europe/Amsterdam", ("2026-11-06", "2026-11-19")),
        ("2026-11-18T23:30:00+00:00", "America/New_York", ("2026-11-05", "2026-11-18")),
        ("2026-11-07T12:00:00+00:00", "Europe/Amsterdam", ("2026-10-25", "2026-11-07")),
    ],
)
def test_retained_days_are_fourteen_local_dates_including_today(instant, timezone, expected):
    """The same instant can belong to different HA dates, and a DST boundary retains fourteen dates."""
    actual = performance.retained_days(datetime.fromisoformat(instant), timezone)
    assert tuple(day.isoformat() for day in actual) == expected


def test_api_hides_expired_diagnostics_even_before_cleanup(tmp_path, monkeypatch):
    """Pending physical cleanup never exposes the expired row or curve outside Oct 25 through Nov 7."""
    freeze_clock(monkeypatch, datetime(2026, 11, 7, 12, tzinfo=UTC))
    monkeypatch.setattr(performance.CalculationHistory, "prune", lambda self, before: False)
    repository = SqlPlanRepository(tmp_path)
    documents = SqlDocuments(repository.engine, "planning")
    history = performance.CalculationHistory(documents)
    for started in (
        datetime(2026, 10, 24, 21, 59, 59, tzinfo=UTC),
        datetime(2026, 10, 24, 22, tzinfo=UTC),
    ):
        history.record(capture(started), None, started, started, "planning_error")
    expired = history.day("2026-10-24", "Europe/Amsterdam")[0]
    retained = history.day("2026-10-25", "Europe/Amsterdam")[0]

    async def scenario():
        """Read the actual day/detail routes with a discovered installation timezone."""
        app = web.Application()
        performance.register(app, documents, lambda: "Europe/Amsterdam")
        async with TestClient(TestServer(app)) as client:
            for selected in ("2026-10-24", "2026-10-25", "2026-11-08"):
                response = await client.get("/v1/planning-performance?day=" + selected)
                assert response.status == 200
                value = await response.json()
                assert value["retention"] == {
                    "days": 14,
                    "min_day": "2026-10-25",
                    "max_day": "2026-11-07",
                }
                assert len(value["calculations"]) == (1 if selected == "2026-10-25" else 0)
            response = await client.get("/v1/planning-performance/" + expired["id"])
            assert response.status == 410
            assert await response.text() == "Calculation is outside the retained period"
            response = await client.get("/v1/planning-performance/" + retained["id"])
            assert response.status == 200
            assert (await response.json())["started_at"] == "2026-10-24T22:00:00+00:00"
        assert documents.exists("calculation-" + expired["id"] + ".json")

    try:
        asyncio.run(scenario())
    finally:
        repository.close()


def test_background_cleanup_runs_at_startup_and_while_idle(tmp_path, monkeypatch):
    """Startup removes expired diagnostics and the next local date cleans the boundary without another run."""
    clock = freeze_clock(monkeypatch, datetime(2026, 11, 18, 12, tzinfo=UTC))
    monkeypatch.setattr(performance, "HISTORY_CLOCK_POLL_SECONDS", 0.01)
    repository = SqlPlanRepository(tmp_path)
    documents = SqlDocuments(repository.engine, "planning")
    history = performance.CalculationHistory(documents)
    for hour in (22, 23):
        started = datetime(2026, 11, 4, hour, 30, tzinfo=UTC)
        history.record(capture(started), None, started, started, "planning_error")
    old = history.day("2026-11-04", "Europe/Amsterdam")[0]
    boundary = history.day("2026-11-05", "Europe/Amsterdam")[0]
    prune = performance.CalculationHistory.prune

    async def scenario():
        """Observe actual committed deletions and queue draining through the registered lifecycle."""
        changed = asyncio.Event()
        loop = asyncio.get_running_loop()

        def observed_prune(owner, before):
            """Signal only after a real cleanup batch committed."""
            result = prune(owner, before)
            if result:
                loop.call_soon_threadsafe(changed.set)
            return result

        monkeypatch.setattr(performance.CalculationHistory, "prune", observed_prune)
        app = web.Application()
        record = performance.register(app, documents, lambda: "Europe/Amsterdam")
        async with TestClient(TestServer(app)):
            await asyncio.wait_for(changed.wait(), 2)
            assert not documents.exists("calculation-" + old["id"] + ".json")
            assert documents.exists("calculation-" + boundary["id"] + ".json")
            changed.clear()
            clock["now"] += timedelta(days=1)
            await asyncio.wait_for(changed.wait(), 2)
            assert not documents.exists("calculation-" + boundary["id"] + ".json")
            expired = datetime(2026, 11, 4, 23, 45, tzinfo=UTC)
            fresh = clock["now"]
            await record(capture(expired), None, expired, expired, "planning_error")
            await record(capture(fresh), None, fresh, fresh, "planning_error")
        assert history.day("2026-11-05", "Europe/Amsterdam") == []
        assert len(history.day("2026-11-19", "Europe/Amsterdam")) == 1

    try:
        asyncio.run(scenario())
    finally:
        repository.close()


def test_cleanup_failure_retries_without_leaking_private_values(tmp_path, monkeypatch, caplog):
    """Repeated cleanup failures log once without exception values, then actual deletion resumes."""
    import logging

    freeze_clock(monkeypatch, datetime(2026, 11, 18, 12, tzinfo=UTC))
    monkeypatch.setattr(performance, "HISTORY_CLOCK_POLL_SECONDS", 0.01)
    caplog.set_level(logging.INFO, logger=performance.__name__)
    repository = SqlPlanRepository(tmp_path)
    documents = SqlDocuments(repository.engine, "planning")
    history = performance.CalculationHistory(documents)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    history.record(capture(started), None, started, started, "planning_error")
    prune, calls = performance.CalculationHistory.prune, []

    async def scenario():
        """Wait for a committed retry through the same background worker as normal operation."""
        recovered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def transient(owner, before):
            """Fail the first two attempts, then signal an actual committed deletion."""
            calls.append(before)
            if len(calls) <= 2:
                raise ValueError("private-cleanup-sentinel")
            changed = prune(owner, before)
            if changed:
                loop.call_soon_threadsafe(recovered.set)
            return changed

        monkeypatch.setattr(performance.CalculationHistory, "prune", transient)
        app = web.Application()
        performance.register(app, documents, lambda: "UTC")
        async with TestClient(TestServer(app)):
            await asyncio.wait_for(recovered.wait(), 2)
        assert list(documents.iter_prefix("calculation-")) == []

    try:
        asyncio.run(scenario())
        assert "private-cleanup-sentinel" not in caplog.text
        assert caplog.text.count("Calculation history cleanup failed") == 1
        assert caplog.text.count("Calculation history cleanup recovered") == 1
    finally:
        repository.close()


def test_shutdown_waits_for_an_inflight_cleanup_thread(tmp_path, monkeypatch):
    """The archive remains owned until an off-loop cleanup returns, rather than cancelling its await."""
    freeze_clock(monkeypatch, datetime(2026, 11, 18, 12, tzinfo=UTC))
    repository = SqlPlanRepository(tmp_path)
    entered, release, finished = Event(), Event(), Event()

    def slow_prune(_owner, _before):
        """Hold the cleanup operation so shutdown must await its completion."""
        entered.set()
        assert release.wait(5)
        finished.set()
        return False

    monkeypatch.setattr(performance.CalculationHistory, "prune", slow_prune)

    async def scenario():
        """Close the real cleanup context while its worker thread is still active."""
        app = web.Application()
        performance.register(app, SqlDocuments(repository.engine, "planning"), lambda: "UTC")
        context = app.cleanup_ctx[0](app)
        await context.__anext__()
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(context.__anext__())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(closing, 2)
        assert finished.is_set()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        repository.close()
