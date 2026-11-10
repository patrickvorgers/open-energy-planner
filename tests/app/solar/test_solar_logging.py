"""Solar availability logs follow accepted evidence and the existing retry/cache schedules."""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web

from open_energy_planner.notifications.api import NOTIFICATIONS
from open_energy_planner.notifications.collection import NotificationCenter
from open_energy_planner.solar import api as solar_api
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.solar.storage import SolarStore
from open_energy_planner.solar.weather import OpenMeteoSolarSource
from tests.app.solar.support import SITE, START, plant, weather
from tests.app.solar.test_solar_weather import Session
from tests.database.support.archive import make_store


def controlled_poll(monkeypatch):
    """Advance only the solar module's clock and poll waits without changing shared asyncio behavior."""
    clock = [START]
    sleeps, resume = asyncio.Queue(), asyncio.Queue()

    class Clock(datetime):
        """Expose independently selected UTC instants to the actual solar lifecycle."""

        @classmethod
        def now(cls, timezone=None):
            """Return the explicit test instant rather than progressing wall time."""
            return clock[0].astimezone(timezone) if timezone else clock[0]

    async def sleep(seconds):
        """Observe each actual requested poll interval and hold the next loop until released."""
        sleeps.put_nowait(seconds)
        await resume.get()

    monkeypatch.setattr(solar_api, "datetime", Clock)
    monkeypatch.setattr(
        solar_api,
        "asyncio",
        SimpleNamespace(
            create_task=asyncio.create_task,
            sleep=sleep,
            CancelledError=asyncio.CancelledError,
        ),
    )
    return clock, sleeps, resume


def solar_app(runtime, *, live=None, weather_factory=None, membership=None):
    """Register the actual solar lifecycle while leaving unrelated HTTP composition inactive."""
    app = web.Application()
    app[NOTIFICATIONS] = NotificationCenter()
    solar_api.register(app, runtime, live, asyncio.Lock(), None, None, weather_factory, membership)
    return app


@pytest.mark.parametrize("surface", ["activity", "notifications"])
def test_configuration_worker_does_not_publish_mixed_solar_status(tmp_path, monkeypatch, surface):
    """Actual HTTP saves cannot expose a new configuration with the previous forecast during restoration."""
    from threading import Event

    from open_energy_planner.activities.events import Events, Outcome
    from open_energy_planner.solar.wire import configuration_document, parse_configuration
    from tests.app.forecast.support.http import configure_forecasts, running_forecasts
    from tests.app.solar.support import fixture_weather

    original_init = SolarRuntime.__init__
    original_restore = SolarRuntime.restore_feedback
    original_finish = Events.finish
    original_usable = solar_api.forecast_usable
    instances, mixed, completed = [], [], []
    release_worker = Event()
    target_version = None

    async def scenario():
        nonlocal target_version
        loop = asyncio.get_running_loop()
        finish_waiting, finish_release, finish_returned, restoring = (
            asyncio.Event() for _ in range(4)
        )
        held = False

        def capture_runtime(current, *arguments, **options):
            original_init(current, *arguments, **options)
            instances.append(current)

        def held_restore(current):
            if current.configuration and current.configuration.version == target_version:
                loop.call_soon_threadsafe(restoring.set)
                assert release_worker.wait(10), "Synthetic configuration barrier was not released"
            return original_restore(current)

        async def held_finish(events, identity, *arguments, **options):
            nonlocal held
            if not held and events.running.get(identity, {}).get("owner") == "solar":
                held = True
                finish_waiting.set()
                await finish_release.wait()
                result = await original_finish(events, identity, *arguments, **options)
                completed.append((arguments[0], options.get("usable")))
                finish_returned.set()
                return result
            return await original_finish(events, identity, *arguments, **options)

        def observed_usable(configuration, forecast, location, now):
            if restoring.is_set() and not release_worker.is_set():
                mixed.append(
                    (configuration.version, forecast.configuration_version if forecast else None)
                )
            return original_usable(configuration, forecast, location, now)

        monkeypatch.setattr(SolarRuntime, "__init__", capture_runtime)
        monkeypatch.setattr(SolarRuntime, "restore_feedback", held_restore)
        monkeypatch.setattr(Events, "finish", held_finish)
        monkeypatch.setattr(solar_api, "forecast_usable", observed_usable)
        monkeypatch.setattr(solar_api, "SOLAR_WORKER_POLL_SECONDS", 0.01)
        async with running_forecasts(tmp_path, weather_factory=fixture_weather) as (session, url):
            initial = configuration_document(plant())
            initial.pop("version")
            configured = await configure_forecasts(session, url, solar_definition=initial)
            pending = None
            try:
                await asyncio.wait_for(finish_waiting.wait(), 15)
                current = instances[0]
                old_forecast = current.forecast
                assert old_forecast is not None
                assert old_forecast.configuration_version == current.configuration.version
                baseline = await session.get(url + "/v1/notifications")
                assert baseline.status == 200
                assert not [
                    row
                    for row in (await baseline.json())["notifications"]
                    if row["owner"] == "solar"
                ]
                definition = configured["solar"]["configuration"]
                body = {
                    **definition,
                    "expected_version": definition["version"],
                    "sources": [{**definition["sources"][0], "capacity_w": 1100}],
                }
                target_version = parse_configuration(body).version
                pending = asyncio.create_task(session.put(url + "/v1/solar", json=body))
                await asyncio.wait_for(restoring.wait(), 5)
                assert current.configuration.version == target_version
                assert current.forecast is old_forecast
                finish_release.set()
                await asyncio.wait_for(finish_returned.wait(), 5)
                await asyncio.sleep(0)
                assert completed == [(Outcome.COMPLETED, True)]
                if surface == "activity":
                    assert mixed == []
                else:
                    response = await asyncio.wait_for(session.get(url + "/v1/notifications"), 2)
                    assert response.status == 200
                    document = await response.json()
                    assert not [row for row in document["notifications"] if row["owner"] == "solar"]
                    assert "solar" in document["unavailable_sources"]
            finally:
                finish_release.set()
                release_worker.set()
                if pending is not None:
                    response = await pending
                    assert response.status == 200, await response.text()
                    assert (await response.json())["configuration"]["version"] == target_version

    try:
        asyncio.run(scenario())
    finally:
        release_worker.set()


