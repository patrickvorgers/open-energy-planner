"""Verify plan subscription and selected sensor push with real Home Assistant and pinned TLS."""

import asyncio

from aiohttp import ClientConnectionError, ClientSession

from custom_components.open_energy_planner import coordinator as coordinator_module
from custom_components.open_energy_planner import observation_push
from custom_components.open_energy_planner.client import LocalClient
from open_energy_planner.home_assistant import plan_status_watch
from open_energy_planner.planning import PlanningService
from tests.custom_component.support.chain import TOKEN, find, running_chain


async def wait_for_projection(coordinator, ready):
    """Observe actual coordinator projections until the expected HA state appears."""
    changed = asyncio.Event()
    cancel = coordinator.subscribe(changed.set)
    try:
        async with asyncio.timeout(5):
            while not ready():
                changed.clear()
                await changed.wait()
    finally:
        cancel()


def test_idle_watch_avoids_plan_reads_and_failed_subscription_reconciles_revocation(monkeypatch):
    """Fetch the new plan once, retain valid commands during loss, and revoke via status fallback."""

    async def scenario():
        """Drive normal publication and pause while recording actual HA HTTP traffic."""
        monkeypatch.setattr(plan_status_watch, "WATCH_SECONDS", 0.04)
        monkeypatch.setattr(coordinator_module, "PLAN_WATCH_RETRY_SECONDS", 0.02)
        async with running_chain(peer_gateway=True) as (_hass, entry, url, _root):
            coordinator = entry.runtime_data
            await wait_for_projection(coordinator, lambda: coordinator.schedule.plan is not None)
            original_id = coordinator.schedule.plan.plan_id
            client = coordinator.client
            request = client.request
            requests = []
            idle = asyncio.Event()
            disconnected = asyncio.Event()
            watch_online = True
            status_online = True

            async def observe(method, path, body=None):
                """Count real requests and simulate loss of only the relevant command transports."""
                requests.append(path)
                if path.startswith("/v1/ha/plan-status/watch"):
                    if not watch_online:
                        raise ClientConnectionError()
                    if sum(item.startswith("/v1/ha/plan-status/watch") for item in requests) >= 3:
                        idle.set()
                if path == "/v1/ha/plan-status" and not status_online:
                    disconnected.set()
                    raise ClientConnectionError()
                return await request(method, path, body)

            monkeypatch.setattr(client, "request", observe)
            await asyncio.wait_for(idle.wait(), timeout=3)
            entered, release = asyncio.Event(), asyncio.Event()
            calculate = PlanningService.calculate

            async def hold_calculation(service, snapshot):
                """Leave the old plan valid while the app changes its running diagnostics."""
                entered.set()
                await release.wait()
                return await calculate(service, snapshot)

            with monkeypatch.context() as patch:
                patch.setattr(PlanningService, "calculate", hold_calculation)
                try:
                    async with ClientSession() as session:
                        admin = LocalClient(session, url, TOKEN)
                        await admin.request("POST", "/v1/replan", {"initial_kwh": 0.5})
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    requests.clear()
                    idle.clear()
                    await asyncio.wait_for(idle.wait(), timeout=3)
                    assert coordinator.schedule.plan.plan_id == original_id
                    assert not any(path.startswith("/v1/ha/plan?") for path in requests)
                    assert "/v1/ha/plan-status" not in requests
                finally:
                    release.set()
            await wait_for_projection(
                coordinator,
                lambda: (
                    coordinator.schedule.plan and coordinator.schedule.plan.plan_id != original_id
                ),
            )
            assert sum(path.startswith("/v1/ha/plan?") for path in requests) == 1
            assert "/v1/ack" in requests

            watch_online = status_online = False
            await asyncio.wait_for(disconnected.wait(), timeout=3)
            assert coordinator.schedule.plan is not None
            async with ClientSession() as session:
                admin = LocalClient(session, url, TOKEN)
                await admin.enabled(False, "fixture-pause")
            status_online = True
            await wait_for_projection(coordinator, lambda: coordinator.schedule.plan is None)
            assert not coordinator.schedule.enabled
            watch_online = True

    asyncio.run(scenario())


