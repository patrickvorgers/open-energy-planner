"""Exercise persisted control receipts through the actual app, client and HA coordinator."""

import asyncio
import threading

import pytest

from tests.custom_component.support.chain import find, running_chain


def test_pause_confirmation_does_not_fetch_history_and_periodic_sync_still_does(monkeypatch):
    """Pause succeeds while two HA data requests wait on history."""
    from open_energy_planner.savings.runtime import SavingsRuntime

    async def scenario():
        """Gate the real archive owner rather than returning a fabricated status snapshot."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
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
                    asyncio.create_task(coordinator.client.service_requests()) for _ in range(2)
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
                    metadata = await asyncio.gather(*readers_pending)
                assert all("plan" not in result for result in metadata)
                assert (await coordinator.client.plan_status())["enabled"] is False
            requests = []
            request = coordinator.client.request

            async def record(method, path, body=None):
                """Observe the normal client exchange without substituting any response."""
                requests.append((method, path))
                return await request(method, path, body)

            monkeypatch.setattr(coordinator.client, "request", record)
            await coordinator.sync()
            assert ("GET", "/v1/ha/requests") in requests
            assert coordinator.issue is None
            assert coordinator.pending_enabled is None

    asyncio.run(scenario())


def test_local_pause_withdraws_commands_while_existing_sync_holds_its_lock(monkeypatch):
    """A blocked sync delays the control receipt, while local pause immediately withdraws commands."""

    async def scenario():
        """Block a plan status read while observing the independent local pause before release."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            entered, release = asyncio.Event(), asyncio.Event()
            requests = []
            request = coordinator.client.request

            async def record(method, path, body=None):
                """Record real client requests so a local pause cannot masquerade as an HTTP receipt."""
                requests.append((method, path))
                return await request(method, path, body)

            status = coordinator.client.plan_status

            async def delayed_status():
                """Hold the first plan status receipt while the user pauses locally."""
                response = await status()
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                return response

            monkeypatch.setattr(coordinator.client, "request", record)
            with monkeypatch.context() as patch:
                patch.setattr(coordinator.client, "plan_status", delayed_status)
                syncing = asyncio.create_task(coordinator.sync_plan())
                pausing = None
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    assert coordinator.lock.locked()
                    pausing = asyncio.create_task(coordinator.set_enabled(False))
                    async with asyncio.timeout(1):
                        while coordinator.pending_enabled is not False:
                            await asyncio.sleep(0)
                    assert find(hass, "battery_power_target").state == "unavailable"
                    assert not coordinator.schedule.enabled
                    assert not syncing.done() and not pausing.done()
                    assert ("PUT", "/v1/enabled") not in requests
                finally:
                    release.set()
                    await asyncio.gather(syncing, *([pausing] if pausing else []))
            assert requests.count(("PUT", "/v1/enabled")) == 1
            assert coordinator.pending_enabled is None
            assert find(hass, "battery_power_target").state == "unavailable"
            await coordinator.sync()
            assert ("GET", "/v1/ha/requests") in requests
            assert coordinator.issue is None
            assert not coordinator.schedule.enabled

    asyncio.run(scenario())


