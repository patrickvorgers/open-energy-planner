"""Verify real HA entities, command freshness, controls and safe state transitions."""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)

from aiohttp import ClientSession, web
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.open_energy_planner.const import DOMAIN
from tests.custom_component.support.chain import (
    COMPONENT,
    TOKEN,
    configure_energy,
    find,
    running_chain,
    states,
)
from tests.ha_contract.support.commands import command_plan


def test_ev_native_controls_explicit_identity_blueprint_and_local_withdrawal():
    """Real HA selects car B, persists its target/departure and drives only a simulated wallbox. SoC/identity loss withdraw permission."""
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.helpers import entity_registry as er

    from test_environment.oep_test_fixture.ev import DATA_KEY, setup_ev
    from tests.app.ev.support.devices import CAR, OTHER_CAR, POINT

    async def scenario():
        """Select car B and charge through the blueprint. Target SoC and disconnect withdraw permission. Reconnect rejects the old session."""
        async with running_chain(live=True, recorder=True, peer_gateway=True) as (
            hass,
            entry,
            url,
            _root,
        ):
            hass.states.async_set(
                "sensor.ev_grid", 0, {"unit_of_measurement": "W", "device_class": "power"}
            )
            hass.states.async_set(
                "sensor.ev_import",
                100,
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            await configure_energy(
                hass,
                entry,
                [
                    {
                        "type": "grid",
                        "stat_energy_from": "sensor.ev_import",
                        "stat_rate": "sensor.ev_grid",
                    }
                ],
            )
            await setup_ev(hass, entry)
            coordinator = entry.runtime_data
            fixture = hass.data[DATA_KEY]
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                async with session.post(url + "/v1/ha/source-choices/ev", json={}) as response:
                    assert response.status == 200
                    assert (await response.json())["pending"]
                await coordinator.sync()
                async with session.get(url + "/v1/ha/source-choices/ev") as response:
                    assert response.status == 200
                    choices = await response.json()
                assert not choices["pending"] and not choices["failed"]
                assert {car["soc"] for car in fixture["cars"]} <= {
                    entity["entity_id"] for entity in choices["catalogue"]["entities"]
                }
                async with session.get(url + "/v1/ev") as response:
                    state = await response.json()
                configuration = {
                    "vehicles": [
                        {
                            "id": vehicle_id,
                            "soc_entity": fixture["cars"][vehicle_index]["soc"],
                            "capacity_kwh": capacity_kwh,
                            "maximum_w": maximum_w,
                            "target_soc": 80,
                            "departure_time": "07:00",
                        }
                        for vehicle_index, (vehicle_id, capacity_kwh, maximum_w) in enumerate(
                            ((CAR, 60, 11000), (OTHER_CAR, 40, 7400))
                        )
                    ],
                    "points": [
                        {
                            "id": POINT,
                            "energy_entity": fixture["points"][0]["energy"],
                            "power_entity": fixture["points"][0]["power"],
                            "connected_entity": fixture["points"][0]["connected"],
                            "minimum_w": 1400,
                            "maximum_w": 11000,
                            "control": "variable",
                            "vehicle_ids": [CAR, OTHER_CAR],
                        }
                    ],
                }
                async with session.post(
                    url + "/v1/ev/configuration",
                    json={
                        "configuration": configuration,
                        "expected_version": state["configuration_version"],
                    },
                ) as response:
                    assert response.status == 200, await response.text()
            await coordinator.sync()
            await hass.async_block_till_done()
            registry = er.async_get(hass)

            def entity(domain, identity, key):
                """Resolve the exact virtual registry identity, independent of HA-generated display entity names."""
                value = registry.async_get_entity_id(
                    domain, DOMAIN, f"{entry.entry_id}_ev_{identity}_{key}"
                )
                assert value is not None
                return value

            car_select = entity("select", POINT, "vehicle")
            target = entity("number", OTHER_CAR, "target_soc")
            daily = entity("time", OTHER_CAR, "departure_time")
            await hass.services.async_call(
                "number", "set_value", {"entity_id": target, "value": 85}, blocking=True
            )
            await hass.services.async_call(
                "time", "set_value", {"entity_id": daily, "time": "06:30:00"}, blocking=True
            )
            assert hass.states.get(target).state == "85.0"
            assert hass.states.get(daily).state == "06:30:00"
            await hass.services.async_call(
                "oep_test_fixture",
                "ev_connect",
                {"point": 0, "vehicle": 1, "connected": True},
                blocking=True,
            )
            await coordinator.sync()
            await hass.async_block_till_done()
            assert hass.states.get(car_select).state == "Unknown"
            car_a = f"{entry.title} ({fixture['cars'][0]['soc']})"
            car_b = f"{entry.title} ({fixture['cars'][1]['soc']})"
            assert car_a != car_b
            assert hass.states.get(car_select).attributes["options"] == ["Unknown", car_a, car_b]
            await hass.services.async_call(
                "select", "select_option", {"entity_id": car_select, "option": car_b}, blocking=True
            )
            await hass.async_block_till_done()
            assert coordinator.ev["points"][0]["vehicle_id"] == OTHER_CAR
            permission, power = (
                entity("binary_sensor", POINT, "charging"),
                entity("sensor", POINT, "power"),
            )
            assert await async_setup_component(
                hass,
                "automation",
                {
                    "automation": [
                        {
                            "id": "simulated_ev_chain",
                            "alias": "Simulated EV chain",
                            "use_blueprint": {
                                "path": "open_energy_planner/ev.yaml",
                                "input": {
                                    "permission": permission,
                                    "power": power,
                                    "charge_action": [
                                        {
                                            "action": "oep_test_fixture.ev_power",
                                            "data": {
                                                "point": 0,
                                                "power_w": "{{ requested_power_w }}",
                                            },
                                        }
                                    ],
                                    "stop_action": [
                                        {
                                            "action": "oep_test_fixture.ev_power",
                                            "data": {"point": 0, "power_w": 0},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
            await hass.async_block_till_done()
            now = dt_util.utcnow()
            plan = command_plan(now, powers=(0,))
            plan["config_revision"] = coordinator.schedule.config_revision
            plan["device_commands"] = True
            plan["schema"] = 2
            nonce = coordinator.ev["points"][0]["actual"]["session_id"]
            plan["ev_context"] = {
                "revision": coordinator.ev["revision"],
                "work": [
                    {
                        "point_id": POINT,
                        "vehicle_id": OTHER_CAR,
                        "session_id": nonce,
                        "maximum_w": 7400,
                        "minimum_w": 1400,
                        "control": "variable",
                        "ready_at": now.isoformat(),
                        "deadline": plan["valid_until"],
                    }
                ],
            }
            for step in plan["steps"]:
                step["devices"] = {
                    POINT: {
                        "kind": "ev",
                        "power_w": 2000,
                        "vehicle_id": OTHER_CAR,
                        "session_id": nonce,
                    }
                }
            coordinator.schedule.accept(plan, now)
            coordinator.refresh(now)
            await hass.async_block_till_done()
            assert hass.states.get(permission).state == "on"
            assert hass.states.get(power).state == "2000"
            assert fixture["data"]["points"][0]["power_w"] == 2000
            assert fixture["data"]["points"][1]["power_w"] == 0
            await hass.services.async_call(
                "oep_test_fixture", "ev_soc", {"vehicle": 1, "soc": 85}, blocking=True
            )
            coordinator.refresh()
            await hass.async_block_till_done()
            assert hass.states.get(permission).state == "off"
            assert fixture["data"]["points"][0]["power_w"] == 0
            await hass.services.async_call(
                "oep_test_fixture", "ev_soc", {"vehicle": 1, "soc": 50}, blocking=True
            )
            await hass.services.async_call(
                "oep_test_fixture",
                "ev_connect",
                {"point": 0, "vehicle": 1, "connected": False},
                blocking=True,
            )
            coordinator.refresh()
            await hass.async_block_till_done()
            assert hass.states.get(permission).state == "off"
            assert fixture["data"]["points"][0]["power_w"] == 0
            assert len((await async_get_manager(hass)).data["device_consumption"]) == 2
            await hass.services.async_call(
                "oep_test_fixture",
                "ev_connect",
                {"point": 0, "vehicle": 1, "connected": True},
                blocking=True,
            )
            await hass.async_block_till_done()
            # No app sync occurs across the physical reconnect. The old nonce must stay unusable.
            assert coordinator.ev_sessions[POINT]["session_id"] != nonce
            assert coordinator.ev_sessions[POINT]["connected"] is True
            coordinator.refresh()
            await hass.async_block_till_done()
            assert hass.states.get(permission).state == "off"
            assert fixture["data"]["points"][0]["power_w"] == 0

    asyncio.run(scenario())


def test_measurement_bridge_reads_real_ha_state_machine_without_rest():
    """Read a real HA kW state in process. Require 1500 normalized watts without REST or hardware access."""
    from custom_components.open_energy_planner.measurement_sources import (
        HomeAssistantMeasurementSource,
    )
    from custom_components.open_energy_planner.measurements import Quantity, SourceBinding

    async def scenario():
        """Read a real HA kW state through the in-process bridge and normalize it to watts."""
        async with running_chain() as (hass, _entry, _url, _root):
            hass.states.async_set("sensor.source_grid", "1.5", {"unit_of_measurement": "kW"})
            batch = HomeAssistantMeasurementSource(hass.states).read(
                [SourceBinding("grid.power", "sensor.source_grid", Quantity.POWER)],
                dt_util.utcnow(),
            )
            assert batch.ready
            assert batch.readings[0].value == 1500
            assert batch.readings[0].unit == "W"

    asyncio.run(scenario())


def test_known_local_fault_during_outage_requires_a_new_plan_after_sensor_recovery(monkeypatch):
    """Fault a required source during disconnection. Keep commands revoked after sensor recovery until a newer plan arrives."""
    from aiohttp import ClientConnectionError

    from custom_components.open_energy_planner.observation_bridge import ObservationBridge

    async def scenario():
        """Recover a required source during an outage and keep the old commands revoked until a new plan arrives."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            assert coordinator.schedule.plan is not None
            original_id = coordinator.schedule.plan.plan_id
            snapshot = coordinator.client.snapshot
            config = {
                "revision": "selected-1",
                "bindings": [
                    {
                        "key": key,
                        "entity_id": entity,
                        "quantity": quantity,
                        "required": True,
                        "multiplier": 1,
                        "maximum_age_s": 300,
                        "timestamp_attribute": None,
                    }
                    for key, entity, quantity in (
                        ("grid.power", "sensor.grid", "power"),
                        ("battery.soc", "sensor.soc", "soc"),
                        ("battery.power", "sensor.battery", "power"),
                    )
                ],
            }
            coordinator.observation_bridge = ObservationBridge(hass.states, config)
            hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
            hass.states.async_set("sensor.soc", "unavailable", {"unit_of_measurement": "%"})
            hass.states.async_set("sensor.battery", "0", {"unit_of_measurement": "W"})

            async def disconnected():
                """Simulate an unavailable app transport while the coordinator handles a locally known source fault."""
                raise ClientConnectionError("Test connection loss")

            monkeypatch.setattr(coordinator.client, "snapshot", disconnected)
            await coordinator.sync()
            assert coordinator.schedule.plan is None
            assert find(hass, "battery_power_target").state == "unavailable"
            # Recovery before reconnect must not reactivate the app's unchanged old plan.
            hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})
            monkeypatch.setattr(coordinator.client, "snapshot", snapshot)
            assert (await snapshot())["plan"]["plan_id"] == original_id
            await coordinator.sync()
            assert coordinator.schedule.plan is None
            assert coordinator.issue is None
            assert find(hass, "battery_power_target").state == "unavailable"
            await coordinator.client.enabled(True, str(uuid4()))
            await coordinator.sync()
            assert coordinator.schedule.plan.plan_id != original_id
            assert find(hass, "battery_power_target").state == "-1000"

    asyncio.run(scenario())


def test_real_ha_unchanged_state_report_updates_freshness():
    """Report the same sensor value again. Require updated last_reported despite unchanged last_updated."""
    from custom_components.open_energy_planner.measurement_sources import (
        HomeAssistantMeasurementSource,
    )
    from custom_components.open_energy_planner.measurements import Quantity, SourceBinding

    async def scenario():
        """Report an unchanged sensor value and compare its real HA report and update timestamps."""
        async with running_chain() as (hass, _entry, _url, _root):
            hass.states.async_set("sensor.stable_grid", "0", {"unit_of_measurement": "W"})
            first = hass.states.get("sensor.stable_grid")
            await asyncio.sleep(0.01)
            hass.states.async_set("sensor.stable_grid", "0", {"unit_of_measurement": "W"})
            current = hass.states.get("sensor.stable_grid")
            assert current.last_updated == first.last_updated
            assert current.last_reported > first.last_updated
            batch = HomeAssistantMeasurementSource(hass.states).read(
                [SourceBinding("grid.power", "sensor.stable_grid", Quantity.POWER)],
                dt_util.utcnow(),
            )
            assert batch.ready
            assert batch.readings[0].timestamp_kind == "ha_report"
            assert batch.readings[0].observed_at == current.last_reported

    asyncio.run(scenario())


def test_large_schema2_resume_and_ordinary_response_size_limits():
    """A resume response carrying a >256 KiB schema-2 plan succeeds. An equally large ordinary settings response remains rejected."""
    import json

    from custom_components.open_energy_planner.client import LocalClient
    from custom_components.open_energy_planner.protocol import Plan

    async def scenario():
        """Serve real chunked HTTP responses and preserve the bounded per-response contract."""
        now = dt_util.utcnow()
        plan = command_plan(now, powers=(0, 0))
        plan.update(schema=2, device_commands=True, input_snapshot={"evidence": "x" * 300000})
        for step in plan["steps"]:
            step["devices"] = {"battery": {"kind": "battery", "power_w": 0}}
        data = {"plan": plan, "enabled": True}
        assert 262144 < len(json.dumps(data)) < 4 * 1024 * 1024

        async def respond(_request):
            """Return the same oversized evidence to exercise snapshot versus ordinary limits."""
            from open_energy_planner.build_info import BUILD_INFO, HEADER_FIELDS

            return web.json_response(
                data,
                headers={
                    header: BUILD_INFO[field] or "" for field, header in HEADER_FIELDS.items()
                },
            )

        app = web.Application()
        app.router.add_put("/v1/enabled", respond)
        app.router.add_get("/v1/solar", respond)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as session:
                client = LocalClient(session, url, TOKEN)
                result = await client.enabled(True, "resume-large")
                assert Plan.parse(result["plan"], plan["config_revision"]).steps
                with pytest.raises(ValueError, match="size limit"):
                    await client.request("GET", "/v1/solar")
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_fixed_pv_export_warning_is_actionable_in_actual_ha(monkeypatch):
    """A fixed-PV/export-cap mismatch produces a deduplicated HA notification instead of silently assuming proportional curtailment."""
    from unittest.mock import patch

    from homeassistant.components import persistent_notification

    async def scenario():
        """Add the app-owned configuration warning to an otherwise valid real snapshot."""
        async with running_chain() as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            snapshot = coordinator.client.snapshot

            async def warned():
                """Retain the genuine response and add only the declared warning code."""
                data = await snapshot()
                data["installation"]["warnings"] = ["fixed_pv_export_limit"]
                return data

            monkeypatch.setattr(coordinator.client, "snapshot", warned)
            with patch.object(
                persistent_notification, "async_create", wraps=persistent_notification.async_create
            ) as notify:
                await coordinator.sync()
                assert coordinator.issue == "fixed_pv_export_limit"
                notify.assert_called_once()
                assert "grid limit does not grant PV control permission" in notify.call_args.args[1]

    asyncio.run(scenario())


def test_actual_ha_reconciles_per_device_capabilities_and_expires_targets():
    """Independent battery/PV commands create only their configured entities, become unavailable on expiry and are removed on capability withdrawal."""

    async def scenario():
        """Use the real HA entity platform with a validated local schedule, never hardware services."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.devices = [
                {
                    "id": "store_a",
                    "kind": "battery",
                    "name": "Store A",
                    "reserve_status": "recovery_required",
                    "configured": True,
                    "configuration": {
                        "capacity_kwh": 10,
                        "recover_reserve": True,
                        "can_follow_grid": True,
                        "cycle_fee": 0,
                        "minimum_soc": 20,
                        "charge_limit_w": 2000,
                        "inverter": {
                            "inverter_id": "hybrid",
                            "ac_limit_w": 3000,
                            "efficiency": 0.96,
                            "hybrid": True,
                        },
                    },
                },
                {"id": "roof_a", "kind": "solar", "name": "Roof A"},
                {
                    "id": "fixed",
                    "kind": "solar",
                    "name": "Fixed PV",
                    "energy_entity": "sensor.fixed_production",
                    "can_curtail": False,
                    "configured": True,
                    "configuration": {"capacity_w": 1200},
                },
            ]
            now = dt_util.utcnow()
            plan = command_plan(now, powers=(0, 0))
            plan["device_commands"] = True
            plan["schema"] = 2
            for step in plan["steps"]:
                step["devices"] = {
                    "store_a": {"kind": "battery", "power_w": -500},
                    "roof_a": {"kind": "solar", "enabled": False},
                }
            coordinator.schedule.accept(plan, now)
            coordinator.refresh(now)
            await hass.async_block_till_done()
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(hass)
            battery_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_power"
            )
            solar_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_roof_a_enabled"
            )
            details_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_setup_status"
            )
            fixed_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_fixed_config_capacity_w"
            )
            capacity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_config_capacity_kwh"
            )
            efficiency_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_config_inverter_efficiency"
            )
            assert hass.states.get(capacity_id).state == "10"
            assert hass.states.get(capacity_id).attributes["unit_of_measurement"] == "kWh"
            assert hass.states.get(efficiency_id).state == "96.0"
            assert hass.states.get(efficiency_id).attributes["unit_of_measurement"] == "%"
            permission_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_config_can_follow_grid"
            )
            wear_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_config_cycle_fee"
            )
            assert hass.states.get(permission_id).state == "yes"
            assert registry.async_get(permission_id).translation_key == "permission"
            assert registry.async_get(details_id).translation_key == "setup_status"
            assert registry.async_get(solar_id).translation_key == "solar_enabled"
            mode_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_mode"
            )
            assert registry.async_get(mode_id).translation_key == "battery_mode"
            assert hass.states.get(wear_id).state == "0"
            assert "capacity_kwh" not in hass.states.get(details_id).attributes
            assert hass.states.get(fixed_id).state == "1200"
            meter_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_fixed_config_energy_entity"
            )
            assert hass.states.get(meter_id).state == "sensor.fixed_production"
            assert (
                registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{entry.entry_id}_unit_fixed_enabled"
                )
                is None
            )
            reserve_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_reserve_status"
            )
            recovery_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_store_a_config_recover_reserve"
            )
            assert hass.states.get(reserve_id).state == "recovery_required"
            assert registry.async_get(reserve_id).translation_key == "reserve_status"
            assert hass.states.get(recovery_id).state == "yes"
            assert hass.states.get(battery_id).state == "-500"
            assert hass.states.get(solar_id).state == "off"
            coordinator.refresh(now + timedelta(hours=2))
            assert hass.states.get(battery_id).state == "unavailable"
            assert hass.states.get(details_id).state == "configured"
            assert hass.states.get(reserve_id).state == "recovery_required"
            coordinator.devices[0]["reserve_status"] = "reserve_satisfied"
            coordinator.devices[0]["configuration"]["capacity_kwh"] = 12
            coordinator.devices[0]["configuration"]["can_follow_grid"] = False
            coordinator.refresh(now + timedelta(hours=2))
            assert hass.states.get(reserve_id).state == "reserve_satisfied"
            assert hass.states.get(capacity_id).state == "12"
            assert hass.states.get(permission_id).state == "no"
            assert hass.states.get(wear_id).state == "0"
            assert hass.states.get(capacity_id).attributes["unit_of_measurement"] == "kWh"
            coordinator.devices = [coordinator.devices[0]]
            coordinator.refresh(now + timedelta(hours=2))
            await hass.async_block_till_done()
            assert registry.async_get(fixed_id) is None
            assert registry.async_get(solar_id) is None
            assert hass.states.get(solar_id) is None

    asyncio.run(scenario())


