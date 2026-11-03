"""Verify real Recorder history, settlement reconstruction and optional-history failures."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from tests.database.support.archive import make_store

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)

from aiohttp import ClientSession
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.open_energy_planner.const import DOMAIN
from tests.custom_component.support.chain import (
    TOKEN,
    configure_energy,
    running_chain,
    setup_energy,
)


def test_dishwasher_history_learning_virtual_entities_and_blueprint_reject_stale_start():
    """Real Recorder supplies .83-kWh completed cycles. Normal HA actions configure the app and a stale blueprint request cannot start hardware."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.open_energy_planner.appliances import consumer_identity
    from test_environment.oep_test_fixture.dishwasher import DATA_KEY, setup_dishwasher

    async def scenario():
        """Use the genuine app, integration, Recorder, blueprint loader and simulated physical services."""
        async with running_chain(live=True, recorder=True) as (hass, entry, url, _root):
            hass.states.async_set(
                "sensor.appliance_grid", 0, {"device_class": "power", "unit_of_measurement": "W"}
            )
            hass.states.async_set(
                "sensor.appliance_import",
                100,
                {
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "unit_of_measurement": "kWh",
                },
            )
            await configure_energy(
                hass,
                entry,
                [
                    {
                        "type": "grid",
                        "stat_energy_from": "sensor.appliance_import",
                        "stat_rate": "sensor.appliance_grid",
                    }
                ],
            )
            await setup_dishwasher(hass, entry)
            entry.runtime_data.catalogue_sent = None
            await entry.runtime_data.sync()
            await hass.async_block_till_done(wait_background_tasks=True)
            entity = hass.data[DATA_KEY]["ids"]["energy"]
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:
                async with session.get(url + "/v1/consumers") as response:
                    consumers = await response.json()
                async with session.put(
                    url + "/v1/consumers",
                    json={
                        "expected_version": consumers["version"],
                        "selected": [{"entity_id": entity, "weather_sensitive": False}],
                    },
                ) as response:
                    assert response.status == 200, await response.text()
                async with session.post(
                    url + "/v1/appliances/device", json={"entity_id": entity, "shiftable": True}
                ) as response:
                    assert response.status == 200, await response.text()
            await entry.runtime_data.sync()
            await hass.async_block_till_done()
            device = dr.async_get(hass).async_get_device_by_identifier(
                (DOMAIN, entry.entry_id + "_" + consumer_identity(entity)), entry.entry_id
            )
            assert device and device.name == "Test dishwasher"
            end = dt_util.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            start = end - timedelta(hours=2)
            result = await hass.services.async_call(
                DOMAIN,
                "appliance_record_cycle",
                {
                    "device_id": device.id,
                    "cycle_id": "completed-night",
                    "started_at": start.isoformat(),
                    "completed_at": end.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
            learned = result["appliances"]["devices"][0]["profile"]
            assert learned["power_w"] == pytest.approx([1800, 80, 80, 0, 80, 1200, 80, 0])
            assert learned["samples"] == 1 and learned["evidence"] == "synthetic"
            await hass.async_block_till_done()
            from homeassistant.helpers import entity_registry as er

            energy_entity = er.async_get(hass).async_get_entity_id(
                "sensor", DOMAIN, entry.entry_id + "_" + consumer_identity(entity) + "_cycle_energy"
            )
            assert energy_entity is not None
            assert float(hass.states.get(energy_entity).state) == pytest.approx(0.83)
            assert "Test dishwasher" in hass.states.get(energy_entity).name
            duration_entity = er.async_get(hass).async_get_entity_id(
                "sensor",
                DOMAIN,
                entry.entry_id + "_" + consumer_identity(entity) + "_cycle_duration",
            )
            assert hass.states.get(duration_entity).state == "120.0"
            assert hass.states.get(duration_entity).attributes["unit_of_measurement"] == "min"
            date_entity = er.async_get(hass).async_get_entity_id(
                "date",
                DOMAIN,
                entry.entry_id + "_" + consumer_identity(entity) + "_finish_date",
            )
            ready_entity = er.async_get(hass).async_get_entity_id(
                "button",
                DOMAIN,
                entry.entry_id + "_" + consumer_identity(entity) + "_ready_for_planning",
            )
            time_entity = er.async_get(hass).async_get_entity_id(
                "time", DOMAIN, entry.entry_id + "_" + consumer_identity(entity) + "_finish_time"
            )
            assert date_entity and time_entity and ready_entity
            cancel_entity = er.async_get(hass).async_get_entity_id(
                "button",
                DOMAIN,
                entry.entry_id + "_" + consumer_identity(entity) + "_cancel_planning",
            )
            assert cancel_entity and hass.states.get(cancel_entity).state == "unavailable"
            assert er.async_get(hass).async_get(date_entity).device_id == device.id
            assert er.async_get(hass).async_get(ready_entity).device_id == device.id
            assert hass.states.get(ready_entity).state == "unavailable"
            deadline = (dt_util.utcnow() + timedelta(hours=5)).replace(microsecond=0)
            local_deadline = dt_util.as_local(deadline)
            await hass.services.async_call(
                "date",
                "set_value",
                {"entity_id": date_entity, "date": local_deadline.date().isoformat()},
                blocking=True,
            )
            assert hass.states.get(ready_entity).state == "unavailable"
            await hass.services.async_call(
                "time",
                "set_value",
                {"entity_id": time_entity, "time": local_deadline.time().isoformat()},
                blocking=True,
            )
            assert hass.states.get(date_entity).state == local_deadline.date().isoformat()
            assert hass.states.get(time_entity).state == local_deadline.time().isoformat()
            assert hass.states.get(ready_entity).state != "unavailable"
            await hass.services.async_call(
                "button", "press", {"entity_id": ready_entity}, blocking=True
            )
            accepted_task = entry.runtime_data.appliances["devices"][0]["task"]
            assert dt_util.parse_datetime(accepted_task["deadline"]) == deadline
            assert accepted_task["status"] in (
                "pending",
                "scheduled",
                "forecast_not_ready",
                "deadline_not_achievable",
            )
            assert hass.states.get(ready_entity).state == "unavailable"
            assert hass.states.get(date_entity).state == "unavailable"
            assert hass.states.get(time_entity).state == "unavailable"
            assert hass.states.get(cancel_entity).state != "unavailable"
            await hass.services.async_call(
                "button", "press", {"entity_id": ready_entity}, blocking=True
            )
            assert entry.runtime_data.appliances["devices"][0]["task"]["id"] == accepted_task["id"]
            assert await async_setup_component(
                hass,
                "automation",
                {
                    "automation": [
                        {
                            "alias": "Dishwasher OEP acceptance",
                            "use_blueprint": {
                                "path": "open_energy_planner/appliance.yaml",
                                "input": {
                                    "appliance": device.id,
                                    "start_action": [
                                        {"action": "oep_test_fixture.dishwasher_start"}
                                    ],
                                    "started_trigger": [
                                        {
                                            "trigger": "state",
                                            "entity_id": "sensor.oep_test_dishwasher_status",
                                            "to": "running",
                                        }
                                    ],
                                    "completed_trigger": [
                                        {
                                            "trigger": "state",
                                            "entity_id": "sensor.oep_test_dishwasher_status",
                                            "to": "completed",
                                        }
                                    ],
                                    "cancel_action": [
                                        {"action": "oep_test_fixture.dishwasher_cancel"}
                                    ],
                                    "cancelled_trigger": [
                                        {
                                            "trigger": "state",
                                            "entity_id": "sensor.oep_test_dishwasher_status",
                                            "to": "cancelled",
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            )
            await hass.async_block_till_done()
            assert hass.states.get("automation.dishwasher_oep_acceptance").state == "on"
            hass.bus.async_fire(
                DOMAIN + "_appliance_request",
                {"device_id": device.id, "task_id": "stale", "attempt_id": "stale"},
            )
            await hass.async_block_till_done()
            assert hass.data[DATA_KEY]["starts"] == 0
            # Explicit fixture-only controls prove cancellation survives advancing past the old delayed start.
            await hass.services.async_call(
                "oep_test_fixture", "dishwasher_options", {"delay_seconds": 600}, blocking=True
            )
            await hass.services.async_call(
                "oep_test_fixture", "dishwasher_start", {}, blocking=True
            )
            await hass.services.async_call(
                "oep_test_fixture", "dishwasher_cancel", {}, blocking=True
            )
            await hass.services.async_call(
                "oep_test_fixture", "dishwasher_advance", {"seconds": 7200}, blocking=True
            )
            assert hass.states.get("sensor.oep_test_dishwasher_status").state == "cancelled"
            assert hass.data[DATA_KEY]["data"]["progress"] == 0
            cancellations = []
            hass.bus.async_listen(
                DOMAIN + "_appliance_cancelled", lambda event: cancellations.append(event.data)
            )
            await hass.services.async_call(
                "button", "press", {"entity_id": cancel_entity}, blocking=True
            )
            assert entry.runtime_data.appliances["devices"][0]["task"]["status"] == "cancelled"
            assert hass.states.get(date_entity).state != "unavailable"
            assert hass.states.get(ready_entity).state != "unavailable"
            assert hass.states.get(cancel_entity).state == "unavailable"
            await entry.runtime_data.sync()
            await hass.async_block_till_done()
            assert len(cancellations) == 1
            assert cancellations[0]["task_id"] == accepted_task["id"]
            assert cancellations[0]["device_id"] == device.id
            async with (
                ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session,
                session.post(
                    url + "/v1/appliances/device", json={"entity_id": entity, "shiftable": False}
                ) as response,
            ):
                assert response.status == 200
            await entry.runtime_data.sync()
            await hass.async_block_till_done()
            assert er.async_get(hass).async_get(date_entity) is None
            assert er.async_get(hass).async_get(time_entity) is None
            assert er.async_get(hass).async_get(ready_entity) is None
            assert er.async_get(hass).async_get(cancel_entity) is None
            assert dr.async_get(hass).async_get(device.id) is not None

    asyncio.run(scenario())


def test_live_dishwasher_grid_allocation_preserves_direction_units_and_quarter_history():
    """.1-kWh appliance use reduces .25 export to .15. Next-quarter .3+.2=.5 import never rewrites old export."""
    from copy import deepcopy
    from datetime import UTC

    from test_environment.oep_test_fixture.grid import LiveGridMeter

    start = datetime(2026, 10, 24, 12, tzinfo=UTC)
    roles = {
        "import_wh": "grid_import",
        "import_kwh": "grid_import",
        "export_mwh": "grid_export",
        "export_wh": "grid_export",
    }
    units = {"import_wh": 1000, "import_kwh": 1, "export_mwh": 0.001, "export_wh": 1000}

    def sample(at):
        """Declare independent aggregate .25-kWh export then .2-kWh import quarter baselines."""
        return {
            "import_wh": 0 if at.minute == 0 else 0.1,
            "import_kwh": 0 if at.minute == 0 else 0.1,
            "export_mwh": 0.125 if at.minute == 0 else 0,
            "export_wh": 0.125 if at.minute == 0 else 0,
        }

    data = {"at": start.isoformat(), "counters": dict.fromkeys(roles, 0.0), "dishwasher_kwh": 10.0}
    meter = LiveGridMeter(roles, units, sample, data)
    first = meter.advance(start + timedelta(minutes=15), 10.1, 10)
    assert first == pytest.approx(
        {"import_wh": 0, "import_kwh": 0, "export_mwh": 0.000075, "export_wh": 75}
    )
    second = meter.advance(start + timedelta(minutes=30), 10.4, 10)
    assert second == pytest.approx(
        {"import_wh": 250, "import_kwh": 0.25, "export_mwh": 0.000075, "export_wh": 75}
    )
    assert all(second[entity] >= first[entity] for entity in roles)
    restored = LiveGridMeter(roles, units, sample, deepcopy(meter.data))
    third = restored.advance(start + timedelta(minutes=45), 10.4, 10.4)
    assert third == pytest.approx(
        {"import_wh": 350, "import_kwh": 0.35, "export_mwh": 0.000075, "export_wh": 75}
    )
    before = deepcopy(restored.data)
    with pytest.raises(ValueError, match="counter reset"):
        restored.advance(start + timedelta(minutes=46), 1, 1)
    assert restored.data == before
    with pytest.raises(ValueError, match="dishwasher counter reset"):
        restored.advance(start + timedelta(minutes=46), 1, 1, {"ev": (100, 0)})
    assert restored.data == before


@pytest.mark.parametrize(
    "power_mode,expected_powers",
    [
        ("separate", ["sensor.grid0", "sensor.grid1"]),
        ("tariff", ["sensor.grid0"]),
        ("absent", []),
        ("shared", ["sensor.grid0"]),
    ],
)
def test_energy_multiple_grid_pairs_and_explicit_battery_settings_without_history(
    power_mode, expected_powers
):
    """Both tariff pairs survive without readings. Explicit SoC/capacity lead and distinct configured powers are used once, requiring at least one."""
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    async def scenario():
        """Read real HA Energy preferences and validate the complete integration-to-app metadata contract."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert "energy" not in hass.config.components
            manager = await async_get_manager(hass)
            grid = [
                {
                    "type": "grid",
                    "stat_energy_from": f"sensor.import{index}",
                    "stat_energy_to": f"sensor.export{index}",
                    "stat_rate": f"sensor.grid{index}",
                }
                for index in range(2)
            ]
            if power_mode in ("tariff", "absent"):
                grid[1].pop("stat_rate")
            if power_mode == "absent":
                grid[0].pop("stat_rate")
            if power_mode == "shared":
                grid[1]["stat_rate"] = "sensor.grid0"
            await manager.async_update(
                {
                    "energy_sources": [
                        *grid,
                        {
                            "type": "battery",
                            "stat_energy_from": "sensor.discharge",
                            "stat_energy_to": "sensor.charge",
                            "stat_rate": "sensor.battery",
                            "stat_soc": "sensor.soc",
                            "capacity": 13.5,
                        },
                    ],
                    "device_consumption": [],
                }
            )
            body = await setup_catalogue(hass)
            hints = body["suggestions"]
            assert hints["energy_status"] == "ready"
            assert hints["grid_power_entities"] == expected_powers
            assert hints["settlement"] == {
                "imports": ["sensor.import0", "sensor.import1"],
                "exports": ["sensor.export0", "sensor.export1"],
            }
            assert hints["batteries"] == [
                {
                    "energy_entity": "sensor.discharge",
                    "charge_energy_entity": "sensor.charge",
                    "power_entity": "sensor.battery",
                    "soc_entity": "sensor.soc",
                    "capacity_kwh": 13.5,
                }
            ]
            assert {term["entity_id"]: term["sign"] for term in hints["terms"]} == {
                "sensor.import0": 1,
                "sensor.export0": -1,
                "sensor.import1": 1,
                "sensor.export1": -1,
                "sensor.discharge": 1,
                "sensor.charge": -1,
            }
            assert body["history"] == []
            assert {entity["entity_id"] for entity in body["entities"]} >= {
                "sensor.charge",
                "sensor.discharge",
                "sensor.soc",
                "sensor.battery",
            }
            HACatalogue().receive(body, dt_util.utcnow())

    asyncio.run(scenario())


@pytest.mark.parametrize("upload_stage", ["observations", "appliance_history"])
def test_delayed_observations_or_appliance_history_sync_cannot_overwrite_newer_user_resume_intent(
    monkeypatch, upload_stage
):
    """Delay either upload across explicit resume and preserve the newer intent despite its stale response."""
    from custom_components.open_energy_planner.const import RELEASE

    async def scenario():
        """Suspend a real coordinator upload across user resume and reject stale intent from its eventual reply."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.schedule.pause(dt_util.utcnow())
            hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
            data = {
                "release": RELEASE,
                "enabled": False,
                "config_revision": "selected-1",
                "plan": None,
                "measurement_config": {
                    "revision": "selected-1",
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
                },
            }
            started, finish = asyncio.Event(), asyncio.Event()

            async def snapshot():
                """Return the fixed pre-upload snapshot without changing user intent."""
                return data

            async def upload(_report):
                """Hold synchronization open until explicit resume, then return its stale intent."""
                started.set()
                await finish.wait()
                return data

            monkeypatch.setattr(coordinator.client, "snapshot", snapshot)
            if upload_stage == "observations":
                monkeypatch.setattr(coordinator.client, "observations", upload)
            else:

                async def observations(_report):
                    """Allow control decisions to publish before delaying only optional cycle history."""
                    return data

                monkeypatch.setattr(coordinator.client, "observations", observations)
                monkeypatch.setattr(
                    "custom_components.open_energy_planner.appliance_history.synchronize", upload
                )
            task = asyncio.create_task(coordinator.sync())
            await asyncio.wait_for(started.wait(), 5)
            requested = dt_util.utcnow()
            coordinator.schedule.resume(requested)
            coordinator.pending_enabled = True
            finish.set()
            await task
            assert coordinator.schedule.enabled is True
            assert coordinator.schedule.resume_after == requested
            assert coordinator.pending_enabled is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "unit,factor,mode",
    [
        ("Wh", 1000, "complete"),
        ("kWh", 1, "complete"),
        ("MWh", 0.001, "complete"),
        ("kWh", 1, "gap"),
        ("kWh", 1, "negative_difference"),
        ("kWh", 1, "missing_metadata"),
        ("kWh", 1, "missing_recorder"),
        ("kWh", 1, "multiple"),
        ("kWh", 1, "sum_only"),
        ("kWh", 1, "physical_reset"),
        ("kWh", 1, "late"),
        ("kWh", 1, "empty"),
    ],
)
def test_recorder_history_automatically_establishes_app_bonus_counter(unit, factor, mode):
    """Derive 2 kWh/hour per Recorder meter, or zero for a negative period difference. Missing evidence cannot be replaced with a physical manual seed."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics

    from open_energy_planner.storage.counters import CounterStore

    async def scenario():
        """Write fixtures only to isolated HA's temporary database, then exercise the real integration HTTP bridge."""
        async with running_chain(live=True, recorder=mode != "missing_recorder") as (
            hass,
            entry,
            url,
            root,
        ):
            now = dt_util.utcnow()
            start = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
            count = int((end - start).total_seconds() / 3600)
            rows = [
                {
                    "start": start + timedelta(hours=index - 1),
                    "sum": (100 + 2 * index) * factor,
                    "state": None
                    if mode == "sum_only"
                    else (1 if mode == "physical_reset" and index == count else 500 + 2 * index)
                    * factor,
                }
                for index in range(count + 1)
                if (mode != "gap" or index != 1) and (mode != "late" or index >= 3)
            ]
            if mode == "empty":
                # Metadata exists, but the sole row is outside the requested range.
                rows = [{"start": end, "sum": 100, "state": 500}]
            if mode == "negative_difference":
                rows[-1]["sum"] = 99
            if mode not in ("missing_recorder", "missing_metadata"):
                async_import_statistics(
                    hass,
                    {
                        "statistic_id": "sensor.input_export",
                        "source": "recorder",
                        "name": None,
                        "has_sum": True,
                        "mean_type": StatisticMeanType.NONE,
                        "unit_of_measurement": unit,
                        "unit_class": "energy",
                    },
                    rows,
                )
                if mode == "multiple":
                    async_import_statistics(
                        hass,
                        {
                            "statistic_id": "sensor.second_export",
                            "source": "recorder",
                            "name": None,
                            "has_sum": True,
                            "mean_type": StatisticMeanType.NONE,
                            "unit_of_measurement": "Wh",
                            "unit_class": "energy",
                        },
                        [
                            {**row, "sum": row["sum"] * 1000, "state": row["state"] * 1000}
                            for row in rows
                        ],
                    )
                await get_instance(hass).async_block_till_done()
            for entity, value, sensor_unit in (
                ("sensor.grid", 0, "W"),
                ("sensor.soc", 50, "%"),
                ("sensor.battery", 0, "W"),
                ("sensor.input_export", (500 + 2 * count + 0.5) * factor, unit),
            ):
                hass.states.async_set(entity, str(value), {"unit_of_measurement": sensor_unit})
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                body = {
                    "bindings": [
                        {"key": "grid.power", "quantity": "power", "entity_id": "sensor.grid"},
                        {
                            "key": "grid.export_energy",
                            "quantity": "energy",
                            "entity_id": "sensor.input_export",
                        },
                    ]
                }
                if mode == "multiple":
                    hass.states.async_set(
                        "sensor.second_export",
                        (500 + 2 * count + 0.5) * 1000,
                        {"unit_of_measurement": "Wh"},
                    )
                    body["bindings"].append(
                        {
                            "key": "grid.export_energy.1",
                            "quantity": "energy",
                            "entity_id": "sensor.second_export",
                        }
                    )
                async with session.put(url + "/v1/sources", json=body) as response:
                    assert response.status == 200
                body = {
                    "contract_id": "history-test",
                    "market_area": "NL",
                    "currency": "EUR",
                    "periods": [
                        {
                            "effective_from": start.isoformat(),
                            "effective_until": None,
                            "import_formula": "market",
                            "export_formula": "market",
                        }
                    ],
                }
                body["bonuses"] = [
                    {
                        "rule_id": "solar",
                        "effective_from": start.isoformat(),
                        "rate_formula": "0.04",
                        "cap_kwh": 10000,
                        "timezone": "UTC",
                        "reset_month": start.month,
                        "reset_day": start.day,
                    }
                ]
                async with session.put(url + "/v1/contracts", json=body) as response:
                    assert response.status == 200
                coordinator = entry.runtime_data
                await coordinator.sync()
                async with session.get(url + "/v1/bonus-counters") as response:
                    saved = await response.json()
                if mode not in (
                    "complete",
                    "multiple",
                    "sum_only",
                    "physical_reset",
                    "late",
                    "gap",
                    "negative_difference",
                ):
                    assert saved["status"] == "not_configured"
                    assert coordinator.issue == "bonus_history_unavailable"
                    if mode == "empty":
                        async with session.get(url + "/v1/snapshot") as response:
                            assert (await response.json())["bonus_history"][
                                "issue"
                            ] == "history_missing"
                    retry = coordinator.history_bridge.retry_after
                    await coordinator.sync()
                    assert coordinator.history_bridge.retry_after == retry
                    # A current required-source fault must remain visible even while
                    # historical counter evidence is unavailable and awaiting retry.
                    from unittest.mock import patch

                    from homeassistant.components import persistent_notification

                    for fault in ("missing", "unsupported_unit"):
                        if fault == "missing":
                            hass.states.async_remove("sensor.grid")
                        else:
                            hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "C"})
                        with patch.object(
                            persistent_notification,
                            "async_create",
                            wraps=persistent_notification.async_create,
                        ) as notify:
                            await coordinator.sync()
                            assert coordinator.issue == "measurement_sources_unusable"
                            assert "sensor.grid" in notify.call_args.args[1]
                        hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
                        await coordinator.sync()
                        assert coordinator.issue == "bonus_history_unavailable"
                    async with session.get(url + "/v1/contracts/budgets") as response:
                        budget = (await response.json())["budgets"][0]
                        assert budget["used_kwh"] is None
                        assert budget["confirmation"] is None
                    assert make_store(root / "app", CounterStore).current is None
                    return
                assert saved["status"] == "configured", coordinator.issue
                seed = saved["seeds"][0]
                assert seed["statistics"]["series"][0]["metadata_id"] > 0
                assert seed["statistics"]["series"][0]["source"] == "recorder"
                if mode == "late":
                    assert seed["used_kwh"] is None
                    assert seed["statistics"]["prefix_kwh"] is None
                    assert (
                        seed["statistics"]["reference_at"]
                        == (start + timedelta(hours=3)).isoformat()
                    )
                    return
                multiplier = 2 if mode == "multiple" else 1
                assert seed["used_kwh"] == pytest.approx(
                    0 if mode == "negative_difference" else count * 2 * multiplier
                )
                physical = {"sum_only": None, "physical_reset": 1}.get(
                    mode, (500 + count * 2) * multiplier
                )
                if physical is None:
                    assert seed["meter_kwh"] is None
                else:
                    assert seed["meter_kwh"] == pytest.approx(physical)
                if mode == "multiple":
                    assert len(seed["component_meters"]) == 2
                assert seed["observed_at"] == end.isoformat()
                await coordinator.sync()
                assert coordinator.issue is None
                if mode == "negative_difference":
                    async with session.get(url + "/v1/notifications") as response:
                        notices = (await response.json())["notifications"]
                    assert any(
                        notice["code"] == "bonus_statistics_negative_difference"
                        for notice in notices
                    )
                assert make_store(root / "app", CounterStore).current.version == saved["version"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reason", ["unsupported_statistics", "history_unavailable", "recorder_unavailable"]
)
def test_demand_history_fault_notifies_sensor_names_and_clears_after_recovery(reason):
    """Surface each household history fault with sorted sensor names. Preserve forecast-fault priority and dismiss the notification after recovery."""
    from unittest.mock import patch

    from homeassistant.components import persistent_notification

    async def scenario():
        """Inject app diagnostics into real HA synchronization without using production sensors or Recorder credentials."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            snapshot = coordinator.client.snapshot
            demand = {"history_issues": {"sensor.z_home": reason, "sensor.a_home": reason}}

            async def with_diagnostics():
                """Keep the real authenticated snapshot while adding the app-owned history status under test."""
                data = await snapshot()
                data["demand"] = demand
                return data

            with (
                patch.object(coordinator.client, "snapshot", with_diagnostics),
                patch.object(
                    persistent_notification,
                    "async_create",
                    wraps=persistent_notification.async_create,
                ) as notify,
                patch.object(
                    persistent_notification,
                    "async_dismiss",
                    wraps=persistent_notification.async_dismiss,
                ) as dismiss,
            ):
                await coordinator.sync()
                assert coordinator.issue == "demand_history_unavailable"
                assert "sensor.a_home, sensor.z_home" in notify.call_args.args[1]
                assert "startup estimate" in notify.call_args.args[1]
                count = notify.call_count
                await coordinator.sync()
                assert notify.call_count == count
                demand["issue"] = "demand_configuration_unreadable"
                await coordinator.sync()
                assert coordinator.issue == "demand_forecast_unusable"
                assert (
                    "saved household energy configuration cannot be read"
                    in notify.call_args.args[1]
                )
                count = notify.call_count
                demand["issue"] = "demand_forecast_update_failed"
                await coordinator.sync()
                assert coordinator.issue == "demand_forecast_unusable"
                assert notify.call_count == count + 1
                assert "update retries automatically" in notify.call_args.args[1]
                assert "settings backup" not in notify.call_args.args[1]
                count = notify.call_count
                await coordinator.sync()
                assert notify.call_count == count
                demand.clear()
                demand.update(
                    {
                        "issue": "demand_boundary_inconsistent",
                        "configuration": {
                            "terms": [{"entity_id": "sensor.z_home", "sign": 1}],
                            "exclusions": [{"entity_id": "sensor.a_device", "device_id": "device"}],
                        },
                        "history_quality": {
                            "usable_hours": 0,
                            "corrected_hours": 0,
                            "rejected_hours": 24,
                        },
                    }
                )
                await coordinator.sync()
                assert coordinator.issue == "demand_boundary_inconsistent"
                message = notify.call_args.args[1]
                assert "sensor.a_device, sensor.z_home" in message
                assert "negative balances" in message
                assert "excluded from forecast training" in message
                assert "overlapping sources" in message
                assert "backup restore is not required" in message
                assert "Usable hours, when available, are retained" in message
                count = notify.call_count
                await coordinator.sync()
                assert notify.call_count == count
                demand["configuration"]["terms"] = [{"entity_id": "sensor.new_home", "sign": 1}]
                await coordinator.sync()
                assert coordinator.issue == "demand_boundary_inconsistent"
                assert notify.call_count == count + 1
                assert "sensor.new_home" in notify.call_args.args[1]
                assert "sensor.z_home" not in notify.call_args.args[1]
                count = notify.call_count
                await coordinator.sync()
                assert notify.call_count == count
                demand.clear()
                demand["history_quality"] = {
                    "usable_hours": 240,
                    "corrected_hours": 1,
                    "rejected_hours": 1,
                }
                await coordinator.sync()
                assert coordinator.issue is None
                assert coordinator.notification_message is None
                dismiss.assert_called_with(hass, f"{DOMAIN}.{entry.entry_id}")
                assert notify.call_count == count

    asyncio.run(scenario())


@pytest.mark.parametrize("power_shape", ["flat", "legacy", "transformed"])
def test_energy_configuration_leads_without_current_states_or_history(power_shape):
    """Configured grid/PV stay detected without states/history, configured power wins over inference, and no battery is invented."""
    from copy import deepcopy

    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    async def scenario():
        """Read genuine HA Energy settings and pass the resulting catalogue through the app's wire validator."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            grid = {
                "type": "grid",
                "stat_energy_from": "sensor.import_configured",
                "stat_energy_to": "sensor.export_configured",
                "stat_rate": "sensor.grid_configured",
            }
            if power_shape == "legacy":
                grid.pop("stat_rate")
                grid["power"] = [{"stat_rate": "sensor.grid_configured"}]
            if power_shape == "transformed":
                grid["power_config"] = {"stat_rate_inverted": "sensor.raw_inverted"}
            manager = await async_get_manager(hass)
            sources = [grid, {"type": "solar", "stat_energy_from": "sensor.pv_configured"}]
            await manager.async_update({"energy_sources": sources, "device_consumption": []})
            configured_sources = deepcopy(manager.data["energy_sources"])
            body = await setup_catalogue(hass)
            hints = body["suggestions"]
            assert hints["energy_status"] == "ready"
            expected_power = (
                "sensor.raw_inverted_inverted"
                if power_shape == "transformed"
                else "sensor.grid_configured"
            )
            assert hints["grid_power"] == expected_power
            assert hints["solar_sources"] == ["sensor.pv_configured"]
            assert hints["batteries"] == []
            assert hints["soc"] == []
            assert body["history"] == []
            assert {entity_item["entity_id"] for entity_item in body["entities"]} >= {
                "sensor.import_configured",
                "sensor.export_configured",
                "sensor.pv_configured",
                expected_power,
            }
            HACatalogue().receive(body, dt_util.utcnow())
            assert manager.data["energy_sources"] == configured_sources

    asyncio.run(scenario())


@pytest.mark.parametrize("history_days,second_history_days", [(7, None), (365, None), (30, 10)])
def test_setup_reuses_actual_energy_preferences_and_recorder_history(
    history_days, second_history_days, monkeypatch
):
    """Real Recorder endpoints preserve a week/year total and align differently aged meters to one common period."""
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics

    from custom_components.open_energy_planner import setup_history as setup_reader
    from custom_components.open_energy_planner.catalogue import setup_catalogue

    async def scenario():
        """Exercise the genuine HA Recorder API, normalization, metadata upload and app calculation with no physical devices."""
        async with running_chain(live=True, recorder=True) as (hass, entry, url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            manager = await async_get_manager(hass)
            channels = {"sensor.setup_import": (history_days, 10)}
            if second_history_days:
                channels["sensor.setup_second_import"] = (second_history_days, 3)
            sources = [
                {
                    "type": "grid",
                    "flow_from": [{"stat_energy_from": entity} for entity in channels],
                    "flow_to": [],
                }
            ]
            await manager.async_update({"energy_sources": sources, "device_consumption": []})
            end = dt_util.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            start = end - timedelta(days=history_days)
            for entity, (available_days, daily_kwh) in channels.items():
                hass.states.async_set(
                    entity,
                    "100",
                    {
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                )
                async_import_statistics(
                    hass,
                    {
                        "statistic_id": entity,
                        "source": "recorder",
                        "name": None,
                        "has_sum": True,
                        "mean_type": StatisticMeanType.NONE,
                        "unit_of_measurement": "kWh",
                        "unit_class": "energy",
                    },
                    [
                        {
                            "start": end
                            - timedelta(days=available_days)
                            + timedelta(hours=hour_offset - 1),
                            "sum": 100 + hour_offset * daily_kwh / 24,
                            "state": 100 + hour_offset * daily_kwh / 24,
                        }
                        for hour_offset in range(available_days * 24 + 1)
                    ]
                    + [
                        {
                            "start": end,
                            "sum": 100 + available_days * daily_kwh + 9,
                            "state": 100 + available_days * daily_kwh + 9,
                        }
                    ],
                )
            await get_instance(hass).async_block_till_done()
            queries = []
            original_read = setup_reader.statistics_during_period

            def observe_read(source_hass, first, last, *arguments):
                """Observe real Recorder query windows without replacing its returned measurements."""
                queries.append((first, last))
                return original_read(source_hass, first, last, *arguments)

            with monkeypatch.context() as patch:
                patch.setattr(setup_reader, "statistics_during_period", observe_read)
                body = await setup_catalogue(hass)
            assert body["suggestions"]["energy_status"] == "ready"
            expected_days = second_history_days or history_days
            reference = end - timedelta(days=expected_days)
            assert len(body["history"]) == len(channels)
            for channel in body["history"]:
                assert len(channel["points"]) == 2
                assert channel["points"][0][0] == reference.timestamp()
                assert channel["points"][-1][0] == end.timestamp()
                available_days, daily_kwh = channels[channel["entity_id"]]
                assert channel["points"][0][1] == 100 + (available_days - expected_days) * daily_kwh
                assert channel["points"][-1][1] == 100 + available_days * daily_kwh
            if history_days == 365:
                assert set(queries) == {
                    (start - timedelta(hours=1), start),
                    (end - timedelta(hours=1), end),
                }
            else:
                assert any(last - first > timedelta(days=364) for first, last in queries)
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                async with session.post(url + "/v1/ha/catalogue", json=body) as response:
                    assert response.status == 200
                async with session.get(url + "/v1/setup") as response:
                    setup = await response.json()
            assert setup["annual_estimate"]["annual_kwh"] == (4745 if second_history_days else 3650)
            assert setup["annual_estimate"]["days"] == expected_days
            assert setup["annual_estimate"]["extrapolated"] is (history_days < 365)
            assert manager.data["energy_sources"] == sources
            assert not entry.options

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "delta,accepted", [(-4.7e-10, True), (-1e-7, False), (0.25, True), (float("nan"), False)]
)
@pytest.mark.parametrize("period", ["5minute", "hour"])
def test_settlement_bridge_counter_roundoff_never_admits_resets(
    monkeypatch, delta, accepted, caplog, period
):
    """Sub-micro-Wh unit-conversion noise becomes zero. Real decreases and nonfinite sums stop capture."""
    from custom_components.open_energy_planner import settlement as bridge_module

    async def scenario():
        """Read large cumulative kWh endpoints through the real Recorder executor, with independent supplied deltas."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
            entity = "sensor.settlement_import"
            monkeypatch.setattr(
                bridge_module,
                "get_metadata",
                lambda *_args, **_kwargs: {
                    entity: (1, {"has_sum": True, "unit_of_measurement": "kWh"})
                },
            )

            def statistics(*_args):
                """Supply precise quarter endpoints with a deliberately nonmonotone or invalid second sum."""
                if _args[4] != period:
                    return {entity: []}
                return {
                    entity: [
                        {
                            "start": (start + timedelta(minutes=offset)).timestamp()
                            - (3600 if period == "hour" else 300),
                            "sum": total,
                            "state": total,
                        }
                        for offset, total in [
                            (0, 55771.70358119747),
                            (60 if period == "hour" else 15, 55771.70358119747 + delta),
                        ]
                    ]
                }

            monkeypatch.setattr(bridge_module, "statistics_during_period", statistics)
            report = await bridge_module.SettlementBridge(hass).capture(
                {
                    "version": "roundoff",
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=15)).isoformat(),
                    "bindings": {
                        "terms": [{"entity_id": entity, "sign": 1}],
                        "solar": [],
                        "batteries": [],
                        "imports": [entity],
                        "exports": [],
                    },
                }
            )
            if accepted:
                assert report["frames"][0]["channels"][entity] == pytest.approx(
                    max(0, delta) / (4 if period == "hour" else 1), abs=1e-10
                )
                assert report["frames"][0]["quality"] == (
                    "estimated_hourly" if period == "hour" else "recorded"
                )
            else:
                assert report is None
                assert (
                    "Settlement statistics have a gap: sensor.settlement_import"
                    if period == "hour"
                    else "Invalid settlement sum: sensor.settlement_import"
                ) in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    [
        "idle",
        "idle_hourly",
        "charge",
        "discharge",
        "changed_soc",
        "no_return",
        "too_long",
        "missing_charge",
        "missing_discharge",
        "missing_metadata",
        "invalid_unit",
        "invalid_value",
        "invalid_return",
        "nonfinite_value",
        "out_of_range",
        "missing_meter",
        "no_prior",
    ],
)
def test_settlement_bridge_only_reconstructs_bounded_proven_idle_soc(monkeypatch, case, caplog):
    """A restart gap needs equal valid bounding SoC and two unchanged counters. Every missing proof blocks intake."""
    from homeassistant.core import State

    from custom_components.open_energy_planner import settlement as bridge_module

    async def scenario():
        """A 50% 10-kWh battery remains at 5 kWh. No current state can substitute for missing history."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=4)
            energy_entities = ["sensor.import", "sensor.charge", "sensor.discharge"]
            metadata = {
                entity: (index, {"has_sum": True, "unit_of_measurement": "kWh"})
                for index, entity in enumerate(energy_entities)
            }
            if case == "missing_metadata":
                del metadata["sensor.charge"]
            monkeypatch.setattr(bridge_module, "get_metadata", lambda *_args, **_kwargs: metadata)

            def statistics(*_args):
                """Meter endpoints encompass both sides of the outage. Missing and active counters are distinct."""
                hourly = _args[4] == "hour"
                return {
                    entity: [
                        {
                            "start": (start + timedelta(minutes=offset)).timestamp()
                            - (3600 if hourly else 300),
                            "sum": 100
                            + (0.1 if entity == "sensor." + case and offset >= 30 else 0),
                            "state": 100,
                        }
                        for offset in range(-60, 136, 60 if hourly else 5)
                        if not (case == "idle_hourly" and not hourly and 10 <= offset <= 40)
                        and not (
                            case == "missing_meter"
                            and entity == "sensor.charge"
                            and (hourly or offset == 45)
                        )
                    ]
                    for entity in energy_entities
                }

            monkeypatch.setattr(bridge_module, "statistics_during_period", statistics)
            before = State(
                "sensor.soc",
                {"invalid_value": "bad", "nonfinite_value": "nan", "out_of_range": "101"}.get(
                    case, "50"
                ),
                {"unit_of_measurement": "kWh" if case == "invalid_unit" else "%"},
                last_updated=start - timedelta(hours=1),
            )
            missing = State(
                "sensor.soc", "unavailable", {}, last_updated=start + timedelta(minutes=8)
            )
            after = State(
                "sensor.soc",
                {"changed_soc": "51", "invalid_return": "bad"}.get(case, "50"),
                {"unit_of_measurement": "%"},
                last_updated=start
                + timedelta(minutes=80 if case == "too_long" else 40, seconds=43),
            )
            history = (
                ([] if case == "no_prior" else [before])
                + [missing]
                + ([] if case == "no_return" else [after])
            )

            def historical_soc(_hass, left, right, *_args, **_kwargs):
                """Verify lookback finds a valid state before a gap even when it predates the requested quarter."""
                assert left == start - timedelta(hours=1)
                assert right == start + timedelta(hours=2, seconds=1)
                return {"sensor.soc": history}

            monkeypatch.setattr(bridge_module, "get_significant_states", historical_soc)
            hass.states.async_set("sensor.soc", "90", {"unit_of_measurement": "%"})
            battery = {
                "device_id": "battery",
                "soc_entity": "sensor.soc",
                "capacity_kwh": 10,
                "energy_entity": None if case == "missing_discharge" else "sensor.discharge",
                "charge_energy_entity": None if case == "missing_charge" else "sensor.charge",
            }
            report = await bridge_module.SettlementBridge(hass).capture(
                {
                    "version": "soc-gap",
                    "start": start.isoformat(),
                    "end": (start + timedelta(hours=2)).isoformat(),
                    "bindings": {
                        "terms": [{"entity_id": entity, "sign": 1} for entity in energy_entities],
                        "solar": [],
                        "batteries": [battery],
                        "imports": ["sensor.import"],
                        "exports": [],
                    },
                }
            )
            if case in ("idle", "idle_hourly"):
                assert len(report["frames"]) == 8
                assert all(
                    row["battery_start"] == row["battery_end"] == {"battery": 5}
                    for row in report["frames"]
                )
                assert [row["quality"] for row in report["frames"]] == ["estimated_soc"] * 3 + [
                    "recorded"
                ] * 5
            else:
                assert report is None
                expected = {
                    "charge": "Battery energy changed during unavailable SoC: sensor.charge",
                    "discharge": "Battery energy changed during unavailable SoC: sensor.discharge",
                    "missing_charge": "Battery energy meter charge_energy_entity is required to prove idle SoC for sensor.soc: None",
                    "missing_discharge": "Battery energy meter energy_entity is required for sensor.soc: None",
                    "missing_metadata": "Energy statistics metadata is unusable: sensor.charge",
                    "invalid_value": "Historical battery state is invalid: sensor.soc",
                    "invalid_return": "Historical battery state is invalid: sensor.soc",
                    "nonfinite_value": "Historical battery state is invalid: sensor.soc",
                    "out_of_range": "Historical battery state is invalid: sensor.soc",
                    "missing_meter": f"Settlement statistics have a gap: sensor.charge at {(start + timedelta(minutes=45)).isoformat()}",
                }.get(case, "Historical battery state is missing: sensor.soc")
                assert expected in caplog.text

    asyncio.run(scenario())


def test_settlement_bridge_reads_native_recorder_and_labels_hourly_reconstruction():
    """Real Recorder hourly sums produce two 1-kWh quarters, explicitly estimated, without touching Energy or physical devices."""
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics

    from custom_components.open_energy_planner.settlement import SettlementBridge

    async def scenario():
        """Use the pinned HA Recorder API and an installation without storage, so no SoC is invented."""
        async with running_chain(live=True, recorder=True) as (hass, _entry, _url, _root):
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await setup_energy(hass)
            manager = await async_get_manager(hass)
            await manager.async_update(
                {
                    "energy_sources": [
                        {
                            "type": "grid",
                            "flow_from": [{"stat_energy_from": "sensor.settlement_import"}],
                            "flow_to": [],
                        }
                    ],
                    "device_consumption": [],
                }
            )
            start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
            async_import_statistics(
                hass,
                {
                    "statistic_id": "sensor.settlement_import",
                    "source": "recorder",
                    "name": None,
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "unit_of_measurement": "kWh",
                    "unit_class": "energy",
                },
                [
                    {
                        "start": start - timedelta(hours=1) + timedelta(hours=index_item),
                        "sum": 100 + 4 * index_item,
                        "state": 100 + 4 * index_item,
                    }
                    for index_item in range(3)
                ],
            )
            await get_instance(hass).async_block_till_done()
            before = list(manager.data["energy_sources"])
            bridge = SettlementBridge(hass)
            capture_request = {
                "version": "capture-1",
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=30)).isoformat(),
                "bindings": {
                    "terms": [{"entity_id": "sensor.settlement_import", "sign": 1}],
                    "solar": [],
                    "batteries": [],
                },
            }
            report = await bridge.capture(capture_request)
            assert report is not None
            assert await bridge.capture(capture_request) is None
            retried = await bridge.capture({**capture_request, "version": "capture-2"})
            assert retried is not None
            assert retried["request_version"] == "capture-2"
            assert report["imports"] == ["sensor.settlement_import"]
            assert len(report["frames"]) == 2
            assert [flow["channels"]["sensor.settlement_import"] for flow in report["frames"]] == [
                1,
                1,
            ]
            assert all(flow["quality"] == "estimated_hourly" for flow in report["frames"])
            assert all(
                flow["battery_start"] == flow["battery_end"] == {} for flow in report["frames"]
            )
            assert manager.data["energy_sources"] == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "request_key,bridge_attribute",
    [
        ("settlement_request", "settlement_bridge"),
        ("forecast_quality_request", "forecast_history_bridge"),
        ("settlement_bonus_request", "settlement_bonus_bridge"),
    ],
)
@pytest.mark.parametrize("failure", [ValueError, TimeoutError])
def test_optional_history_failure_does_not_interrupt_command_synchronization(
    monkeypatch, failure, request_key, bridge_attribute
):
    """Malformed or timed-out optional history capture leaves the valid control plan accepted and synchronization healthy."""

    async def scenario():
        """Inject only an optional history-read failure into the real HA coordinator."""
        from unittest.mock import Mock

        async with running_chain() as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            snapshot = coordinator.client.snapshot
            original = coordinator.schedule.plan.plan_id
            attempted = asyncio.Event()
            accepted = Mock(wraps=coordinator.schedule.accept)
            monkeypatch.setattr(coordinator.schedule, "accept", accepted)

            async def pending_snapshot():
                """Keep the real plan response while requesting a faulty optional settlement transfer."""
                data = await snapshot()
                data[request_key] = {"version": "unusable-capture"}
                return data

            async def failed_capture(_request):
                """Emulate an independent history-read problem, not a planner or authentication failure."""
                attempted.set()
                accepted.assert_called_once()
                raise failure("Settlement fixture failure")

            monkeypatch.setattr(coordinator.client, "snapshot", pending_snapshot)
            monkeypatch.setattr(getattr(coordinator, bridge_attribute), "capture", failed_capture)
            await coordinator.sync()
            assert attempted.is_set()
            assert coordinator.issue is None
            assert coordinator.schedule.plan.plan_id == original

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "request_key,bridge_attribute",
    [
        ("settlement_request", "settlement_bridge"),
        ("forecast_quality_request", "forecast_history_bridge"),
        ("settlement_bonus_request", "settlement_bonus_bridge"),
    ],
)
def test_optional_history_wait_cannot_override_newer_resume_intent(
    monkeypatch, request_key, bridge_attribute
):
    """Finishing optional history capture after explicit resume never reapplies or acknowledges an older control response."""

    from unittest.mock import AsyncMock, Mock

    async def scenario():
        """Change user intent during only the optional history await, keeping the actual HA/app chain."""
        async with running_chain() as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.schedule.pause(dt_util.utcnow())
            snapshot = coordinator.client.snapshot
            started, finish = asyncio.Event(), asyncio.Event()

            async def pending_snapshot():
                """Expose a pending optional capture on a valid but pre-resume snapshot."""
                data = await snapshot()
                data[request_key] = {"version": "delayed-history"}
                return data

            async def capture(_request):
                """Wait while a user resumes without providing a fake history batch."""
                started.set()
                await finish.wait()

            monkeypatch.setattr(coordinator.client, "snapshot", pending_snapshot)
            monkeypatch.setattr(getattr(coordinator, bridge_attribute), "capture", capture)
            task = asyncio.create_task(coordinator.sync())
            await asyncio.wait_for(started.wait(), 5)
            accepted = Mock(wraps=coordinator.schedule.accept)
            acknowledged = AsyncMock(wraps=coordinator.client.acknowledge)
            monkeypatch.setattr(coordinator.schedule, "accept", accepted)
            monkeypatch.setattr(coordinator.client, "acknowledge", acknowledged)
            requested = dt_util.utcnow()
            coordinator.schedule.resume(requested)
            coordinator.pending_enabled = True
            finish.set()
            await task
            assert coordinator.schedule.resume_after == requested
            assert coordinator.pending_enabled is True
            accepted.assert_not_called()
            acknowledged.assert_not_awaited()

    asyncio.run(scenario())


def test_appliance_learning_reads_real_recorder_power_and_energy_units(monkeypatch):
    """Recorder statistics convert kW/Wh to W/kWh, keeping negative power and missing energy unknown."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    from homeassistant.components.recorder.models import StatisticMeanType

    from custom_components.open_energy_planner import appliance_history
    from custom_components.open_energy_planner.appliance_history import capture

    async def scenario():
        """Preserve positive power after a negative mean and retain missing readings as None, never zero."""
        async with running_chain(recorder=True, fixture_devices=False) as (
            hass,
            _,
            _,
            _,
        ):
            start = (dt_util.utcnow() - timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            recorder = get_instance(hass)
            energy_entity, power_entity = "sensor.cycle_energy", "sensor.cycle_power"
            recorder.async_import_statistics(
                {
                    "statistic_id": energy_entity,
                    "name": "Energy",
                    "source": "recorder",
                    "unit_of_measurement": "Wh",
                    "unit_class": "energy",
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                },
                [
                    {"start": start, "sum": 1000, "state": 1000},
                    {"start": start + timedelta(minutes=5), "sum": 1100, "state": 1100},
                ],
                StatisticsShortTerm,
            )
            recorder.async_import_statistics(
                {
                    "statistic_id": power_entity,
                    "name": "Power",
                    "source": "recorder",
                    "unit_of_measurement": "kW",
                    "unit_class": "power",
                    "has_sum": False,
                    "mean_type": StatisticMeanType.ARITHMETIC,
                },
                [
                    {
                        "start": start + timedelta(minutes=interval_index * 5),
                        "mean": mean_power,
                        "min": mean_power,
                        "max": mean_power,
                    }
                    for interval_index, mean_power in enumerate((1.2, -0.01, 1.2, 1.2))
                ],
                StatisticsShortTerm,
            )
            await recorder.async_block_till_done()
            rows = await capture(hass, energy_entity, power_entity, dt_util.utcnow())
            assert len(rows) == 4
            assert [row["power_w"] for row in rows] == [1200, None, 1200, 1200]
            assert [row["kwh"] for row in rows] == [1, 1.1, None, None]
            assert rows[0]["at"] == start.isoformat()
            original_statistics = appliance_history.statistics_during_period

            def faulted_statistics(*arguments, **keywords):
                """Inject a numeric source fault after the real Recorder query and unit conversion."""
                result = original_statistics(*arguments, **keywords)
                result[energy_entity][1]["sum"] = float("inf")
                result[power_entity][0]["mean"] = float("nan")
                return result

            monkeypatch.setattr(appliance_history, "statistics_during_period", faulted_statistics)
            faulted = await capture(hass, energy_entity, power_entity, dt_util.utcnow())
            assert faulted[0]["power_w"] is None and faulted[1]["kwh"] is None
            assert faulted[2]["power_w"] == 1200
            json.dumps(faulted, allow_nan=False)

    asyncio.run(scenario())


@pytest.mark.parametrize("fine_fault", [None, "timeout", "upload"])
def test_hourly_recorder_backlog_automatically_admits_provisional_planning_profile(
    monkeypatch, fine_fault
):
    """Real hourly Recorder history supplies a provisional model even when fine capture or ingestion fails."""
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics

    from custom_components.open_energy_planner import appliance_history
    from custom_components.open_energy_planner.appliance_history import synchronize
    from custom_components.open_energy_planner.catalogue import setup_catalogue

    reads, original_capture = [], appliance_history.capture_range

    async def measured_capture(hass, entity, power, first, last, period, **keywords):
        """Retain real Recorder execution while observing its independent discovery/read budgets."""
        reads.append((period, keywords.get("timeout_seconds", 20)))
        if period == "5minute":
            if fine_fault == "timeout":
                raise TimeoutError("Fixture fine-history read timed out")
            if fine_fault == "upload":
                return [{"at": first.isoformat()}]
        return await original_capture(hass, entity, power, first, last, period, **keywords)

    monkeypatch.setattr(appliance_history, "capture_range", measured_capture)

    async def scenario():
        """Use twelve-day-old hourly kWh/kW history with two complete one-hour runs and no fine history."""
        async with running_chain(live=True, recorder=True, fixture_devices=False) as (
            hass,
            entry,
            _,
            _,
        ):
            entity, power = "sensor.old_washer_energy", "sensor.old_washer_power"
            hass.states.async_set(
                entity,
                50,
                {
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "unit_of_measurement": "kWh",
                    "synthetic": True,
                },
            )
            hass.states.async_set(
                power,
                0,
                {
                    "device_class": "power",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "synthetic": True,
                },
            )
            hass.states.async_set(
                "sensor.history_grid_power",
                0,
                {"device_class": "power", "state_class": "measurement", "unit_of_measurement": "W"},
            )
            hass.states.async_set(
                "sensor.history_grid_import",
                100,
                {
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "unit_of_measurement": "kWh",
                },
            )
            await configure_energy(
                hass,
                entry,
                [
                    {
                        "type": "grid",
                        "stat_energy_from": "sensor.history_grid_import",
                        "stat_rate": "sensor.history_grid_power",
                    }
                ],
            )
            manager = await async_get_manager(hass)
            await manager.async_update(
                {"device_consumption": [{"stat_consumption": entity, "stat_rate": power}]}
            )
            start = (dt_util.utcnow() - timedelta(days=12)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            powers = [0, 0, 0.5, 0, 0, 1, 0]
            total, energies = 1000, []
            for mean in powers:
                total += mean * 1000
                energies.append(total)
            for identity, unit, kind, sums in (
                (entity, "Wh", "energy", True),
                (power, "kW", "power", False),
            ):
                async_import_statistics(
                    hass,
                    {
                        "statistic_id": identity,
                        "source": "recorder",
                        "name": "Old washer",
                        "has_sum": sums,
                        "mean_type": StatisticMeanType.NONE
                        if sums
                        else StatisticMeanType.ARITHMETIC,
                        "unit_of_measurement": unit,
                        "unit_class": kind,
                    },
                    [
                        {
                            "start": start + timedelta(hours=index),
                            **(
                                {"sum": energies[index], "state": energies[index]}
                                if sums
                                else {"mean": value, "min": value, "max": value}
                            ),
                        }
                        for index, value in enumerate(powers)
                    ],
                )
            await get_instance(hass).async_block_till_done()
            coordinator = entry.runtime_data
            await coordinator.client.request(
                "POST", "/v1/ha/catalogue", await setup_catalogue(hass)
            )
            consumers = await coordinator.client.request("GET", "/v1/consumers")
            assert (
                next(row for row in consumers["options"] if row["entity_id"] == entity)[
                    "power_entity"
                ]
                == power
            )
            await coordinator.client.request(
                "PUT",
                "/v1/consumers",
                {
                    "expected_version": consumers["version"],
                    "selected": [{"entity_id": entity, "weather_sensitive": False}],
                },
            )
            coordinator.consumer_forecasts = await coordinator.client.request(
                "GET", "/v1/consumers"
            )
            for owner in ("charging_meters", "thermal_meters"):
                coordinator.consumer_forecasts[owner] = [entity]
                await synchronize(coordinator)
                await coordinator.appliance_history_task
                assert (entity, power) not in coordinator.appliance_history_attempts
                assert not reads
                assert not (await coordinator.client.request("GET", "/v1/appliances"))["devices"]
                coordinator.consumer_forecasts[owner] = []
            await synchronize(coordinator)
            await coordinator.appliance_history_task
            document = await coordinator.client.request("GET", "/v1/appliances")
            [row] = document["devices"]
            assert row["profile_kind"] == "Historical estimate"
            assert row["learning_status"] == "provisional"
            assert row["hourly_profile"]["samples"] == 2
            assert row["hourly_synthetic"] is True
            assert row["profile"]["evidence"] == "hourly_estimate"
            assert sum(row["profile"]["power_w"]) / 4000 == pytest.approx(0.75, abs=0.001)
            assert row["history_import"]["phase"] == "complete"
            assert row["history_import"]["usable_intervals"] == 6
            if fine_fault is None:
                assert row["learning_history"]["usable_intervals"] == 0
            else:
                assert "learning_history" not in row
            assert not row["shiftable"] and row["task"] is None
            assert ("day", 60) in reads
            assert ("hour", 20) in reads and ("5minute", 20) in reads

    asyncio.run(scenario())


def test_background_appliance_scan_does_not_hold_sync_open_and_is_cancelled_on_close(monkeypatch):
    """A slow historical reader never blocks scheduling or creates duplicate workers, and unload cancels it."""
    from unittest.mock import AsyncMock

    from custom_components.open_energy_planner import appliance_history

    original_process = appliance_history.process

    async def scenario():
        """Pause the optional worker while genuine coordinator state and lifecycle remain responsive."""
        async with running_chain(fixture_devices=False) as (_, entry, _, _):
            coordinator = entry.runtime_data
            if coordinator.appliance_history_task:
                await coordinator.appliance_history_task
            entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def delayed(_coordinator):
                """Keep the worker busy until unload, recording cancellation rather than swallowing it."""
                entered.set()
                try:
                    await release.wait()
                finally:
                    cancelled.set()

            monkeypatch.setattr(appliance_history, "process", delayed)
            await asyncio.wait_for(appliance_history.synchronize(coordinator), 0.5)
            await entered.wait()
            task = coordinator.appliance_history_task
            await appliance_history.synchronize(coordinator)
            assert coordinator.appliance_history_task is task
            await coordinator.close()
            assert cancelled.is_set() and task.cancelled()
            await appliance_history.synchronize(coordinator)
            assert coordinator.appliance_history_task is task
            coordinator.consumer_forecasts = {
                "selected": [{"entity_id": "sensor.old_energy"}],
                "options": [{"entity_id": "sensor.old_energy", "power_entity": "sensor.old_power"}],
            }
            reader = AsyncMock()
            monkeypatch.setattr(coordinator.client, "request", reader)
            await original_process(coordinator)
            reader.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["authentication", "release"])
@pytest.mark.parametrize("completion", ["synchronize", "unload"])
def test_background_connection_fault_is_consumed_once_without_poisoning_recovery(
    monkeypatch, fault, completion
):
    """Known connection faults reach existing recovery once. They cannot prevent unload or poison later jobs."""
    from custom_components.open_energy_planner import appliance_history
    from custom_components.open_energy_planner.client import (
        AuthenticationError,
        ReleaseMismatchError,
    )

    async def scenario():
        """Fail only the optional appliance read of a genuine coordinator, leaving its lifecycle requests intact."""
        async with running_chain(fixture_devices=False) as (_, entry, _, _):
            coordinator = entry.runtime_data
            if coordinator.appliance_history_task:
                await coordinator.appliance_history_task
            coordinator.consumer_forecasts = {
                "selected": [{"entity_id": "sensor.old_energy"}],
                "options": [{"entity_id": "sensor.old_energy", "power_entity": "sensor.old_power"}],
            }
            original = coordinator.client.request
            error = AuthenticationError if fault == "authentication" else ReleaseMismatchError

            async def failed_read(method, path, body=None):
                """Represent an authenticated optional endpoint rejecting this peer or release identity."""
                if path == "/v1/appliances":
                    raise error("Fixture connection requires recovery")
                return await original(method, path, body)

            monkeypatch.setattr(coordinator.client, "request", failed_read)
            await appliance_history.synchronize(coordinator)
            with pytest.raises(error):
                await coordinator.appliance_history_task
            if completion == "unload":
                await coordinator.close()
                return
            with pytest.raises(error):
                await appliance_history.synchronize(coordinator)
            assert coordinator.appliance_history_task is None
            monkeypatch.setattr(coordinator.client, "request", original)
            await appliance_history.synchronize(coordinator)
            await coordinator.appliance_history_task

    asyncio.run(scenario())
