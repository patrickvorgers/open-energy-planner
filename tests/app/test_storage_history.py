"""Authenticated period reads and daily lifecycle collection expose no connection details."""

import asyncio
import threading
from contextlib import suppress
from datetime import UTC, date, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.storage.api import period_start
from tests.ha_contract.support.metadata import setup_metadata


@pytest.mark.parametrize(
    "end,period,start",
    [
        ("2026-03-31", "month", "2026-02-28"),
        ("2024-02-29", "year", "2023-02-28"),
        ("2026-01-17", "quarter", "2025-10-17"),
        ("2026-11-01", "week", "2026-10-26"),
        ("2026-11-17", "all", None),
    ],
)
def test_history_windows_use_calendar_months(end, period, start):
    """Month ends and leap years clamp correctly while weeks include exactly seven dates."""
    result = period_start(date.fromisoformat(end), period)
    assert (result.isoformat() if result else None) == start


def test_history_authentication_background_sampling_and_safe_failures(tmp_path, monkeypatch):
    """A real started app collects once without a page visit, then refresh reads never add points."""
    import open_energy_planner.storage.api as storage_api

    class Clock(datetime):
        """Keep the daily worker and period endpoint on one independently chosen UTC instant."""

        instant = datetime(2026, 11, 17, 12, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            """Return a deterministic after-schedule instant for the real app lifecycle."""
            return cls.instant

    monkeypatch.setattr(storage_api, "datetime", Clock)

    async def scenario():
        """Run actual middleware and background startup, checking read-only refresh and private error handling."""
        token = "storage-history-test-token-0123456789"
        headers = {"Authorization": f"Bearer {token}"}
        async with TestClient(TestServer(create_app(tmp_path, token))) as client:
            assert (await client.get("/v1/storage/history")).status == 401
            for _ in range(100):
                response = await client.get("/v1/storage/history", headers=headers)
                body = await response.json()
                if body["samples"]:
                    break
                await asyncio.sleep(0.01)
            assert response.headers["Cache-Control"] == "no-store"
            assert body["period"] == "month" and body["start"] == "2026-10-17"
            assert body["end"] == "2026-11-17" and body["sample_hour"] == 4
            assert len(body["samples"]) == 1
            assert body["samples"][0]["database_bytes"] > 0
            assert body["latest"] == body["samples"][-1]
            assert (
                await (await client.get("/v1/storage/history?period=all", headers=headers)).json()
            )["samples"] == body["samples"]
            custom_response = await client.get(
                "/v1/storage/history?start=2026-11-17&end=2026-11-17", headers=headers
            )
            assert custom_response.status == 200
            custom = await custom_response.json()
            assert custom["start"] == custom["end"] == "2026-11-17"
            assert custom["samples"] == body["samples"]
            assert custom["calendar"] == {"min_day": "2026-11-17", "max_day": "2026-11-17"}
            for query in (
                "start=2026-11-18&end=2026-11-17",
                "start=2026-11-17",
                "start=2026-11-17&end=2026-11-18",
                "start=2026-02-30&end=2026-11-17",
            ):
                invalid_range = await client.get("/v1/storage/history?" + query, headers=headers)
                assert invalid_range.status == 400
                assert (await invalid_range.json())[
                    "error"
                ] == "Choose increasing calendar dates no later than today."
            Clock.instant = datetime(2027, 1, 17, 12, tzinfo=UTC)
            older = await (await client.get("/v1/storage/history", headers=headers)).json()
            assert older["samples"] == []
            assert older["latest"] == body["samples"][-1]
            invalid = await client.get(
                "/v1/storage/history?period=private-password", headers=headers
            )
            assert invalid.status == 400
            assert "private-password" not in await invalid.text()

            def failed(*_args):
                """Simulate a database exception that must never reach the HTTP response."""
                raise OSError("private-password@host/database")

            monkeypatch.setattr(storage_api.StorageHistory, "read", failed)
            failed_response = await client.get("/v1/storage/history", headers=headers)
            assert failed_response.status == 503
            assert "private-password" not in await failed_response.text()

    asyncio.run(scenario())


def test_history_waits_for_ha_timezone_and_uses_its_calendar(tmp_path, monkeypatch):
    """An unavailable HA timezone never becomes UTC, and Auckland's next day owns the returned month."""
    import open_energy_planner.storage.api as storage_api

    class Clock(datetime):
        """Place the app at an instant that crosses the Auckland date boundary."""

        @classmethod
        def now(cls, tz=None):
            """Return UTC while the installation is already in the following calendar day."""
            return datetime(2026, 11, 17, 23, 30, tzinfo=UTC)

    monkeypatch.setattr(storage_api, "datetime", Clock)

    async def scenario():
        """Admit actual HA metadata before selecting the installation-local month window."""
        token = "storage-calendar-test-token-0123456789"
        headers = {"Authorization": f"Bearer {token}"}
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            pending = await (await client.get("/v1/storage/history", headers=headers)).json()
            assert (
                pending["timezone"] is None and pending["start"] is None and pending["end"] is None
            )
            assert pending["samples"] == []
            assert pending["latest"] is None
            metadata = {**setup_metadata(), "timezone": "Pacific/Auckland"}
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            ).status == 200
            ready = await (await client.get("/v1/storage/history", headers=headers)).json()
            assert ready["timezone"] == "Pacific/Auckland"
            assert ready["end"] == "2026-11-18" and ready["start"] == "2026-10-18"

    asyncio.run(scenario())


@pytest.mark.parametrize("write_fails", [False, True])
def test_shutdown_drains_daily_measurement_before_repository_close(
    tmp_path, monkeypatch, write_fails
):
    """Migration/shutdown waits for an in-flight daily SQL writer before disposing its engine."""
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
    from open_energy_planner.storage.adapters.usage_history import StorageHistory

    entered, release = threading.Event(), threading.Event()
    order = []
    original_close = SqlPlanRepository.close

    def sample(_self, _now, _timezone):
        """Hold the off-loop sampler until shutdown is actively waiting on it."""
        entered.set()
        assert release.wait(10)
        order.append("measurement finished")
        if write_fails:
            raise OSError("synthetic write failed during shutdown")

    def close(self):
        """Observe the actual shared repository cleanup after its sampler drains."""
        order.append("repository closed")
        original_close(self)

    monkeypatch.setattr(StorageHistory, "sample", sample)
    monkeypatch.setattr(SqlPlanRepository, "close", close)

    async def scenario():
        """Begin normal app cleanup while a real worker is inside a blocking storage operation."""
        client = TestClient(TestServer(create_app(tmp_path, "storage-drain-test-token-0123456789")))
        cleanup = None
        await client.start_server()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            worker = next(
                task
                for task in asyncio.all_tasks()
                if task.get_coro().__qualname__.endswith(
                    "register.<locals>.lifecycle.<locals>.collect"
                )
            )
            cleanup = asyncio.create_task(client.close())
            for _ in range(200):
                if worker.cancelling():
                    break
                await asyncio.sleep(0.01)
            assert worker.cancelling()
            assert "repository closed" not in order
            release.set()
            await asyncio.wait_for(asyncio.shield(cleanup), 1)
            assert order == ["measurement finished", "repository closed"]
        finally:
            release.set()
            if cleanup is not None and not cleanup.done():
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
            await client.close()

    asyncio.run(scenario())
