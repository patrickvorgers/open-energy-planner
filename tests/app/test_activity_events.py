"""Business events retain source ownership, restart truth and race-free subscriptions."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.activities.api import EVENTS
from open_energy_planner.activities.events import Events, Health, Outcome
from open_energy_planner.activities.storage import ActivityStore
from open_energy_planner.app import create_app
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
TOKEN = "activity-test-only-0123456789abcdef"


def test_replay_atomic_subscription_reset_and_detached_snapshots(tmp_path):
    """Events emitted after a snapshot and before waiting are replayed without a lost wakeup."""

    async def scenario():
        """Exercise a waiting subscriber, cursor overflow and a different process epoch."""
        repository = SqlPlanRepository(tmp_path)
        try:
            events = Events(ActivityStore(repository.engine), lambda: NOW)
            initial = await events.subscribe()
            assert initial["reset"] and initial["snapshot"]["health"] == []
            events.availability("prices", Health.DEGRADED, "Retained prices remain usable.")
            update = await events.subscribe(initial["cursor"])
            assert [row["topic"] for row in update["events"]] == ["health"]
            update["events"][0]["value"]["state"] = "unavailable"
            assert events.snapshot()["health"][0]["state"] == "degraded"
            waiting = asyncio.create_task(events.subscribe(update["cursor"], timeout=1))
            await asyncio.sleep(0)
            events.availability("prices", Health.AVAILABLE, "Fresh prices available.")
            assert (await waiting)["events"][0]["value"]["state"] == "available"
            cursor = events.cursor
            for index in range(513):
                events.availability("prices", Health.AVAILABLE, str(index))
            assert events.changes(cursor)["reset"]
            assert events.changes("another-process:1")["reset"]
            assert (await events.subscribe(events.cursor, timeout=0.001))["events"] == []
        finally:
            repository.close()

    asyncio.run(scenario())


def test_failure_recovery_notifications_and_six_hour_schedule(tmp_path):
    """An empty success is not recovery. Availability and notices require explicit domain publication."""

    async def scenario():
        """Retain real repeated attempts while keeping the independent notification contract."""
        repository = SqlPlanRepository(tmp_path)
        try:
            events = Events(ActivityStore(repository.engine), lambda: NOW)
            first = await events.start("weather", "weather", "Fetch weather")
            await events.finish(first, Outcome.FAILED, "Retained forecast still usable.")
            second = await events.start("weather", "weather", "Fetch weather")
            with pytest.raises(ValueError, match="usable result"):
                await events.finish(second, Outcome.COMPLETED, "Empty response.")
            await events.finish(second, Outcome.FAILED, "No usable weather returned.")
            third = await events.start("weather", "weather", "Fetch weather")
            await events.finish(third, Outcome.COMPLETED, "Usable forecast received.", usable=True)
            await events.flush()
            rows = events.store.read(NOW - timedelta(days=1), NOW + timedelta(days=1))
            assert len(rows) == 3
            by_id = {row["id"]: row for row in rows}
            assert not by_id[second]["recovered"] and by_id[third]["recovered"]
            assert events.snapshot()["notifications"] == {}
            assert events.snapshot()["health"][0]["state"] == "unknown"
            events.scheduled("weather", "weather", "Fetch weather", NOW + timedelta(hours=6))
            events.scheduled("models", "training", "Train model", NOW + timedelta(hours=7))
            assert [row["owner"] for row in events.snapshot()["scheduled"]] == ["weather"]
            events.scheduled("weather", "weather", "Fetch weather")
            assert events.snapshot()["scheduled"] == []
        finally:
            repository.close()

    asyncio.run(scenario())


def test_restart_interruption_retention_and_stable_paging(tmp_path):
    """Seven-day retention survives restart. Equal start timestamps never skip or duplicate executions."""

    async def scenario():
        """Create unfinished and completed real records in the shared database."""
        repository = SqlPlanRepository(tmp_path)
        try:
            store = ActivityStore(repository.engine)
            events = Events(store, lambda: NOW)
            first = await events.start("planning", "planning", "Calculate plan")
            second = await events.start("solar", "training", "Update solar forecast")
            await events.finish(second, Outcome.COMPLETED, "Forecast installed.", usable=True)
            await events.flush()
            store.restart(NOW + timedelta(hours=1))
            rows = store.read(NOW - timedelta(days=1), NOW + timedelta(days=1), limit=1)
            next_rows = store.read(
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                (rows[0]["started_at"], rows[0]["id"]),
            )
            assert {row["id"] for row in rows + next_rows} == {first, second}
            states = {row["id"]: row["state"] for row in rows + next_rows}
            assert states == {first: "interrupted", second: "completed"}
            store.restart(NOW + timedelta(days=8))
            assert store.read(NOW - timedelta(days=1), NOW + timedelta(days=9)) == []
        finally:
            repository.close()

    asyncio.run(scenario())


def test_api_authentication_day_validation_and_live_cursor(tmp_path):
    """Both APIs reuse existing authorization. History defaults to the installation day."""

    async def scenario():
        """Drive the actual app with no HA connection or real user data."""
        app = create_app(tmp_path, TOKEN)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/v1/events")).status == 401
            assert (await client.get("/v1/activities")).status == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            response = await client.get("/v1/events", headers=headers)
            initial = await response.json()
            assert initial["reset"]
            identity = await app[EVENTS].start(
                "prices:nord_pool", "prices", "Fetch published prices"
            )
            await app[EVENTS].finish(
                identity, Outcome.COMPLETED, "Market: NL. Usable prices received.", usable=True
            )
            changes = await (
                await client.get(
                    "/v1/events", params={"cursor": initial["cursor"]}, headers=headers
                )
            ).json()
            assert any(
                row["topic"] == "activity" and row["value"]["id"] == identity
                for row in changes["events"]
            )
            history = await (await client.get("/v1/activities", headers=headers)).json()
            assert history["timezone"] == "UTC"
            assert any(row["id"] == identity for row in history["activities"])
            assert (await client.get("/v1/activities?day=invalid", headers=headers)).status == 400
            assert (
                await client.get("/v1/activities?before=invalid", headers=headers)
            ).status == 400

    asyncio.run(scenario())


def test_optional_writer_is_nonblocking_and_discloses_lost_updates(tmp_path):
    """A full queue cannot delay or reject domain work, and a new subscriber sees incomplete evidence."""

    async def scenario():
        """Keep the writer unscheduled until a bounded queue has deliberately filled."""
        repository = SqlPlanRepository(tmp_path)
        events = Events(ActivityStore(repository.engine), lambda: NOW)
        events.pending = asyncio.Queue(maxsize=1)
        try:
            identity = await events.start("planning", "planning", "Calculate plan")
            await events.finish(identity, Outcome.COMPLETED, "Plan published.", usable=True)
            assert events.running == {}
            assert events.snapshot()["lost_updates"] == 1
            assert not events.snapshot()["history_available"]
            await events.close()
            assert events.changes()["snapshot"]["lost_updates"] == 1
        finally:
            repository.close()

    asyncio.run(scenario())


def test_threaded_producer_drains_and_restores_recovery_after_restart(tmp_path):
    """Threaded model work publishes on the app loop, with deterministic same-instant outcome order."""

    async def scenario():
        """Use actual SQL persistence and restart reconstruction, without a broker or mock store."""
        repository = SqlPlanRepository(tmp_path)
        store = ActivityStore(repository.engine)
        events = Events(store, lambda: NOW)
        events.loop = asyncio.get_running_loop()
        try:

            def calculate():
                """Publish the actual worker-owned failure before propagating its domain exception."""
                with events.thread_attempt(
                    "models", "training", "Compare models", failure="Baseline models remain active."
                ):
                    raise ValueError("Synthetic numerical failure")

            with pytest.raises(ValueError, match="Synthetic numerical"):
                await asyncio.to_thread(calculate)
            await events.close()
            restored = Events(store, lambda: NOW)
            restored.failed, restored.sequence = store.restart(NOW)
            assert restored.failed == {("models", "training")}
            identity = await restored.start("models", "training", "Compare models")
            await restored.finish(identity, Outcome.COMPLETED, "Usable evidence.", usable=True)
            await restored.close()
            assert store.restart(NOW)[0] == set()
            rows = store.read(NOW - timedelta(days=1), NOW + timedelta(days=1))
            assert next(row for row in rows if row["id"] == identity)["recovered"]
        finally:
            repository.close()

    asyncio.run(scenario())


def test_automatic_ha_price_delivery_has_one_activity_and_actual_transport(tmp_path):
    """Pending bridge delivery is not a failed request or a new activity on every refresh poll."""
    from open_energy_planner.prices.adapters.home_assistant import AutomaticPublishedPrices
    from open_energy_planner.prices.service import PriceService
    from open_energy_planner.prices.storage.adapters.store import PriceStore
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from tests.app.price.test_ha_prices import report, response

    async def scenario():
        """Request and accept a complete DST delivery day through the actual HA normalization path."""
        repository = SqlPlanRepository(tmp_path)
        now = NOW.replace(hour=0)
        events = Events(ActivityStore(repository.engine), lambda: now)
        try:
            source = AutomaticPublishedPrices("NL", None)
            service = PriceService(
                "NL", source, None, PriceStore(SqlDocuments(repository.engine, "prices"))
            )
            service.events = events
            await service.refresh(now)
            identity = next(iter(events.running))
            await service.refresh(now + timedelta(minutes=1))
            assert list(events.running) == [identity]
            body = report(source, now)
            body["responses"] = [response(source, now.date(), now)]
            source.receive(body, now)
            service.attempts.clear()
            await service.refresh(now)
            assert not events.running
            await events.close()
            rows = events.store.read(now - timedelta(days=1), now + timedelta(days=1))
            assert len(rows) == 1
            assert rows[0]["id"] == identity and rows[0]["state"] == "completed"
            assert rows[0]["owner"] == "prices:nord_pool:NL"
            assert "Transport: Home Assistant" in rows[0]["detail"]
            assert not rows[0]["recovered"]
        finally:
            repository.close()

    asyncio.run(scenario())


def test_managed_gateway_shutdown_releases_active_subscription(tmp_path, monkeypatch):
    """The outer maintenance gateway cannot wait on a stream that only inner shutdown can release."""
    from open_energy_planner.runtime.maintenance import MAINTENANCE, create_managed_app

    async def scenario():
        """Observe actual request admission without replacing the underlying subscription behavior."""
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            initial = await (await client.get("/v1/events", headers=headers)).json()
            events = app[MAINTENANCE].runner.app[EVENTS]
            entered = asyncio.Event()
            original = events.subscribe

            async def observe(cursor=None, timeout=25):
                """Signal that the live request reached the real event collector."""
                entered.set()
                return await original(cursor, timeout)

            monkeypatch.setattr(events, "subscribe", observe)
            pending = asyncio.create_task(
                client.get("/v1/events", params={"cursor": initial["cursor"]}, headers=headers)
            )
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(client.server.close(), 2)
            response = await pending
            assert response.status == 200
        finally:
            await client.close()

    asyncio.run(scenario())