def test_notification_guard_retains_active_warning_until_coherent_recovery(tmp_path, monkeypatch):
    """Solar configuration work retains stale notices, but unrelated app work cannot delay recovery."""
    runtime = SolarRuntime(make_store(tmp_path, SolarStore))
    runtime.configure(plant())
    runtime.set_weather(SITE, weather())

    async def scenario():
        controlled_poll(monkeypatch)
        lock = asyncio.Lock()
        app = web.Application()
        center = NotificationCenter()
        app[NOTIFICATIONS] = center
        live = SimpleNamespace(report=SimpleNamespace(location=SITE))
        solar_api.register(app, runtime, live, lock, None, None, None, None)
        initial = center.document()
        assert len(initial["notifications"]) == 1
        notice = initial["notifications"][0]
        assert notice["owner"] == "solar" and not notice["stale"]
        result = await asyncio.to_thread(runtime.build, runtime.job(START))
        async with lock:
            with runtime.configuration_lock:
                assert runtime.install(result, START)
                during = center.document()
                assert len(during["notifications"]) == 1
                assert during["notifications"][0] == {**notice, "stale": True}
                assert during["unavailable_sources"] == ["solar"]
            assert center.document() == {"notifications": [], "unavailable_sources": []}
        assert center.document() == {"notifications": [], "unavailable_sources": []}

    asyncio.run(scenario())


@pytest.mark.parametrize("superseded", [False, True])
def test_failed_worker_reports_only_the_backoff_it_actually_recorded(
    superseded, tmp_path, monkeypatch, caplog
):
    """An old job cannot borrow a newer job's five-minute retry or overwrite its current failure state."""
    runtime = SolarRuntime(make_store(tmp_path, SolarStore))
    runtime.configure(plant())
    runtime.set_weather(SITE, weather())
    current_job = [None]

    async def failing_worker(_runtime, job):
        """Optionally replace inputs during the actual worker await, then reject the captured job."""
        if superseded:
            runtime.set_weather(SITE, weather(ghi=650))
            current_job[0] = runtime.job(START)
            assert runtime.failed(current_job[0], START) is True
            assert runtime.failed(job, START) is False
        else:
            current_job[0] = job
        raise OSError("Controlled worker failure")

    async def scenario():
        """Run one real forecast loop and preserve its unchanged current-input backoff state."""
        _clock, sleeps, _resume = controlled_poll(monkeypatch)
        monkeypatch.setattr(solar_api, "worker", failing_worker)
        app = solar_app(runtime)
        lifecycle = app.cleanup_ctx[0](app)
        await anext(lifecycle)
        try:
            assert await asyncio.wait_for(sleeps.get(), 3) == 5
            assert runtime.failed_key == current_job[0][0]
            assert runtime.retry_after == START + timedelta(minutes=5)
            assert runtime.issue == "solar_forecast_unusable"
        finally:
            await lifecycle.aclose()

    with caplog.at_level(logging.DEBUG, logger=solar_api.__name__):
        asyncio.run(scenario())
    failures = [
        record for record in caplog.records if "Solar forecast update failed" in record.message
    ]
    assert len(failures) == 1
    expected_retry = "Retrying in 5 seconds." if superseded else "Retrying in 5 minutes."
    assert expected_retry in failures[0].message


