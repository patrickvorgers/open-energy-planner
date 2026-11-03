"""Verify remote native solver results and invalid-candidate retention through real HA."""

import asyncio
from datetime import datetime, timedelta

import pytest

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)

from aiohttp import ClientSession

from tests.custom_component.support.chain import (
    TOKEN,
    find,
    remote_replan,
    remote_reset,
    running_chain,
)


def test_remote_real_solver_plan_reaches_actual_ha_entities():
    """Buy 1 kWh at 0.12 and sell at 0.40 with unit efficiency, no fees or terminal value. Cost is -0.28 EUR and quarter-hour charging is -4000 W."""

    async def scenario():
        """Run the remote native solver and compare authenticated app evidence with accepted HA commands."""
        url = await remote_reset()
        async with running_chain(remote_url=url) as (hass, entry, url, _root):
            assert find(hass, "battery_power_target").state == "-4000"
            assert find(hass, "battery_control_mode").state == "fixed_power"
            assert find(hass, "open_energy_planner_status").state == "active"
            plan = entry.runtime_data.schedule.document
            assert (
                datetime.fromisoformat(plan["steps"][-1]["end"])
                - datetime.fromisoformat(plan["steps"][0]["start"])
            ) == timedelta(hours=48)
            assert "independent_costs" not in plan
            async with (
                ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session,
                session.get(url + "/v1/snapshot") as response,
            ):
                original = (await response.json())["plan"]
            assert original["plan_id"] == plan["plan_id"]
            assert original["independent_costs"]["objective"] == pytest.approx(-0.28)
            assert original["input_sha256"] == plan["input_sha256"]
            assert plan["input_sha256"]

    asyncio.run(scenario())


def test_remote_changed_soc_produces_and_activates_a_new_validated_plan():
    """Start with 0.5 kWh stored. Charge only the remaining half, giving -2000 W and 0.40-0.5*0.12=0.34 profit."""

    async def scenario():
        """Change the remote initial charge and inspect the new plan identity, target and independent cost."""
        url = await remote_reset()
        async with running_chain(remote_url=url) as (hass, entry, url, _root):
            old = entry.runtime_data.schedule.plan.plan_id
            data = await remote_replan(url, initial_kwh=0.5)
            await entry.runtime_data.sync()
            assert entry.runtime_data.schedule.plan.plan_id != old
            assert find(hass, "battery_power_target").state == "-2000"
            assert data["plan"]["independent_costs"]["objective"] == pytest.approx(-0.34)

    asyncio.run(scenario())


def test_remote_infeasible_forecast_never_replaces_the_previous_plan():
    """Retain the previous plan when 1.25 kWh in 0.25 h needs 5000 W.

    The grid allows 4000 W and the initially empty battery cannot supply the
    remaining 1000 W, so no feasible replacement exists.
    """

    async def scenario():
        """Request load above the grid limit with an empty battery and retain the previously accepted plan."""
        url = await remote_reset()
        async with running_chain(remote_url=url) as (hass, entry, url, _root):
            old = entry.runtime_data.schedule.plan.plan_id
            data = await remote_replan(url, initial_kwh=0, first_load_kwh=1.25)
            assert data["planning"]["status"] == "failed"
            assert data["planning"]["reason"] == "infeasible"
            await entry.runtime_data.sync()
            assert entry.runtime_data.schedule.plan.plan_id == old
            assert find(hass, "battery_power_target").state == "-4000"

    asyncio.run(scenario())


def test_remote_pause_resume_waits_for_a_new_solver_plan():
    """Pause/resume the real remote solver chain. Keep old commands inactive until a new validated plan is accepted."""

    async def scenario():
        """Pause and resume the remote chain while keeping old commands inactive until a fresh solve completes."""
        url = await remote_reset()
        async with running_chain(remote_url=url) as (hass, entry, url, _root):
            coordinator_item = entry.runtime_data
            old = coordinator_item.schedule.plan.plan_id
            await coordinator_item.set_enabled(False)
            assert find(hass, "battery_power_target").state == "unavailable"
            await coordinator_item.set_enabled(True)
            # The resume request returned before worker completion. An old plan cannot reactivate.
            assert coordinator_item.schedule.plan.plan_id == old
            assert coordinator_item.projection.command is None
            for _ in range(300):
                await coordinator_item.sync()
                if coordinator_item.projection.status == "active":
                    break
                await asyncio.sleep(0.05)
            assert coordinator_item.schedule.plan.plan_id != old
            assert find(hass, "battery_power_target").state == "-4000"

    asyncio.run(scenario())
