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
from tests.app.solar.test_solar import SITE, START, plant, weather
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


def solar_app(runtime, *, live=None, weather_factory=None):
    """Register the actual solar lifecycle while leaving unrelated HTTP composition inactive."""
    app = web.Application()
    app[NOTIFICATIONS] = NotificationCenter()
    solar_api.register(app, runtime, live, asyncio.Lock(), None, None, weather_factory, None)
    return app


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
        assert await source.read(SITE, START)
        assert not source.past
        assert source.past_key == (SITE.latitude, SITE.longitude, START.date())
        await source.read(SITE, START + timedelta(hours=1))
        assert session.archive_calls == 1
        next_site = replace(SITE, longitude=7) if refresh_trigger == "site" else SITE
        next_time = START if refresh_trigger == "site" else START + timedelta(days=1)
        assert await source.read(next_site, next_time)
        assert source.past
        assert session.archive_calls == 2
        await source.read(next_site, next_time + timedelta(hours=1))
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