def test_actual_config_flow_loads_virtual_devices_and_entities():
    """Configure the app through real HA flow. Require the expected virtual devices/entities and active command state."""

    async def scenario():
        """Load the integration through its real config flow and inspect virtual entities and active commands."""
        async with running_chain() as (hass, entry, _url, _root):
            assert find(hass, "battery_power_target").state == "-1000"
            assert find(hass, "battery_control_mode").state == "fixed_power"
            assert find(hass, "grid_power_target").state == "unknown"
            assert find(hass, "open_energy_planner_status").state == "active"
            assert entry.runtime_data.schedule.plan is not None

    asyncio.run(scenario())


def test_unknown_and_zero_are_distinct_in_real_ha_states():
    """Project inactive and zero targets. Require unknown for inapplicable values and numeric zero for an applicable target."""

    async def scenario():
        """Project inactive and zero targets through real entities without conflating unknown and zero."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator_item = entry.runtime_data
            now = dt_util.utcnow()
            coordinator_item.schedule.accept(command_plan(now, powers=(None, 0)), now)
            coordinator_item.refresh(now)
            assert find(hass, "battery_power_target").state == "unknown"
            assert find(hass, "battery_power_target").attributes["applicable"] is False
            assert find(hass, "grid_power_target").state == "0"
            coordinator_item.refresh(now + timedelta(seconds=30))
            assert find(hass, "battery_power_target").state == "0"
            assert find(hass, "battery_power_target").attributes["applicable"] is True
            assert find(hass, "grid_power_target").state == "unknown"

    asyncio.run(scenario())


def test_english_state_labels_load_through_ha_without_changing_raw_states():
    """HA loads explicit readable labels for every enum option while entity IDs and automation-facing states remain unchanged."""
    import json

    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.translation import async_get_translations

    expected = {
        "status": {
            "active": "Active",
            "paused": "Paused",
            "waiting_for_new_plan": "Waiting for new plan",
            "not_ready": "Not ready",
            "expired": "Expired",
            "stale": "Stale",
        },
        "grid_mode": {"zero_grid": "Zero grid", "inactive": "Inactive"},
        "battery_mode": {"fixed_power": "Fixed power", "follow_grid_target": "Follow grid target"},
        "solar_enabled": {"on": "On", "off": "Off"},
        "setup_status": {"configured": "Configured", "setup_required": "Setup required"},
        "permission": {"yes": "Yes", "no": "No"},
        "reserve_status": {
            "measurement_unavailable": "Waiting for usable measurements",
            "reserve_satisfied": "Reserve satisfied",
            "recovery_required": "Recovery required",
            "below_reserve": "Below reserve",
        },
    }

    async def scenario():
        """Verify actual registry metadata and HA's translation loader rather than a custom formatting helper."""
        async with running_chain() as (hass, _entry, _url, _root):
            translations = await async_get_translations(hass, "en", "entity", {DOMAIN})
            source = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
            english = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))
            assert source["entity"] == english["entity"]
            for key, labels in expected.items():
                assert source["entity"]["sensor"][key]["state"] == labels
                for state, label in labels.items():
                    assert (
                        translations[f"component.{DOMAIN}.entity.sensor.{key}.state.{state}"]
                        == label
                    )
            registry = er.async_get(hass)
            for suffix, key, raw in (
                ("open_energy_planner_status", "status", "active"),
                ("grid_control_mode", "grid_mode", "inactive"),
                ("battery_control_mode", "battery_mode", "fixed_power"),
            ):
                state = find(hass, suffix)
                assert state.state == raw
                assert registry.async_get(state.entity_id).translation_key == key
                assert state.attributes["device_class"] == "enum"
                assert set(state.attributes["options"]) == set(expected[key])

    asyncio.run(scenario())


