"""Run early real HA contracts for setup, history conservation and command invalidation."""

import asyncio
from datetime import datetime, timedelta

import pytest

from tests.database.support.archive import list_documents, read_document

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)

from aiohttp import ClientSession
from homeassistant.util import dt as dt_util

from custom_components.open_energy_planner.const import DOMAIN
from tests.custom_component.support.chain import (
    TOKEN,
    app_write,
    configure_energy,
    find,
    running_chain,
)


def test_changed_revision_in_observation_response_revokes_old_commands_immediately(monkeypatch):
    """Change revision during observation upload. Reject the reply and make old command entities unavailable immediately."""
    from copy import deepcopy

    async def scenario():
        """Return a changed revision during observation upload and inspect immediate command revocation."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
            state = await coordinator.client.snapshot()
            state["measurement_config"] = {
                "revision": "demo-1",
                "bindings": [
                    {
                        "key": "grid.power",
                        "entity_id": "sensor.grid",
                        "quantity": "power",
                        "required": True,
                        "multiplier": 1,
                        "maximum_age_s": 300,
                        "timestamp_attribute": None,
                    }
                ],
            }

            async def snapshot():
                """Return the fixed pre-upload snapshot without changing user intent."""
                return deepcopy(state)

            async def upload(_report):
                """Simulate a changed configuration revision in the observation transport reply."""
                return {
                    **state,
                    "config_revision": "changed-while-uploading",
                    "plan_invalidated": True,
                }

            monkeypatch.setattr(coordinator.client, "snapshot", snapshot)
            monkeypatch.setattr(coordinator.client, "observations", upload)
            await coordinator.sync()
            assert coordinator.schedule.plan is None
            assert coordinator.issue == "synchronization_failed"
            assert find(hass, "battery_power_target").state == "unavailable"

    asyncio.run(scenario())


def test_app_demand_setup_reads_recorder_and_keeps_startup_without_history():
    """Configure one household counter and annual estimate. Forecast immediately, then learn measured 0.4 kWh/hour through real Recorder."""
    import json

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics

    async def scenario():
        """Use actual app configuration, Recorder and local API without helpers, external weather or physical control."""
        async with running_chain(live=True, recorder=True) as (hass, entry, url, root):
            now = dt_util.utcnow()
            end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)
            start = end - timedelta(days=7, hours=1)
            async_import_statistics(
                hass,
                {
                    "statistic_id": "sensor.home_energy",
                    "source": "recorder",
                    "name": None,
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "unit_of_measurement": "kWh",
                    "unit_class": "energy",
                },
                [
                    {
                        "start": start + timedelta(hours=index_item),
                        "sum": 100 + index_item * 0.4,
                        "state": 100 + index_item * 0.4,
                    }
                    for index_item in range(7 * 24 + 1)
                ],
            )
            await get_instance(hass).async_block_till_done()
            hass.states.async_set("sensor.home_energy", "200", {"unit_of_measurement": "kWh"})
            hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
            await configure_energy(
                hass,
                entry,
                [
                    {
                        "type": "grid",
                        "stat_energy_from": "sensor.home_energy",
                        "stat_rate": "sensor.grid",
                    }
                ],
            )
            await app_write(
                url,
                "sources",
                {
                    "bindings": [
                        {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"}
                    ]
                },
            )
            await app_write(
                url,
                "demand",
                {
                    "timezone": hass.config.time_zone,
                    "annual_kwh": 3650,
                    "terms": [{"entity_id": "sensor.home_energy", "sign": 1}],
                    "exclusions": [],
                },
            )
            await entry.runtime_data.sync()
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                for _ in range(120):
                    async with session.get(url + "/v1/demand") as response:
                        demand = await response.json()
                    if demand["forecast"] and demand["forecast"]["model"] == "weekday-hour-v2":
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError(demand)
                steps = demand["forecast"]["steps"]
                first = datetime.fromisoformat(steps[0]["start"])
                assert len(steps) == {0: 120, 15: 123, 30: 122, 45: 121}[first.minute]
                assert sum(step["duration_h"] for step in steps) == 48
                assert demand["forecast"]["steps"][0]["kwh"] == pytest.approx(0.1)
                assert demand["configuration"]["timezone"] == hass.config.time_zone
                assert "current.json" in list_documents(root / "app", "demand")
                quality_names = [
                    name
                    for name in list_documents(root / "app", "demand")
                    if name.startswith("quality-history-")
                ]
                assert quality_names, (
                    "The existing Recorder transport also archives quality evidence"
                )
                # A partial calendar day can contain one point. Check the retained history across days.
                quality_hours = {}
                for quality_name in quality_names:
                    receipt = json.loads(read_document(root / "app", "demand", quality_name))
                    block = json.loads(read_document(root / "app", "demand", receipt["block"]))
                    assert block["entity_id"] == "sensor.home_energy"
                    quality_hours.update(
                        {hour_start: total_kwh for hour_start, total_kwh, _ in block["hours"]}
                    )
                totals = [total_kwh for _, total_kwh in sorted(quality_hours.items())]
                assert len(totals) > 1
                assert totals[1] - totals[0] == pytest.approx(0.4)
                assert not entry.options

    asyncio.run(scenario())


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("history_error", ["unsupported_statistics", "history_unavailable"])
def test_app_solar_configuration_prefills_energy_sources_and_reads_independent_history(
    shared, history_error, monkeypatch
):
    """Configure two app-owned PV sources with separate or shared converters. Real Recorder flags unsupported history only for roof_west."""
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics

    from custom_components.open_energy_planner.history import HistoryBridge

    original_capture = HistoryBridge.capture

    async def capture(bridge, request):
        """Keep real Recorder reads while distinguishing unsupported sources from a transient outage."""
        result = await original_capture(bridge, request)
        if result and result.get("error") == "unsupported_statistics":
            result["error"] = history_error
        return result

    monkeypatch.setattr(HistoryBridge, "capture", capture)

    async def scenario():
        """Exercise actual Energy preferences, Recorder and authenticated app configuration in isolated HA."""
        async with running_chain(live=True, recorder=True) as (hass, entry, url, root):
            sources = [
                {
                    "type": "solar",
                    "stat_energy_from": f"sensor.roof_{name}",
                    "config_entry_solar_forecast": None,
                }
                for name in ("east", "west")
            ]
            sources.insert(
                0,
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.grid_import",
                    "stat_energy_to": "sensor.grid_export",
                    "stat_rate": "sensor.grid",
                },
            )
            await configure_energy(hass, entry, sources)
            manager = await async_get_manager(hass)
            for name in ("east", "west"):
                hass.states.async_set(f"sensor.roof_{name}", "12", {"unit_of_measurement": "kWh"})
            hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
            end = dt_util.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
                hours=1
            )
            async_import_statistics(
                hass,
                {
                    "statistic_id": "sensor.roof_east",
                    "source": "recorder",
                    "name": None,
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "unit_of_measurement": "kWh",
                    "unit_class": "energy",
                },
                [
                    {
                        "start": end - timedelta(hours=3 - index_item),
                        "sum": 100 + index_item,
                        "state": 100 + index_item,
                    }
                    for index_item in range(3)
                ],
            )
            await get_instance(hass).async_block_till_done()
            await app_write(
                url,
                "sources",
                {
                    "bindings": [
                        {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"}
                    ]
                },
            )
            await app_write(
                url,
                "solar",
                {
                    "sources": [
                        {
                            "source_id": name,
                            "entity_id": f"sensor.roof_{name}",
                            "inverter_id": "shared" if shared else name,
                            "capacity_w": capacity,
                            "orientation": name,
                        }
                        for name, capacity in (("east", 2000), ("west", 3000))
                    ],
                    "inverters": (
                        [{"inverter_id": "shared", "ac_limit_w": 5000, "efficiency": 0.96}]
                        if shared
                        else [
                            {"inverter_id": name, "ac_limit_w": capacity, "efficiency": 0.96}
                            for name, capacity in (("east", 2000), ("west", 3000))
                        ]
                    ),
                },
            )
            # East discovers monthly coverage, then imports its annual chunk.
            # The third independent request diagnoses unsupported west statistics.
            await entry.runtime_data.sync()
            await entry.runtime_data.sync()
            await entry.runtime_data.sync()
            async with (
                ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
                session.get(url + "/v1/solar") as response,
            ):
                saved = await response.json()
            assert len(saved["configuration"]["sources"]) == 2
            assert len(saved["configuration"]["inverters"]) == (1 if shared else 2)
            assert saved["configuration"]["sources"][0]["capacity_w"] == 2000
            assert saved["history_issues"] == {"sensor.roof_west": history_error}
            if history_error == "unsupported_statistics":
                assert entry.runtime_data.issue == "solar_history_unavailable"
            else:
                assert entry.runtime_data.issue != "solar_history_unavailable"
            assert saved["request"] is None
            assert "current.json" in list_documents(root / "app", "solar")
            assert manager.data["energy_sources"] == sources
            assert not entry.options
            assert not any(
                state_item.entity_id.startswith("input_number.")
                for state_item in hass.states.async_all()
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("batteries", [0, 2])
def test_app_installation_saves_optional_batteries_without_helpers(batteries):
    """The app installation API persists zero/two independent batteries and grid limits, deriving only required roles and matching HA targets."""

    async def scenario():
        """Exercise real HA sensor capture and entity reconciliation from app settings without production devices."""
        async with running_chain(live=True, recorder=True) as (hass, entry, url, root):
            for entity, value, unit in (
                ("sensor.grid", 0, "W"),
                ("sensor.house", 1234, "kWh"),
                ("sensor.export", 0, "kWh"),
                *((f"sensor.soc{index_item}", 50, "%") for index_item in range(batteries)),
                *((f"sensor.power{index_item}", 0, "W") for index_item in range(batteries)),
            ):
                hass.states.async_set(entity, str(value), {"unit_of_measurement": unit})
            terms = await configure_energy(
                hass,
                entry,
                [
                    {
                        "type": "grid",
                        "stat_energy_from": "sensor.house",
                        "stat_energy_to": "sensor.export",
                        "stat_rate": "sensor.grid",
                    },
                    *[
                        {
                            "type": "battery",
                            "stat_energy_from": f"sensor.discharge{index_item}",
                            "stat_energy_to": f"sensor.charge{index_item}",
                            "stat_soc": f"sensor.soc{index_item}",
                            "stat_rate": f"sensor.power{index_item}",
                            "capacity": 5,
                        }
                        for index_item in range(batteries)
                    ],
                ],
            )
            await app_write(
                url,
                "demand",
                {
                    "timezone": hass.config.time_zone,
                    "annual_kwh": 3500,
                    "terms": terms,
                    "exclusions": [],
                },
            )
            await app_write(url, "solar", {"sources": [], "inverters": []})
            await app_write(
                url,
                "installation",
                {
                    "grid_power_entity": "sensor.grid",
                    "import_limit_w": 5000,
                    "export_limit_w": 3000,
                    "grid_export_entity": "sensor.export",
                    "batteries": [
                        {
                            "device_id": f"battery{index_item}",
                            "inverter_id": f"inv{index_item}",
                            "energy_entity": f"sensor.discharge{index_item}",
                            "charge_energy_entity": f"sensor.charge{index_item}",
                            "soc_entity": f"sensor.soc{index_item}",
                            "power_entity": f"sensor.power{index_item}",
                            "capacity_kwh": 5,
                            "charge_limit_w": 2000,
                            "discharge_limit_w": 2000,
                        }
                        for index_item in range(batteries)
                    ],
                    "inverters": [
                        {"inverter_id": f"inv{index_item}", "ac_limit_w": 2000, "efficiency": 0.96}
                        for index_item in range(batteries)
                    ],
                },
            )
            await entry.runtime_data.sync()
            await hass.async_block_till_done()
            async with (
                ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
                session.get(url + "/v1/snapshot") as response,
            ):
                saved = await response.json()
            assert len(saved["installation"]["configuration"]["batteries"]) == batteries
            assert len(saved["measurement_config"]["bindings"]) == 2 + 2 * batteries
            from homeassistant.helpers import device_registry as dr
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(hass)
            targets = [
                entity_item
                for entity_item in er.async_entries_for_config_entry(registry, entry.entry_id)
                if "_unit_" in entity_item.unique_id and entity_item.unique_id.endswith("_power")
            ]
            assert len(targets) == batteries
            assert (
                dr.async_get(hass).async_get_device_by_identifier(
                    (DOMAIN, f"{entry.entry_id}_battery"), entry.entry_id
                )
                is None
            )
            assert saved["installation"]["configuration"]["import_limit_w"] == 5000
            grid_details = find(hass, "grid_maximum_grid_import")
            assert grid_details.state == "5000.0"
            assert grid_details.attributes["unit_of_measurement"] == "W"
            assert find(hass, "grid_maximum_grid_export").state == "3000.0"
            assert "current.json" in list_documents(root / "app", "installation")
            assert not entry.options
            assert not any(
                state_item.entity_id.startswith("input_number.")
                for state_item in hass.states.async_all()
            )
            result = await hass.config_entries.options.async_init(entry.entry_id)
            assert result["step_id"] == "init"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "request_key,bridge_attribute",
    [
        ("settlement_request", "settlement_bridge"),
        ("forecast_quality_request", "forecast_history_bridge"),
        ("settlement_bonus_request", "settlement_bonus_bridge"),
    ],
)
@pytest.mark.parametrize("decision", ["pause", "revocation"])
def test_received_stop_is_projected_before_optional_history_wait(
    monkeypatch, decision, request_key, bridge_attribute
):
    """A received pause or revoked plan removes HA commands before an optional history read can block."""

    async def scenario():
        """Hold Recorder capture open while checking the actual coordinator projection and HA command entities."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            snapshot = coordinator.client.snapshot
            started, finish = asyncio.Event(), asyncio.Event()
            assert coordinator.projection.command is not None

            async def stopped_snapshot():
                """Return a legitimate control decision and an independently pending optional history request."""
                data = await snapshot()
                data[request_key] = {"version": "delayed-history"}
                if decision == "pause":
                    data["enabled"] = False
                else:
                    data["plan_invalidated"] = True
                    data["plan"] = None
                return data

            async def capture(_request):
                """Keep optional history work pending until the control-path assertions are complete."""
                started.set()
                await finish.wait()

            monkeypatch.setattr(coordinator.client, "snapshot", stopped_snapshot)
            monkeypatch.setattr(getattr(coordinator, bridge_attribute), "capture", capture)
            task = asyncio.create_task(coordinator.sync())
            try:
                await asyncio.wait_for(started.wait(), 5)
                assert not task.done()
                assert coordinator.projection.command is None
                assert find(hass, "battery_power_target").state == "unavailable"
                if decision == "pause":
                    assert coordinator.schedule.enabled is False
                    assert find(hass, "open_energy_planner_status").state == "paused"
                else:
                    assert coordinator.schedule.plan is None
            finally:
                finish.set()
                await task

    asyncio.run(scenario())
