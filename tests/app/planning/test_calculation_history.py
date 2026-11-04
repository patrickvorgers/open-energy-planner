"""Calculation telemetry stays optional while normal publication and failure handling remain authoritative."""

import asyncio
from datetime import timedelta

from open_energy_planner.planning import PlanningCoordinator
from open_energy_planner.synthetic import capture
from tests.app.planning.test_planning_pipeline import NOW, service


def test_history_failure_cannot_block_validated_publication():
    """A broken history writer is invoked after publication and cannot change ready status or commands."""

    async def scenario():
        """Run real independently checked planning with an unavailable observation store."""
        published, observed = [], []

        async def publish(document):
            """Collect the independently validated document without physical execution."""
            published.append(document)

        async def broken_history(*arguments):
            """Record the attempted evidence before injecting a storage outage."""
            observed.append(arguments)
            raise OSError("Simulated history outage")

        coordinator = PlanningCoordinator(service(), publish, record_calculation=broken_history)
        coordinator.submit(capture(NOW))
        await coordinator.wait()
        assert coordinator.status == "ready"
        assert len(published) == len(observed) == 1
        assert observed[0][-1] == "published"
        assert observed[0][1].result.progress
        await coordinator.close()

    asyncio.run(scenario())


def test_old_input_attempt_records_absent_progress_without_calling_solver():
    """An admission rejection is retained with no fabricated solver duration or first candidate."""

    async def scenario():
        """Capture the failed attempt through the same optional observer as successful calculations."""
        observed = []

        async def publish(_document):
            """Fail if invalid old input unexpectedly reaches publication."""
            raise AssertionError("Invalid input was published")

        async def record(*arguments):
            """Retain the completed attempt for independent assertions."""
            observed.append(arguments)

        coordinator = PlanningCoordinator(
            service(clock=lambda: NOW + timedelta(minutes=10)), publish, record_calculation=record
        )
        coordinator.submit(capture(NOW))
        await coordinator.wait()
        assert len(observed) == 1
        assert observed[0][-1] == "stale_input"
        assert observed[0][1].result is None

    asyncio.run(scenario())


def test_stalled_history_writer_allows_next_plan_and_drains_on_shutdown(monkeypatch):
    """A blocked optional write cannot delay a second publication, and graceful shutdown waits for evidence."""
    from dataclasses import replace
    from threading import Event

    from aiohttp import web

    from open_energy_planner.optimization.performance import CalculationHistory, register

    entered, release = Event(), Event()
    written, published = [], []

    def stalled_write(_history, observation):
        """Pause persistence behind an explicit test gate without involving a database or native solver."""
        entered.set()
        assert release.wait(5)
        written.append(observation)

    monkeypatch.setattr(CalculationHistory, "write", stalled_write)

    async def scenario():
        """Exercise the production queued callback, coordinator and aiohttp shutdown lifecycle."""
        app = web.Application()
        record = register(app, None, lambda: "UTC")
        runner = web.AppRunner(app)
        await runner.setup()

        async def publish(document):
            """Observe complete validated plans without touching physical devices."""
            published.append(document)

        coordinator = PlanningCoordinator(service(), publish, record_calculation=record)
        cleanup = None
        try:
            original = capture(NOW)
            coordinator.submit(original)
            await coordinator.wait()
            assert await asyncio.to_thread(entered.wait, 2)
            coordinator.submit(replace(original, snapshot_id="next-calculation"))
            await asyncio.wait_for(coordinator.wait(), 1)
            assert len(published) == 2 and not written
            await coordinator.close()
            cleanup = asyncio.create_task(runner.cleanup())
            await asyncio.sleep(0)
            assert not cleanup.done()
        finally:
            release.set()
            await coordinator.close()
            await asyncio.wait_for(cleanup if cleanup else runner.cleanup(), 2)
        assert len(written) == 2
        assert written[1]["snapshot_id"] == "next-calculation"

    asyncio.run(scenario())


def test_history_worker_survives_write_failure_and_bounds_pending_memory(monkeypatch):
    """Thirty-two detached observations fit. Overload is reported and a failed first write cannot stop later ones."""
    from aiohttp import web

    from open_energy_planner.optimization.performance import CalculationHistory, register

    attempts, written = [], []

    def flaky_write(_history, observation):
        """Fail one write, then retain the following detached values in submission order."""
        attempts.append(observation)
        if len(attempts) == 1:
            raise OSError("Simulated history outage")
        written.append(observation)

    monkeypatch.setattr(CalculationHistory, "write", flaky_write)

    async def scenario():
        """Fill the queue without yielding to its worker, then drain through normal cleanup."""
        app = web.Application()
        record = register(app, None, lambda: "UTC")
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            snapshot = capture(NOW)
            for _ in range(34):
                await record(snapshot, None, NOW, NOW, "planning_error")
        finally:
            await runner.cleanup()
        assert len(attempts) == 32
        assert len(written) == 31
        assert all("problem" not in observation for observation in attempts)

    asyncio.run(scenario())