def test_active_plan_time_tracks_accepted_execution_not_sync_or_rejected_candidates():
    """Expose the accepted plan's original generation time. Polls, step changes and rejected plans cannot replace it, and revocation removes it."""

    async def scenario():
        """Exercise actual timestamp entities and local schedule projection without physical control."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator_item = entry.runtime_data
            current = coordinator_item.schedule.plan
            sensor = find(hass, "open_energy_planner_active_plan_generated_at")
            entity_id = sensor.entity_id
            assert sensor.attributes["device_class"] == "timestamp"
            assert dt_util.parse_datetime(sensor.state) == current.generated_at.replace(
                microsecond=0
            )
            assert sensor.attributes["plan_id"] == current.plan_id
            await coordinator_item.sync()
            assert hass.states.get(entity_id).state == sensor.state

            now = dt_util.utcnow()
            replacement = command_plan(now, powers=(-1000, 0), seconds=60)
            assert coordinator_item.schedule.accept(replacement, now)
            coordinator_item.refresh(now)
            await hass.async_block_till_done()
            updated = hass.states.get(entity_id)
            assert dt_util.parse_datetime(updated.state) == now.replace(microsecond=0)
            assert updated.attributes["plan_id"] == replacement["plan_id"]
            assert updated.attributes["plan_id"] != sensor.attributes["plan_id"]

            with pytest.raises(ValueError, match="Future generation"):
                coordinator_item.schedule.accept(command_plan(now + timedelta(seconds=1)), now)
            coordinator_item.refresh(now + timedelta(seconds=61))
            await hass.async_block_till_done()
            assert hass.states.get(entity_id).state == updated.state
            coordinator_item.schedule.invalidate()
            coordinator_item.refresh(now + timedelta(seconds=61))
            await hass.async_block_till_done()
            assert hass.states.get(entity_id).state == "unavailable"
            assert hass.states.get(entity_id).attributes.get("plan_id") is None

    asyncio.run(scenario())


def test_pause_resume_goes_through_http_and_persists_intent():
    """Toggle the virtual switch. Require authenticated app intent updates and a fresh plan before resumed commands."""

    async def scenario():
        """Toggle the actual virtual switch and verify persisted app intent and a newly accepted plan."""
        async with running_chain() as (hass, entry, _url, _root):
            old = entry.runtime_data.schedule.plan.plan_id
            await hass.services.async_call(
                "switch",
                "turn_off",
                {
                    "entity_id": find(hass, "open_energy_planner_enabled").entity_id,
                },
                blocking=True,
            )
            assert find(hass, "battery_power_target").state == "unavailable"
            assert find(hass, "open_energy_planner_status").state == "paused"
            assert find(hass, "open_energy_planner_active_plan_generated_at").state == "unavailable"
            await hass.services.async_call(
                "switch",
                "turn_on",
                {
                    "entity_id": find(hass, "open_energy_planner_enabled").entity_id,
                },
                blocking=True,
            )
            assert entry.runtime_data.schedule.plan.plan_id != old
            assert find(hass, "battery_power_target").state == "-1000"
            assert dt_util.parse_datetime(
                find(hass, "open_energy_planner_active_plan_generated_at").state
            ) == entry.runtime_data.schedule.plan.generated_at.replace(microsecond=0)

    asyncio.run(scenario())


def test_unchanged_plan_replacement_does_not_repeat_command_or_power_state_event():
    """Replace a plan without changing current commands. Require no duplicate command or power-state event."""

    async def scenario():
        """Replace an accepted plan with equal commands and count actual HA command and state events."""
        async with running_chain() as (hass, entry, url, _root):
            command_events, power_events = [], []
            power_id = find(hass, "battery_power_target").entity_id
            hass.bus.async_listen(
                f"{DOMAIN}_command", lambda event_item: command_events.append(event_item.data)
            )
            hass.bus.async_listen(
                "state_changed",
                lambda event_item: (
                    power_events.append(event_item.data)
                    if event_item.data["entity_id"] == power_id
                    else None
                ),
            )
            old = entry.runtime_data.schedule.plan.plan_id
            async with (
                ClientSession() as session,
                session.put(
                    url + "/v1/enabled",
                    json={
                        "enabled": True,
                        "request_id": "replace-with-same-command",
                    },
                    headers={"Authorization": f"Bearer {TOKEN}"},
                ) as response,
            ):
                assert response.status == 200
            await entry.runtime_data.sync()
            await hass.async_block_till_done()
            assert entry.runtime_data.schedule.plan.plan_id != old
            assert command_events == [] and power_events == []

    asyncio.run(scenario())


def test_actual_boundary_timer_expires_without_app_polling():
    """Advance to an accepted plan boundary without synchronization. Require timer-driven command expiry."""

    async def scenario():
        """Advance HA time beyond command validity without polling and inspect timer-driven expiry."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator_item = entry.runtime_data
            now = dt_util.utcnow()
            coordinator_item.schedule.accept(command_plan(now, powers=(-50, 0), seconds=0.05), now)
            coordinator_item.refresh(now)
            await asyncio.sleep(0.2)
            assert find(hass, "battery_power_target").state == "unavailable"
            assert find(hass, "open_energy_planner_status").state == "expired"
            assert find(hass, "open_energy_planner_active_plan_generated_at").state == "unavailable"

    asyncio.run(scenario())