def test_current_capture_does_not_hold_new_plan_reconciliation(monkeypatch):
    """Accept a new published plan while the independent observation upload is waiting."""

    async def scenario():
        """Gate the event-driven capture while retaining real status, command and ACK traffic."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            hass.states.async_set("sensor.grid_fixture", "0", {"unit_of_measurement": "W"})
            original = coordinator.schedule.plan.plan_id
            revision = coordinator.schedule.config_revision
            coordinator._configure_measurements(
                {
                    "revision": revision,
                    "bindings": [
                        {
                            "key": "grid.power",
                            "entity_id": "sensor.grid_fixture",
                            "quantity": "power",
                            "required": True,
                            "multiplier": 1,
                            "maximum_age_s": 300,
                            "timestamp_attribute": None,
                        }
                    ],
                }
            )
            status = coordinator.client.plan_status
            entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def measured_status():
                """Require the installed live binding with the actual synthetic publication."""
                return {**await status(), "measurements_required": True}

            async def slow_upload(report):
                """Hold a capture in flight while retaining its source and revision."""
                assert report["config_revision"] == revision and report["readings"]
                entered.set()
                await release.wait()
                completed.set()
                return {"accepted": True, "config_revision": revision}

            monkeypatch.setattr(coordinator.client, "plan_status", measured_status)
            monkeypatch.setattr(coordinator.client, "observations", slow_upload)
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await coordinator.client.enabled(True, "observation-overlap")
                async with asyncio.timeout(2):
                    while (await status())["plan_id"] in (None, original):
                        await asyncio.sleep(0.01)
                    assert await coordinator.sync_plan()
                assert coordinator.schedule.plan.plan_id != original
                assert coordinator.projection.command is not None
                assert not completed.is_set()
            finally:
                release.set()
                await asyncio.wait_for(completed.wait(), timeout=2)

    asyncio.run(scenario())


def test_failed_app_pause_withdraws_then_restores_existing_commands(monkeypatch):
    """A live app pause holds command authority without writing uncommitted intent to HA."""
    from unittest.mock import AsyncMock

    from aiohttp import ClientResponseError

    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    async def scenario():
        """Hold a real archive transaction, force rollback and then commit an independent pause."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            old = coordinator.schedule.plan
            generation = coordinator.schedule.invalidated_generation
            save = AsyncMock(wraps=coordinator.save)
            monkeypatch.setattr(coordinator, "save", save)
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()
            write_plan = SqlPlanRepository._write_plan

            def fail_pause(repository, connection, plan):
                """Fail inside the real save so SQL and in-memory state both roll back."""
                if repository.state["last_control_id"] == "failed-app-pause":
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(5)
                    raise OSError("Synthetic failed app pause")
                return write_plan(repository, connection, plan)

            monkeypatch.setattr(SqlPlanRepository, "_write_plan", fail_pause)
            pause = asyncio.create_task(coordinator.client.enabled(False, "failed-app-pause"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert await coordinator.sync_plan()
                assert coordinator.schedule.enabled
                assert coordinator.schedule.suspended
                assert find(hass, "battery_power_target").state == "unavailable"
                save.assert_not_awaited()
            finally:
                release.set()
                with pytest.raises(ClientResponseError) as failure:
                    await pause
                assert failure.value.status == 500
            assert await coordinator.sync_plan()
            assert coordinator.schedule.enabled and not coordinator.schedule.suspended
            assert coordinator.schedule.plan is old
            assert coordinator.schedule.invalidated_generation == generation
            assert coordinator.pending_enabled is None
            assert find(hass, "battery_power_target").state == "-1000"
            save.assert_not_awaited()

            await coordinator.client.enabled(False, "committed-app-pause")
            assert await coordinator.sync_plan()
            assert not coordinator.schedule.enabled
            assert coordinator.schedule.plan is None
            assert coordinator.schedule.invalidated_generation == old.generated_at
            assert find(hass, "battery_power_target").state == "unavailable"
            save.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("result", ["lost-reply", "pause", "missing-receipt"])
def test_candidate_requires_current_authority_after_acknowledgement(monkeypatch, result):
    """Recover a recorded acknowledgement or reject commands withdrawn while its reply was in flight."""
    from unittest.mock import AsyncMock

    async def scenario():
        """Use the real app publication, ACK and pause with a gate after the ACK commits."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            previous = coordinator.schedule.plan
            await coordinator.client.enabled(True, "replace-plan-for-receipt")
            async with asyncio.timeout(3):
                while (await coordinator.client.plan_status())["plan_id"] in (
                    None,
                    previous.plan_id,
                ):
                    await asyncio.sleep(0.01)
            candidate_id = (await coordinator.client.plan_status())["plan_id"]
            acknowledge = coordinator.client.acknowledge
            read_status = coordinator.client.plan_status
            entered, release = asyncio.Event(), asyncio.Event()

            async def gated_acknowledgement(plan_id):
                """Preserve actual ACK persistence while delaying or losing delivery."""
                receipt = await acknowledge(plan_id)
                entered.set()
                await release.wait()
                if result == "lost-reply":
                    raise TimeoutError
                return receipt

            async def status_without_receipt():
                """Simulate a newer status that no longer confirms the candidate's acknowledgement."""
                return {**await read_status(), "acknowledged_plan": None}

            gated = AsyncMock(side_effect=gated_acknowledgement)
            monkeypatch.setattr(coordinator.client, "acknowledge", gated)
            loading = asyncio.create_task(coordinator.sync_plan())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                status = await coordinator.client.plan_status()
                assert status["acknowledged_plan"] == candidate_id
                assert coordinator.schedule.plan is previous
                if result == "pause":
                    await coordinator.client.enabled(False, "pause-after-acknowledgement")
                elif result == "missing-receipt":
                    monkeypatch.setattr(coordinator.client, "plan_status", status_without_receipt)
            finally:
                release.set()
                assert not await loading
            if result == "pause":
                assert coordinator.schedule.plan is None
                assert find(hass, "battery_power_target").state == "unavailable"
            else:
                assert coordinator.schedule.plan is previous
                monkeypatch.setattr(coordinator.client, "plan_status", read_status)
                assert await coordinator.sync_plan()
                assert coordinator.schedule.plan.plan_id == candidate_id
                assert coordinator.projection.command is not None
            gated.assert_awaited_once_with(candidate_id)

    asyncio.run(scenario())


@pytest.mark.parametrize("result", ["closed", "superseded", "mismatch"])
def test_late_observation_result_respects_coordinator_and_identity_ownership(monkeypatch, result):
    """Discard closed or obsolete transfers while revoking commands on a genuine current mismatch."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from custom_components.open_energy_planner.client import (
        InstallationMismatchError,
        SupersededInstallationExchangeError,
    )

    async def scenario():
        """Gate only the observation reply and inspect the real coordinator's command state."""
        async with running_chain() as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            original = coordinator.schedule.plan
            revision = coordinator.schedule.config_revision
            coordinator.observation_bridge = SimpleNamespace(
                revision=revision, capture=lambda *_args, **_kwargs: (True, {})
            )
            entered, release = asyncio.Event(), asyncio.Event()
            notify = Mock()
            monkeypatch.setattr(coordinator, "_update_snapshot_notification", notify)

            async def delayed(_report):
                """Simulate each late completion after the observation lane begins."""
                entered.set()
                await release.wait()
                if result == "superseded":
                    raise SupersededInstallationExchangeError("Obsolete connection epoch")
                if result == "mismatch":
                    raise InstallationMismatchError("Current connection identity changed")
                return {"accepted": True, "config_revision": revision, "observations": {}}

            monkeypatch.setattr(coordinator.client, "observations", delayed)
            pending = asyncio.create_task(coordinator.sync_observations())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if result == "closed":
                    coordinator.closed = True
            finally:
                release.set()
                try:
                    assert not await pending
                finally:
                    coordinator.closed = False
            notify.assert_not_called()
            assert coordinator.observations is None
            if result == "mismatch":
                assert coordinator.schedule.plan is None
                assert coordinator.issue == "installation_identity_changed"
            else:
                assert coordinator.schedule.plan is original
                assert coordinator.issue is None

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