def test_empty_current_weather_never_reports_recovery_and_keeps_hourly_cadence(
    tmp_path, monkeypatch, caplog
):
    """Two valid empty reads remain unavailable. Fresh weather later recovers on the unchanged hourly schedule."""
    runtime = SolarRuntime(make_store(tmp_path, SolarStore))
    runtime.configure(plant())
    calls = []

    class Weather:
        """Return successful empty evidence twice, followed by independently specified physical hours."""

        async def read(self, location, now):
            """Record actual due reads rather than deriving expectations from production constants."""
            assert location == SITE
            calls.append(now)
            return () if len(calls) < 3 else weather(now)

    @asynccontextmanager
    async def factory():
        """Borrow the deterministic source in the actual weather lifecycle."""
        yield Weather()

    async def scenario():
        """Drive real due checks immediately before and at each independently chosen hourly boundary."""
        clock, sleeps, resume = controlled_poll(monkeypatch)
        live = SimpleNamespace(report=SimpleNamespace(location=SITE))
        app = solar_app(runtime, live=live, weather_factory=factory)
        lifecycle = app.cleanup_ctx[1](app)
        await anext(lifecycle)
        try:
            assert await asyncio.wait_for(sleeps.get(), 3) == 5
            assert calls == [START]
            assert not runtime.weather
            for elapsed, expected_calls in (
                (timedelta(minutes=59, seconds=59), 1),
                (timedelta(hours=1), 2),
                (timedelta(hours=1, minutes=59, seconds=59), 2),
                (timedelta(hours=2), 3),
            ):
                clock[0] = START + elapsed
                resume.put_nowait(None)
                assert await asyncio.wait_for(sleeps.get(), 3) == 5
                assert len(calls) == expected_calls
            assert runtime.weather == weather(START + timedelta(hours=2))
        finally:
            await lifecycle.aclose()

    with caplog.at_level(logging.DEBUG, logger=solar_api.__name__):
        asyncio.run(scenario())
    records = [record for record in caplog.records if "Solar weather refresh" in record.message]
    assert [record.levelno for record in records] == [logging.WARNING, logging.DEBUG, logging.INFO]
    assert "60 minutes" in records[0].message
    assert "ready" not in records[0].message and "recovered" not in records[1].message
    assert "recovered" in records[2].message
    assert calls == [START, START + timedelta(hours=1), START + timedelta(hours=2)]


@pytest.mark.parametrize("refresh_trigger", ["utc_day", "site"])
def test_empty_historical_weather_waits_for_original_cache_invalidation(refresh_trigger, caplog):
    """An empty archive remains cached for its day/site. A later genuine archive result announces recovery."""

    class ArchiveSession(Session):
        """Return valid current weather while the first optional archive has no usable hours."""

        def __init__(self):
            """Track archive calls independently of the adapter's cache key."""
            super().__init__()
            self.archive_calls = 0

        def get(self, url, **options):
            """Make only the initial archive empty, preserving valid provider shape and current hours."""
            if "archive-api" in url:
                self.archive_calls += 1
                self.overrides = {"shortwave_radiation": None} if self.archive_calls == 1 else {}
            else:
                self.overrides = {}
            return super().get(url, **options)

    async def scenario():
        """Observe the real parser, cache reuse and UTC/site invalidation without provider access."""
        session = ArchiveSession()
        source = OpenMeteoSolarSource(session)
        assert await source.read_observed(SITE, START)
        assert not source.past
        await source.read_observed(SITE, START + timedelta(hours=1))
        assert session.archive_calls == 1
        next_site = replace(SITE, longitude=7) if refresh_trigger == "site" else SITE
        next_time = START if refresh_trigger == "site" else START + timedelta(days=1)
        assert await source.read_observed(next_site, next_time)
        assert source.past
        assert session.archive_calls == 2
        await source.read_observed(next_site, next_time + timedelta(hours=1))
        assert session.archive_calls == 2

    with caplog.at_level(logging.DEBUG, logger="open_energy_planner.solar.weather"):
        asyncio.run(scenario())
    records = [
        record for record in caplog.records if "Historical solar weather refresh" in record.message
    ]
    assert [record.levelno for record in records] == [logging.WARNING, logging.INFO]
    assert "UTC day or site changes" in records[0].message
    assert "Retrying in" not in records[0].message
    assert "ready" not in records[0].message and "recovered" in records[1].message