def test_disconnect_preserves_valid_plan_but_cannot_extend_validity():
    """Disconnect the app. Retain a locally valid target only until its original validity deadline."""

    async def scenario():
        """Disconnect transport and verify targets survive only until their original validity deadline."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator_item = entry.runtime_data
            coordinator_item.client.url = "http://127.0.0.1:1"
            await coordinator_item.sync()
            assert find(hass, "battery_power_target").state == "-1000"
            assert coordinator_item.issue == "synchronization_failed"
            assert dt_util.parse_datetime(
                find(hass, "open_energy_planner_active_plan_generated_at").state
            ) == coordinator_item.schedule.plan.generated_at.replace(microsecond=0)
            coordinator_item.refresh(coordinator_item.schedule.plan.valid_until)
            assert find(hass, "battery_power_target").state == "unavailable"

    asyncio.run(scenario())


def test_manual_pause_survives_entry_reload_and_same_entities_are_reused():
    """Reload a paused entry. Preserve disabled intent, reuse entity identities and keep commands unavailable."""

    async def scenario():
        """Reload a paused config entry and compare preserved intent, entity identity and unavailable commands."""
        async with running_chain() as (hass, entry, _url, _root):
            before = set(states(hass))
            await entry.runtime_data.set_enabled(False)
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert set(states(hass)) == before
            assert find(hass, "open_energy_planner_enabled").state == "off"
            assert find(hass, "open_energy_planner_status").state == "paused"
            assert find(hass, "battery_power_target").state == "unavailable"

    asyncio.run(scenario())


def test_ordinary_reload_revalidates_plan_over_http():
    """Reload an enabled entry. Re-fetch and validate the same still-valid plan before exposing commands."""

    async def scenario():
        """Reload an enabled config entry and require real HTTP revalidation before targets reappear."""
        async with running_chain() as (hass, entry, _url, _root):
            old = entry.runtime_data.schedule.plan.plan_id
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.runtime_data.schedule.plan.plan_id == old
            assert find(hass, "battery_power_target").state == "-1000"

    asyncio.run(scenario())


def test_mode_transition_event_contains_coherent_command_and_real_states():
    """Switch fixed/grid-following modes. Require one coherent event matching all actual projected HA states."""

    async def scenario():
        """Switch command modes and inspect actual entity states inside each emitted command event."""
        async with running_chain() as (hass, entry, _url, _root):
            received = []

            def capture(event):
                """Read actual entity states inside the command event listener to detect incoherent publication."""
                received.append(
                    (
                        event.data,
                        find(hass, "battery_power_target").state,
                        find(hass, "battery_control_mode").state,
                        find(hass, "grid_power_target").state,
                    )
                )

            hass.bus.async_listen(f"{DOMAIN}_command", capture)
            coordinator_item = entry.runtime_data
            now = dt_util.utcnow()
            coordinator_item.schedule.accept(command_plan(now, powers=(None,)), now)
            coordinator_item.refresh(now)
            await hass.async_block_till_done()
            assert received[-1][1:] == ("unknown", "follow_grid_target", "0")
            assert received[-1][0]["devices"]["battery"]["power_w"] is None
            now = dt_util.utcnow()
            coordinator_item.schedule.accept(command_plan(now, powers=(500,)), now)
            coordinator_item.refresh(now)
            await hass.async_block_till_done()
            assert received[-1][1:] == ("500", "fixed_power", "unknown")

    asyncio.run(scenario())


def test_user_automation_can_capture_commands_and_fallback_without_hardware():
    """Run a capture-only HA automation. Require pause fallback and resumed command events without device actuation."""

    async def scenario():
        """Use a capture-only HA automation to observe pause and resume events without operating equipment."""
        async with running_chain() as (hass, entry, _url, _root):
            captured = []
            hass.bus.async_listen(
                "oep_test_capture", lambda event_item: captured.append(event_item.data)
            )
            assert await async_setup_component(
                hass,
                "automation",
                {
                    "automation": [
                        {
                            "id": "oep_capture_only",
                            "alias": "Capture virtual EMS commands",
                            "triggers": [{"trigger": "event", "event_type": f"{DOMAIN}_command"}],
                            "actions": [
                                {
                                    "event": "oep_test_capture",
                                    "event_data": {
                                        "status": "{{ trigger.event.data.status }}",
                                        "available": "{{ trigger.event.data.available }}",
                                        "mode": "{{ trigger.event.data.mode }}",
                                    },
                                }
                            ],
                        }
                    ]
                },
            )
            await hass.async_block_till_done()
            await entry.runtime_data.set_enabled(False)
            await hass.async_block_till_done()
            assert captured[-1]["status"] == "paused"
            assert captured[-1]["available"] is False
            await entry.runtime_data.set_enabled(True)
            await hass.async_block_till_done()
            assert captured[-1]["status"] == "active"
            assert captured[-1]["mode"] == "fixed_power"

    asyncio.run(scenario())


def test_multi_device_zero_grid_modes_keep_power_enabled_but_inapplicable():
    """Following publishes grid zero and battery unknown/applicable=false. Fixed idle remains numeric zero, while pause makes all commands unavailable."""
    from homeassistant.helpers import entity_registry as er

    async def scenario():
        """Exercise actual HA enum sensors, stable registry identities and independent follower/fixed batteries."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.devices = [
                {"id": "follower", "kind": "battery", "name": "Follower", "can_follow_grid": True},
                {"id": "fixed", "kind": "battery", "name": "Fixed"},
            ]
            now = dt_util.utcnow()
            plan = command_plan(now, powers=(0, None, 0))
            plan.update(schema=2, device_commands=True)
            for index, step in enumerate(plan["steps"]):
                step["devices"] = {
                    "follower": {
                        "kind": "battery",
                        "mode": "follow_grid_target" if index == 1 else "fixed_power",
                        "power_w": None if index == 1 else 0,
                    },
                    "fixed": {"kind": "battery", "mode": "fixed_power", "power_w": 500},
                }
            coordinator.schedule.accept(plan, now)
            coordinator.refresh(now)
            await hass.async_block_till_done()
            registry = er.async_get(hass)
            ids = {
                key: registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{entry.entry_id}_unit_follower_{key}"
                )
                for key in ("power", "mode")
            }
            assert hass.states.get(ids["power"]).state == "0"
            assert hass.states.get(ids["power"]).attributes["applicable"] is True
            assert hass.states.get(ids["mode"]).state == "fixed_power"
            assert hass.states.get(ids["mode"]).attributes["device_class"] == "enum"
            assert find(hass, "grid_control_mode").state == "inactive"
            assert find(hass, "grid_power_target").state == "unknown"
            coordinator.refresh(now + timedelta(seconds=31))
            assert find(hass, "grid_control_mode").state == "zero_grid"
            assert find(hass, "grid_power_target").state == "0"
            assert hass.states.get(ids["power"]).state == "unknown"
            assert hass.states.get(ids["power"]).attributes["applicable"] is False
            assert hass.states.get(ids["mode"]).state == "follow_grid_target"
            assert registry.async_get(ids["power"]).disabled_by is None
            fixed_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_unit_fixed_power"
            )
            assert hass.states.get(fixed_id).state == "500"
            coordinator.schedule.pause(now + timedelta(seconds=32))
            coordinator.refresh(now + timedelta(seconds=32))
            assert hass.states.get(ids["power"]).state == "unavailable"
            assert hass.states.get(ids["mode"]).state == "unavailable"
            assert find(hass, "grid_control_mode").state == "unavailable"

    asyncio.run(scenario())


