"""Exercise persisted control receipts through the actual app, client and HA coordinator."""

import asyncio
import threading

import pytest

from tests.custom_component.support.chain import find, running_chain


def test_pause_confirmation_does_not_fetch_history_and_periodic_sync_still_does(monkeypatch):
    """Pause succeeds while two snapshots wait on history. The next normal sync resumes full processing."""
    from open_energy_planner.savings.runtime import SavingsRuntime

    async def scenario():
        """Gate the real archive owner rather than returning a fabricated status snapshot."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            entered, release = threading.Event(), threading.Event()
            readers, readers_lock = [], threading.Lock()
            original = SavingsRuntime.request

            def read_history(runtime, *arguments):
                """Retain every historical reader until the independently confirmed pause returns."""
                with readers_lock:
                    readers.append(True)
                    if len(readers) == 2:
                        entered.set()
                assert release.wait(4)
                return original(runtime, *arguments)

            with monkeypatch.context() as patch:
                patch.setattr(SavingsRuntime, "request", read_history)
                readers_pending = [
                    asyncio.create_task(coordinator.client.snapshot()) for _ in range(2)
                ]
                try:
                    assert await asyncio.to_thread(entered.wait, 2)
                    async with asyncio.timeout(1):
                        await coordinator.set_enabled(False)
                    assert coordinator.pending_enabled is None
                    assert not coordinator.schedule.enabled
                    assert find(hass, "battery_power_target").state == "unavailable"
                    assert len(readers) == 2
                    assert all(not task.done() for task in readers_pending)
                finally:
                    release.set()
                    snapshots = await asyncio.gather(*readers_pending)
                assert all(snapshot["enabled"] is False for snapshot in snapshots)
            requests = []
            request = coordinator.client.request

            async def record(method, path, body=None):
                """Observe the normal client exchange without substituting any response."""
                requests.append((method, path))
                return await request(method, path, body)

            monkeypatch.setattr(coordinator.client, "request", record)
            await coordinator.sync()
            assert ("GET", "/v1/snapshot") in requests
            assert coordinator.issue is None
            assert coordinator.pending_enabled is None

    asyncio.run(scenario())


def test_invalid_receipt_retains_pending_intent_until_a_valid_retry(monkeypatch):
    """A malformed real HTTP receipt cannot confirm local intent. The same intent succeeds on retry."""

    async def scenario():
        """Corrupt only the receipt after the native transport and release checks have run."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            request = coordinator.client.request

            async def unconfirmed(method, path, body=None):
                """Preserve the actual app write but reject its confirmation as stale evidence."""
                receipt = await request(method, path, body)
                if path == "/v1/enabled":
                    receipt["request_id"] = "stale-control"
                return receipt

            with monkeypatch.context() as patch:
                patch.setattr(coordinator.client, "request", unconfirmed)
                await coordinator.set_enabled(False)
            assert coordinator.pending_enabled is False
            assert coordinator.issue == "synchronization_failed"
            assert find(hass, "battery_power_target").state == "unavailable"
            await coordinator.sync()
            assert coordinator.pending_enabled is None
            await coordinator.sync()
            assert coordinator.issue is None
            assert not coordinator.schedule.enabled

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["newer-intent", "closed"])
def test_delayed_receipt_cannot_clear_newer_or_closed_intent(monkeypatch, change):
    """An in-flight valid acknowledgement loses authority when a newer intent or shutdown supersedes it."""
    from homeassistant.util import dt as dt_util

    async def scenario():
        """Delay a real persisted receipt at the client boundary while HA changes local ownership."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            entered, release = asyncio.Event(), asyncio.Event()
            request = coordinator.client.request

            async def delayed(method, path, body=None):
                """Keep transport validation intact and gate only delivery of a confirmed control."""
                receipt = await request(method, path, body)
                if path == "/v1/enabled":
                    entered.set()
                    await release.wait()
                return receipt

            monkeypatch.setattr(coordinator.client, "request", delayed)
            pending = asyncio.create_task(coordinator.set_enabled(False))
            closing = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if change == "newer-intent":
                    coordinator.schedule.resume(dt_util.utcnow())
                    coordinator.pending_enabled = True
                    coordinator.refresh()
                    await coordinator.save()
                else:
                    closing = asyncio.create_task(coordinator.close())
                    async with asyncio.timeout(2):
                        while not coordinator.closed:
                            await asyncio.sleep(0)
            finally:
                release.set()
                await pending
                if closing:
                    await closing
            assert coordinator.pending_enabled is (change == "newer-intent")
            assert find(hass, "battery_power_target").state == "unavailable"
            if change == "newer-intent":
                await coordinator.sync()
                assert coordinator.pending_enabled is None
                assert find(hass, "battery_power_target").state == "unavailable"
                await coordinator.sync()
                assert coordinator.schedule.plan.generated_at > coordinator.schedule.resume_after
                assert find(hass, "battery_power_target").state != "unavailable"

    asyncio.run(scenario())
