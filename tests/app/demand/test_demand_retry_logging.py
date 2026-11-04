"""Report only backoff actually accepted for the failed demand worker generation."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner import app as app_module
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    EnergyTerm,
    TemperatureHour,
)
from open_energy_planner.demand.runtime import DemandRuntime
from open_energy_planner.installation.location import SiteLocation

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def test_empty_temperature_evidence_waits_for_usable_recovery(tmp_path, monkeypatch, caplog):
    """Empty successful reads stay unavailable until usable hours arrive, with unchanged hourly refresh."""
    runtimes, live_inputs, calls = [], [], []
    original_live_inputs = app_module.LiveInputs
    location = SiteLocation(52, 6, "Europe/Amsterdam")
    temperatures = (TemperatureHour(NOW, 8, NOW),)

    def runtime_factory(store, **options):
        """Configure a real demand runtime whose accepted weather can be inspected."""
        runtime = DemandRuntime(store, **options)
        runtime.configure(
            DemandConfiguration("Europe/Amsterdam", 3650, (EnergyTerm("sensor.house", 1),))
        )
        runtimes.append(runtime)
        return runtime

    def live_factory(*args):
        """Retain the real live-input owner without starting unrelated workers."""
        live = original_live_inputs(*args)
        live_inputs.append(live)
        return live

    class Weather:
        """Supply two empty provider results followed by one usable temperature hour."""

        async def read(self, site, now):
            """Observe actual refresh requests without contacting a weather service."""
            assert site == location
            calls.append(now)
            return () if len(calls) < 3 else temperatures

    @asynccontextmanager
    async def factory():
        """Borrow the deterministic provider for the real temperature lifecycle."""
        yield Weather()

    async def scenario():
        """Release each actual scheduled wait and inspect evidence before cleanup."""
        sleeps, resume = asyncio.Queue(), asyncio.Queue()

        async def sleep(seconds):
            """Capture the actual next refresh interval without waiting an hour."""
            sleeps.put_nowait(seconds)
            await resume.get()

        monkeypatch.setattr(app_module, "DemandRuntime", runtime_factory)
        monkeypatch.setattr(app_module, "LiveInputs", live_factory)
        monkeypatch.setattr(
            app_module,
            "asyncio",
            SimpleNamespace(
                create_task=asyncio.create_task,
                sleep=sleep,
                Lock=asyncio.Lock,
                CancelledError=asyncio.CancelledError,
            ),
        )
        application = app_module.create_app(
            tmp_path,
            "temperature-test-token-0123456789",
            source="home_assistant",
            database_url=None,
            weather_factory=factory,
        )
        live_inputs[0].report = SimpleNamespace(location=location)
        lifecycle = next(
            context
            for context in application.cleanup_ctx
            if context.__name__ == "weather_lifecycle"
        )(application)
        await anext(lifecycle)
        try:
            for attempt in range(3):
                assert await asyncio.wait_for(sleeps.get(), 3) == 3600
                assert len(calls) == attempt + 1
                assert runtimes[0].temperatures == (() if attempt < 2 else temperatures)
                if attempt < 2:
                    resume.put_nowait(None)
        finally:
            await lifecycle.aclose()

    with caplog.at_level(logging.DEBUG, logger=app_module.__name__):
        asyncio.run(scenario())
    records = [
        record for record in caplog.records if "Temperature weather refresh" in record.message
    ]
    assert [record.levelno for record in records] == [logging.WARNING, logging.DEBUG, logging.INFO]
    assert "no usable temperature hours" in records[0].message
    assert "Retrying in 60 minutes." in records[0].message
    assert all(
        "ready" not in record.message and "recovered" not in record.message
        for record in records[:2]
    )
    assert "recovered" in records[2].message
    assert "Received 1 temperature hours" in records[2].message


@pytest.mark.parametrize("failure", ["current", "stale", "before_capture"])
def test_demand_worker_reports_only_current_generation_backoff(
    tmp_path, monkeypatch, caplog, failure
):
    """An older three-minute deadline must not masquerade as retry policy for a new failed job.

    A current failure records five minutes of backoff. If weather changes while
    its immutable job runs, or capture itself fails, the runtime retains its old
    deadline but the worker checks again at the ordinary five-second poll.
    """
    runtimes, jobs = [], []
    original_job = DemandRuntime.job

    class Clock(datetime):
        """Keep generation identity and retry arithmetic independent of wall-clock test duration."""

        @classmethod
        def now(cls, tz=None):
            """Supply the independently fixed test instant used by the actual worker."""
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    def runtime_factory(store, **options):
        """Use the real runtime with a retained failure for an older weather generation."""
        runtime = DemandRuntime(store, **options)
        runtime.configure(
            DemandConfiguration("Europe/Amsterdam", 3650, (EnergyTerm("sensor.house", 1),))
        )
        previous_attempt = NOW - timedelta(minutes=2)
        assert runtime.failed(runtime.key(previous_attempt), previous_attempt)
        assert runtime.retry_after == NOW + timedelta(minutes=3)
        runtime.weather(())
        runtimes.append(runtime)
        return runtime

    def capture_job(runtime, now):
        """Fail before a job exists in the capture case, otherwise capture the real immutable inputs."""
        if failure == "before_capture":
            raise OSError("Controlled demand capture failure")
        return original_job(runtime, now)

    def fail_build(runtime, job):
        """Represent a training failure after optional acceptance of newer weather inputs."""
        jobs.append(job)
        if failure == "stale":
            runtime.weather((TemperatureHour(NOW, 10, NOW),))
        raise OSError("Controlled demand build failure")

    monkeypatch.setattr(app_module, "datetime", Clock)
    monkeypatch.setattr(app_module, "DemandRuntime", runtime_factory)
    monkeypatch.setattr(DemandRuntime, "job", capture_job)
    monkeypatch.setattr(DemandRuntime, "build", fail_build)

    async def scenario():
        """Run the real demand worker and archive ownership without starting unrelated app workers."""
        application = app_module.create_app(
            tmp_path,
            "demand-retry-test-token-0123456789",
            source="home_assistant",
            database_url=None,
        )
        archive = next(
            context
            for context in application.cleanup_ctx
            if context.__name__ == "archive_lifecycle"
        )(application)
        worker = next(
            context for context in application.cleanup_ctx if context.__name__ == "demand_lifecycle"
        )(application)
        await anext(archive)
        try:
            await anext(worker)
            try:
                async with asyncio.timeout(5):
                    while not any(
                        message.startswith("Household demand forecast update failed")
                        for message in caplog.messages
                    ):
                        await asyncio.sleep(0.01)
            finally:
                await worker.aclose()
        finally:
            await archive.aclose()

    with caplog.at_level(logging.WARNING, logger=app_module.__name__):
        asyncio.run(scenario())
    failures = [
        message
        for message in caplog.messages
        if message.startswith("Household demand forecast update failed")
    ]
    assert len(failures) == 1
    runtime = runtimes[0]
    if failure == "current":
        assert "Retrying in 5 minutes." in failures[0]
        assert runtime.retry_after == NOW + timedelta(minutes=5)
        assert runtime.failed_key == jobs[0][0]
        assert original_job(runtime, NOW + timedelta(seconds=5)) is None
    else:
        assert "Retrying in 5 seconds." in failures[0]
        assert runtime.retry_after == NOW + timedelta(minutes=3)
        assert original_job(runtime, NOW + timedelta(seconds=5)) is not None
        if failure == "stale":
            assert jobs[0][0] != runtime.key(NOW)
        else:
            assert not jobs