@pytest.mark.parametrize("duration_hours", [0.25, 0.5, 0.75, 1])
def test_consumer_forecast_projection_is_estimated_and_expires(duration_hours):
    """HA exposes interval mean W and horizon kWh as estimates. Old forecasts are unavailable, not zero."""
    from types import SimpleNamespace

    from custom_components.open_energy_planner.consumer_sensors import ConsumerSensor, validate

    now = dt_util.utcnow()
    source = "sensor.heat"
    document = {
        "selected": [{"entity_id": source, "weather_sensitive": True}],
        "options": [{"entity_id": source, "name": "Heat pump", "upstream": None}],
        "result": {
            "status": "shadow_waiting_for_decision_evidence",
            "devices": [
                {
                    "entity_id": source,
                    "forecast": {
                        "issued_at": now.isoformat(),
                        "model": "ridge-v2",
                        "unit": "kWh",
                        "steps": [
                            {"start": now.isoformat(), "duration_h": duration_hours, "kwh": 0.125}
                        ],
                    },
                }
            ],
        },
    }
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="consumer-test", data={"url": "http://app"}),
        consumer_forecasts=validate(document),
    )
    power = ConsumerSensor(coordinator, source, "Heat pump", "power")
    energy = ConsumerSensor(coordinator, source, "Heat pump", "energy")
    assert power.native_value == pytest.approx(125 / duration_hours)
    assert energy.native_value == 0.125
    assert power.available and power.extra_state_attributes["estimated"]
    assert power.device_info["name"] == "Heat pump"
    status = ConsumerSensor(coordinator, source, "Heat pump", "status")
    document["result"]["status"] = "history_or_weather_not_ready"
    document["result"]["devices"][0]["status"] = "shadow_waiting_for_decision_evidence"
    assert status.native_value == "shadow_waiting_for_decision_evidence"
    document["result"]["devices"][0]["status"] = "invented_status"
    with pytest.raises(ValueError, match="status"):
        validate(document)
    document["result"]["devices"][0].pop("status")
    assert status.native_value == "history_or_weather_not_ready"
    document["result"]["devices"][0]["forecast"]["issued_at"] = (
        now - timedelta(hours=1)
    ).isoformat()
    assert not power.available
    document["result"]["devices"][0]["forecast"]["steps"][0]["kwh"] = float("nan")
    with pytest.raises(ValueError, match="quantity"):
        validate(document)


