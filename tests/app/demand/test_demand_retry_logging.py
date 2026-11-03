"""Report only backoff actually accepted for the failed demand worker generation."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner import app as app_module
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    EnergyTerm,
    TemperatureHour,
)
from open_energy_planner.demand.runtime import DemandRuntime

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


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