def test_disabled_weather_producer_withdraws_health_and_schedule(tmp_path, monkeypatch):
    """Removing PV or location retracts stale availability and upcoming weather work."""
    from open_energy_planner.activities.api import EVENTS
    from open_energy_planner.activities.events import Events
    from open_energy_planner.storage.adapters.activities import ActivityStore

    runtime = SolarRuntime(make_store(tmp_path, SolarStore))
    runtime.configure(plant())

    @asynccontextmanager
    async def factory():
        """Provide accepted weather without external requests."""
        yield SimpleNamespace(read=read)

    async def read(location, now):
        """Use physical synthetic hours for the current site."""
        return weather(now)

    async def scenario():
        """Drive the actual producer through active, disabled, restored and absent-location states."""
        _clock, sleeps, resume = controlled_poll(monkeypatch)
        live = SimpleNamespace(report=SimpleNamespace(location=SITE))
        app = solar_app(runtime, live=live, weather_factory=factory)
        events = Events(ActivityStore(runtime.store.documents.engine), lambda: START)
        app[EVENTS] = events
        cursor = events.cursor
        lifecycle = app.cleanup_ctx[1](app)
        await anext(lifecycle)
        try:
            await asyncio.wait_for(sleeps.get(), 3)
            assert events.health["solar-weather"]["state"] == "available"
            assert "solar-weather:weather" in events.schedules
            assert events.schedules["solar-weather:weather"]["context"] == {
                "label": "Solar production"
            }
            weather_events = [
                row["value"]
                for row in events.changes(cursor)["events"]
                if row["topic"] == "activity"
            ]
            assert weather_events
            assert all(row["context"] == {"label": "Solar production"} for row in weather_events)
            runtime.configure(plant(count=0))
            resume.put_nowait(None)
            await asyncio.wait_for(sleeps.get(), 3)
            assert "solar-weather" not in events.health
            assert "solar-weather:weather" not in events.schedules
            runtime.configure(plant())
            resume.put_nowait(None)
            await asyncio.wait_for(sleeps.get(), 3)
            assert "solar-weather:weather" in events.schedules
            events.availability("solar-weather", "available", "Synthetic usable weather.")
            live.report = None
            resume.put_nowait(None)
            await asyncio.wait_for(sleeps.get(), 3)
            assert "solar-weather" not in events.health
            assert "solar-weather:weather" not in events.schedules
        finally:
            await lifecycle.aclose()
            await events.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["location", "removed"])
def test_changed_installation_discards_weather_without_recovery(tmp_path, monkeypatch, change):
    """Nonempty weather rejected by current inputs is skipped rather than accepted as recovery."""
    from open_energy_planner.activities.api import EVENTS
    from open_energy_planner.activities.events import Events, Outcome
    from open_energy_planner.storage.adapters.activities import ActivityStore

    runtime = SolarRuntime(make_store(tmp_path, SolarStore))
    runtime.configure(plant())
    live = SimpleNamespace(report=SimpleNamespace(location=SITE))

    async def read(location, now):
        """Change authoritative inputs during the unlocked provider request."""
        if change == "location":
            live.report = SimpleNamespace(location=replace(SITE, latitude=51))
        else:
            runtime.configure(plant(count=0))
        return weather(now)

    @asynccontextmanager
    async def factory():
        """Supply genuine typed weather for the original request only."""
        yield SimpleNamespace(read=read)

    async def scenario():
        """Run the real solar-weather lifecycle with actual SQL activity evidence."""
        _clock, sleeps, _resume = controlled_poll(monkeypatch)
        app = solar_app(runtime, live=live, weather_factory=factory)
        events = Events(ActivityStore(runtime.store.documents.engine), lambda: START)
        app[EVENTS] = events
        prior = await events.start("solar-weather", "weather", "Earlier weather request")
        await events.finish(prior, Outcome.FAILED, "Earlier failed request.")
        cursor = events.cursor
        lifecycle = app.cleanup_ctx[1](app)
        await anext(lifecycle)
        try:
            await asyncio.wait_for(sleeps.get(), 3)
            terminal = [
                row["value"]
                for row in events.changes(cursor)["events"]
                if row["topic"] == "activity" and row["value"]["state"] != "running"
            ]
            assert len(terminal) == 1
            assert terminal[0]["state"] == "skipped"
            assert not terminal[0]["usable"] and not terminal[0]["recovered"]
            assert ("solar-weather", "weather") in events.failed
            assert not runtime.weather
        finally:
            await lifecycle.aclose()
            await events.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_minute, expected_minute", [(0, 5), (10, 15), (14, 15), (15, 20)])