def test_invalid_optional_consumer_projection_keeps_valid_plan(monkeypatch):
    """Malformed read-only forecast metadata cannot revoke or block the independent active command plan."""

    async def scenario():
        """Corrupt only the optional extension of a real synchronized local app response."""
        async with running_chain() as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            original = coordinator.client.snapshot
            plan_id = coordinator.schedule.plan.plan_id

            async def malformed():
                """Keep the normal plan envelope while injecting an invalid optional forecast field."""
                data = await original()
                data["consumers"] = {"result": {"status": True}}
                return data

            monkeypatch.setattr(coordinator.client, "snapshot", malformed)
            await coordinator.sync()
            assert coordinator.consumer_forecasts == {}
            assert coordinator.schedule.plan.plan_id == plan_id
            assert coordinator.projection.command is not None

    asyncio.run(scenario())


def test_removed_appliance_is_isolated_and_malformed_projection_withdraws_only_task_commands(
    monkeypatch,
):
    """A withdrawn source cannot purge another forecast. Malformed task data clears stale requests while retaining a valid plan."""
    from custom_components.open_energy_planner.appliances import receive

    async def scenario():
        """Use actual coordinator sync to prove optional projection failures do not corrupt independent command/forecast state."""
        async with running_chain() as (_, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.consumer_forecasts = {
                "options": [{"entity_id": "sensor.healthy", "name": "Healthy"}],
                "selected": [],
            }
            receive(
                coordinator,
                {
                    "devices": [
                        {
                            "entity_id": "sensor.removed",
                            "shiftable": True,
                            "task": {"status": "start_not_confirmed"},
                        },
                        {"entity_id": "sensor.healthy", "shiftable": False, "task": None},
                    ]
                },
            )
            assert coordinator.appliances["devices"] == [
                {"entity_id": "sensor.healthy", "shiftable": False, "task": None}
            ]
            assert coordinator.consumer_forecasts["options"][0]["entity_id"] == "sensor.healthy"
            original = coordinator.client.snapshot
            plan_id = coordinator.schedule.plan.plan_id

            async def malformed():
                """Keep a valid forecast envelope while injecting a broken optional task list."""
                data = await original()
                data["consumers"] = {
                    "options": [{"entity_id": "sensor.healthy", "name": "Healthy"}],
                    "selected": [],
                    "result": None,
                }
                data["appliances"] = {"devices": [None]}
                return data

            monkeypatch.setattr(coordinator.client, "snapshot", malformed)
            await coordinator.sync()
            assert coordinator.appliances == {}
            assert coordinator.consumer_forecasts["options"][0]["entity_id"] == "sensor.healthy"
            assert coordinator.schedule.plan.plan_id == plan_id
            assert coordinator.projection.command is not None

    asyncio.run(scenario())


def test_withdrawn_consumer_removes_its_empty_virtual_device():
    """Withdrawing an Energy forecast selection removes its three sensors and the now-empty virtual device."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    async def scenario():
        """Reconcile through actual HA registration/listeners, not mocked device removal calls."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.consumer_forecasts = {
                "options": [{"entity_id": "sensor.heat", "name": "Heat pump", "upstream": None}],
                "selected": [{"entity_id": "sensor.heat", "weather_sensitive": True}],
            }
            coordinator.refresh()
            await hass.async_block_till_done()
            registry = er.async_get(hass)
            consumer_entries = [
                item
                for item in er.async_entries_for_config_entry(registry, entry.entry_id)
                if "_consumer_" in item.unique_id
            ]
            assert len(consumer_entries) == 3
            device_id = consumer_entries[0].device_id
            assert dr.async_get(hass).async_get(device_id)
            coordinator.consumer_forecasts = {}
            coordinator.refresh()
            await hass.async_block_till_done()
            assert not [
                item
                for item in er.async_entries_for_config_entry(registry, entry.entry_id)
                if "_consumer_" in item.unique_id
            ]
            assert dr.async_get(hass).async_get(device_id) is None

    asyncio.run(scenario())