def test_selected_state_push_revokes_during_a_pending_transfer_and_unloads(monkeypatch):
    """Push one current selected report and withdraw unsafe authority even while HTTP is busy."""

    async def scenario():
        """Use live HA state callbacks and only synthetic state values through the actual gateway."""
        monkeypatch.setattr(plan_status_watch, "WATCH_SECONDS", 0.04)
        monkeypatch.setattr(observation_push, "MIN_PUSH_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(observation_push, "COALESCE_SECONDS", 0.01)
        async with running_chain(live=True, peer_gateway=True) as (hass, entry, url, _root):
            coordinator = entry.runtime_data
            observations = coordinator.client.observations
            reports = asyncio.Queue()
            complete_old = asyncio.Event()
            first = True

            async def transfer(report):
                """Hold the initial report so the state callback must work independently."""
                nonlocal first
                reports.put_nowait(report)
                if first:
                    first = False
                    await complete_old.wait()
                return await observations(report)

            def planning_reason():
                """Read the actual Status entity rather than an internal coordinator field."""
                attributes = find(hass, "open_energy_planner_status").attributes
                return (attributes["planning"] or {}).get("reason")

            monkeypatch.setattr(coordinator.client, "observations", transfer)
            hass.states.async_set("sensor.fixture_grid", "125", {"unit_of_measurement": "W"})
            try:
                async with ClientSession() as session:
                    admin = LocalClient(session, url, TOKEN)
                    await admin.configure_sources(
                        [
                            {
                                "key": "grid.power",
                                "entity_id": "sensor.fixture_grid",
                                "quantity": "power",
                            }
                        ],
                        None,
                    )
                first_report = await asyncio.wait_for(reports.get(), timeout=3)
                reading = first_report["readings"][0]
                assert reading["entity_id"] == "sensor.fixture_grid"
                assert (reading["value"], reading["unit"]) == (125, "W")
                assert (
                    reading["reported_at"]
                    == hass.states.get("sensor.fixture_grid").last_reported.isoformat()
                )

                assert planning_reason() == "waiting_for_measurements"
                generation = coordinator.observation_generation
                hass.states.async_set("sensor.fixture_unselected", "unavailable")
                await asyncio.sleep(0)
                assert coordinator.observation_generation == generation
                hass.states.async_set("sensor.fixture_grid", "unavailable")
                await asyncio.sleep(0)
                assert coordinator.observation_generation > generation
                assert reports.empty()
            finally:
                complete_old.set()
            next_report = await asyncio.wait_for(reports.get(), timeout=3)
            assert not next_report["readings"]
            assert next_report["issues"] == [
                {
                    "key": "grid.power",
                    "entity_id": "sensor.fixture_grid",
                    "reason": "unavailable",
                    "required": True,
                }
            ]
            await wait_for_projection(
                coordinator, lambda: planning_reason() == "required_measurement_unavailable"
            )
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert coordinator.plan_watch_task.done()
            assert coordinator.observation_task.done()
            hass.states.async_set("sensor.fixture_grid", "100", {"unit_of_measurement": "W"})
            await asyncio.sleep(0.04)
            assert reports.empty()

    asyncio.run(scenario())


def test_pending_app_pause_wakes_commands_and_rollback_restores_the_same_plan(monkeypatch):
    """Current status suspends and recovers commands through advisory signals after a failed write."""
    import threading
    from unittest.mock import AsyncMock

    import pytest
    from aiohttp import ClientResponseError

    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    async def scenario():
        """Gate a real archive write, observing an already-subscribed HA without forcing status reads."""
        subscribed = asyncio.Event()
        wait = plan_status_watch.PlanStatusWatch.wait

        async def observe_wait(watch, cursor):
            """Prove the native client reached a matching pending request before the app pause."""
            if cursor is not None and cursor == watch._cursor:
                subscribed.set()
            return await wait(watch, cursor)

        monkeypatch.setattr(plan_status_watch.PlanStatusWatch, "wait", observe_wait)
        async with running_chain(peer_gateway=True) as (hass, entry, url, _root):
            coordinator = entry.runtime_data
            await wait_for_projection(coordinator, lambda: coordinator.schedule.plan is not None)
            await asyncio.wait_for(subscribed.wait(), timeout=3)
            old = coordinator.schedule.plan
            generation = coordinator.schedule.invalidated_generation
            save = AsyncMock(wraps=coordinator.save)
            fetch = AsyncMock(wraps=coordinator.client.plan)
            monkeypatch.setattr(coordinator, "save", save)
            monkeypatch.setattr(coordinator.client, "plan", fetch)
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()
            write_plan = SqlPlanRepository._write_plan

            def fail_pause(repository, connection, plan):
                """Fail the real archive write inside its transaction after allowing suspension."""
                if repository.state["last_control_id"] == "failed-watch-pause":
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(8)
                    raise OSError("Synthetic failed app pause")
                return write_plan(repository, connection, plan)

            monkeypatch.setattr(SqlPlanRepository, "_write_plan", fail_pause)
            async with ClientSession() as session:
                admin = LocalClient(session, url, TOKEN)
                pausing = asyncio.create_task(admin.enabled(False, "failed-watch-pause"))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    await wait_for_projection(coordinator, lambda: coordinator.schedule.suspended)
                    assert not pausing.done()
                    assert coordinator.schedule.enabled
                    assert coordinator.schedule.plan is old
                    assert coordinator.schedule.invalidated_generation == generation
                    assert find(hass, "battery_power_target").state == "unavailable"
                    save.assert_not_awaited()
                finally:
                    release.set()
                    with pytest.raises(ClientResponseError) as error:
                        await pausing
                    assert error.value.status == 500
                await wait_for_projection(coordinator, lambda: not coordinator.schedule.suspended)
                assert coordinator.schedule.enabled
                assert coordinator.schedule.plan is old
                assert coordinator.schedule.invalidated_generation == generation
                assert coordinator.pending_enabled is None
                assert find(hass, "battery_power_target").state == "-1000"
                save.assert_not_awaited()
                fetch.assert_not_awaited()

                await admin.enabled(False, "committed-watch-pause")
                await wait_for_projection(coordinator, lambda: not coordinator.schedule.enabled)
                assert coordinator.schedule.plan is None
                assert coordinator.schedule.invalidated_generation == old.generated_at
                assert find(hass, "battery_power_target").state == "unavailable"
                save.assert_awaited_once()
                fetch.assert_not_awaited()

    asyncio.run(scenario())
