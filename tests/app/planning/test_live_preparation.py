"""Prove worker ownership, cancellation draining and freshness at live snapshot installation."""

import asyncio
import threading
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.home_assistant.observation_wire import parse_report
from open_energy_planner.live_inputs import LiveInputs
from tests.ha_contract.test_observation_inputs import NOW, basis, configuration, report_body


@pytest.mark.parametrize("expire", [False, True])
@pytest.mark.parametrize("close", [False, True])
def test_preparation_keeps_loop_free_and_drains_repeated_cancellation_before_unlock(expire, close):
    """A cancelled worker retains its state lock and only installs still-fresh input on the loop."""

    async def scenario():
        """Gate preparation with real thread events and independently advance the input clock."""
        entered, release = threading.Event(), threading.Event()
        loop_thread, installed, invalidated = threading.get_ident(), [], []
        clock = [NOW]
        price_versions = ["prices-before"]

        def current_basis(*_args, **_kwargs):
            """Capture one coherent price view on the loop that owns cache publication."""
            assert threading.get_ident() == loop_thread
            return replace(basis(), price_version=price_versions[0])

        def submit(snapshot):
            """Coordinator submission must never execute on the preparation thread."""
            assert threading.get_ident() == loop_thread
            assert not planner.closed
            installed.append(snapshot)

        planner = SimpleNamespace(submit=submit, invalidate=invalidated.append, closed=False)
        repository = SimpleNamespace(invalidate_plan=lambda: None)
        live = LiveInputs(
            planner,
            repository,
            SimpleNamespace(current=current_basis),
            clock=lambda: clock[0],
        )
        live.configuration = configuration()
        live.report = parse_report(report_body(), live.configuration, NOW)

        def prepare(snapshot, _now):
            """Simulate expensive device work without blocking the event loop."""
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(3)
            return snapshot

        live.prepare_snapshot = prepare
        lock = asyncio.Lock()

        async def refresh():
            """Keep application ownership until the preparation worker has stopped."""
            async with lock:
                await live.refresh_async()

        task = asyncio.create_task(refresh())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert lock.locked()
            assert not task.done()
            price_versions[0] = "prices-after"
            if expire:
                clock[0] += timedelta(hours=2)
            planner.closed = close
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not lock.locked()
        assert bool(installed) is (not expire and not close)
        if installed:
            assert installed[0].price_version == "prices-before"
        assert (live.current_snapshot is None) is (expire or close)
        assert bool(invalidated) is (expire and not close)

    asyncio.run(scenario())