def test_appliance_schedule_sensors_use_the_accepted_profile_not_new_learning():
    """Four accepted quarter phases mean 60 minutes and 11:00 completion, even after the average grows to two hours."""
    from datetime import UTC
    from types import SimpleNamespace

    from custom_components.open_energy_planner.appliances import ApplianceSensor

    coordinator = SimpleNamespace(
        entry=SimpleNamespace(entry_id="schedule-evidence", data={"url": "http://test"}),
        appliances={
            "devices": [
                {
                    "entity_id": "sensor.dishwasher",
                    "shiftable": True,
                    "profile": {"power_w": [1000] * 8},
                    "task": {
                        "scheduled_start": "2026-10-24T10:00:00+00:00",
                        "profile": {"power_w": [1000] * 4},
                    },
                }
            ]
        },
    )
    duration = ApplianceSensor(coordinator, "sensor.dishwasher", "Dishwasher", "cycle_duration")
    completion = ApplianceSensor(
        coordinator, "sensor.dishwasher", "Dishwasher", "planned_completion"
    )
    assert duration.native_value == 60
    assert completion.native_value == datetime(2026, 10, 24, 11, tzinfo=UTC)


@pytest.mark.parametrize("platform", ["date", "time", "button", "sensor"])
def test_appliance_controls_reconcile_after_pending_removal(monkeypatch, platform):
    """Re-enabling shifting during removal preserves all replacement controls and task sensors."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.open_energy_planner.appliance_controls import ApplianceReady
    from custom_components.open_energy_planner.appliances import ApplianceSensor
    from custom_components.open_energy_planner.date import ApplianceDate
    from custom_components.open_energy_planner.time import ApplianceTime

    async def scenario():
        """Hold real HA entity removal across a newer coordinator update, then verify registry and states."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            source = "sensor.dishwasher_energy"
            coordinator.consumer_forecasts = {
                "options": [{"entity_id": source, "name": "Dishwasher", "upstream": None}],
                "selected": [{"entity_id": source, "weather_sensitive": False}],
            }
            row = {"entity_id": source, "shiftable": True}
            coordinator.appliances = {"devices": [row]}
            coordinator.refresh()
            await hass.async_block_till_done()
            registry = er.async_get(hass)
            original_entity_ids_by_unique_id = {
                item.unique_id: item.entity_id
                for item in er.async_entries_for_config_entry(registry, entry.entry_id)
                if item.domain == platform and "_consumer_" in item.unique_id
            }
            assert original_entity_ids_by_unique_id
            entity_type = {
                "date": ApplianceDate,
                "time": ApplianceTime,
                "button": ApplianceReady,
                "sensor": ApplianceSensor,
            }[platform]
            entered, release = asyncio.Event(), asyncio.Event()
            original_remove = entity_type.async_remove

            async def delayed_remove(entity, *args, **kwargs):
                """Pause the old entity before HA removes its state and registry association."""
                entered.set()
                await release.wait()
                return await original_remove(entity, *args, **kwargs)

            monkeypatch.setattr(entity_type, "async_remove", delayed_remove)
            row["shiftable"] = False
            coordinator.refresh()
            await asyncio.wait_for(entered.wait(), timeout=5)
            row["shiftable"] = True
            coordinator.refresh()
            await asyncio.sleep(0)
            release.set()
            await hass.async_block_till_done()
            for unique_id, entity_id in original_entity_ids_by_unique_id.items():
                assert registry.async_get_entity_id(platform, DOMAIN, unique_id) == entity_id
                assert hass.states.get(entity_id) is not None

    asyncio.run(scenario())