@pytest.mark.parametrize("named_sources", [False, True])
def test_failed_solar_forecast_exposes_its_known_retry_without_a_usable_forecast(
    tmp_path, monkeypatch, failure_minute, expected_minute, named_sources
):
    """Advertise the next admitted attempt, including a new quarter before the old key's backoff."""
    from open_energy_planner.activities.api import EVENTS
    from open_energy_planner.activities.events import Events
    from open_energy_planner.storage.adapters.activities import ActivityStore

    runtime = SolarRuntime(make_store(tmp_path, SolarStore))
    runtime.configure(plant(count=2))
    membership = (
        SimpleNamespace(
            value={
                "entities": [
                    {"entity_id": "sensor.pv0", "energy_name": "East roof"},
                    {"entity_id": "sensor.pv1", "device_name": "West roof"},
                ]
            }
        )
        if named_sources
        else None
    )
    expected_context = {"label": "East roof, West roof" if named_sources else "Solar production"}
    failed_at = START.replace(hour=12, minute=failure_minute)
    expected = START.replace(hour=12, minute=expected_minute)
    runtime.set_weather(SITE, weather(failed_at))

    async def reject(_runtime, job):
        """Reject a current admitted calculation, leaving the runtime to choose its real backoff."""
        raise OSError("Synthetic solar worker failure")

    async def scenario():
        """Observe the real forecast loop after it persists current-generation retry state."""
        clock, sleeps, _resume = controlled_poll(monkeypatch)
        clock[0] = failed_at
        monkeypatch.setattr(solar_api, "worker", reject)
        app = solar_app(runtime, membership=membership)
        events = Events(ActivityStore(runtime.store.documents.engine), lambda: clock[0])
        app[EVENTS] = events
        cursor = events.cursor
        lifecycle = app.cleanup_ctx[0](app)
        await anext(lifecycle)
        try:
            await asyncio.wait_for(sleeps.get(), 3)
            assert runtime.forecast is None
            assert runtime.retry_after == failed_at + timedelta(minutes=5)
            assert events.schedules["solar:training"]["due_at"] == expected.isoformat()
            assert events.schedules["solar:training"]["context"] == expected_context
            forecast_events = [
                row["value"]
                for row in events.changes(cursor)["events"]
                if row["topic"] == "activity"
            ]
            assert forecast_events
            assert all(row["context"] == expected_context for row in forecast_events)
            assert runtime.job(expected - timedelta(microseconds=1)) is None
            assert runtime.job(expected) is not None
        finally:
            await lifecycle.aclose()
            await events.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["unconfigured", "no_sources", "missing_weather"])
def test_solar_without_known_work_withdraws_any_previous_deadline(tmp_path, monkeypatch, state):
    """Absent prerequisites without a current retry never invent a scheduled solar calculation."""
    from open_energy_planner.activities.api import EVENTS
    from open_energy_planner.activities.events import Events
    from open_energy_planner.storage.adapters.activities import ActivityStore

    runtime = SolarRuntime(make_store(tmp_path, SolarStore))
    if state == "no_sources":
        runtime.configure(plant(count=0))
    elif state == "missing_weather":
        runtime.configure(plant())
        runtime.set_weather(SITE, ())

    async def scenario():
        """Observe the actual lifecycle retract a stale deadline without starting numerical work."""
        _clock, sleeps, _resume = controlled_poll(monkeypatch)
        app = solar_app(runtime)
        events = Events(ActivityStore(runtime.store.documents.engine), lambda: START)
        events.scheduled("solar", "training", "Old solar deadline", START + timedelta(minutes=5))
        app[EVENTS] = events
        lifecycle = app.cleanup_ctx[0](app)
        await anext(lifecycle)
        try:
            await asyncio.wait_for(sleeps.get(), 3)
            assert runtime.retry_after is None
            assert "solar:training" not in events.schedules
            assert events.running == {}
        finally:
            await lifecycle.aclose()
            await events.close()

    asyncio.run(scenario())
