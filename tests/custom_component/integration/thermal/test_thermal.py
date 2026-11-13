"""Independent HA thermal evidence, permission and helper-free intent contracts."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant", reason="Thermal HA tests run in the pinned HA container")

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.open_energy_planner.ev_controls import EVTarget
from custom_components.open_energy_planner.thermal import capture, command, reading, receive
from custom_components.open_energy_planner.thermal_controls import (
    ThermalEnabled,
    ThermalHeating,
    ThermalMinimum,
    ThermalSensor,
)
from tests.custom_component.support.ev import available_ev


def device(now):
    """Specify one independently configured thermal device and current interval."""
    return {
        "id": "tank",
        "energy_entity": "sensor.boiler_energy",
        "temperature_entity": "sensor.boiler_temperature",
        "power_entity": "sensor.boiler_power",
        "minimum_c": 50,
        "maximum_c": 65,
        "heater_w": 2000,
        "enabled": True,
        "name": "Boiler",
        "actual": None,
        "issue": None,
        "planned": [
            {
                "start": now.isoformat(),
                "end": (now + timedelta(minutes=15)).isoformat(),
                "power_w": 2000,
                "end_c": 55,
                "shortfall_kwh": 0.2,
            }
        ],
    }


def coordinator(hass, row):
    """Provide only the accepted schedule interface consumed by the thin HA projection."""
    target = SimpleNamespace(device_id="tank", kind="thermal", power_w=2000)
    schedule = SimpleNamespace(
        document={"thermal_context": {"revision": "a" * 64, "devices": [deepcopy(row)]}},
        project=lambda _now: SimpleNamespace(command=SimpleNamespace(devices=[target])),
        plan=None,
    )
    return SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(entry_id="entry", data={"url": "http://app"}),
        thermal={"configuration_version": "b" * 64, "revision": "a" * 64, "devices": [row]},
        consumer_forecasts={"options": [{"entity_id": row["energy_entity"]}]},
        schedule=schedule,
        closed=False,
        client=SimpleNamespace(request=AsyncMock()),
        sync=AsyncMock(),
    )


def report(hass, temperature=40, unit="°C", power=2, power_unit="kW"):
    """Publish actual HA states with current report timestamps and explicit physical metadata."""
    hass.states.async_set(
        "sensor.boiler_temperature",
        temperature,
        {"device_class": "temperature", "unit_of_measurement": unit},
    )
    hass.states.async_set(
        "sensor.boiler_power", power, {"device_class": "power", "unit_of_measurement": power_unit}
    )


@pytest.mark.parametrize("temperature,unit", [(40, "°C"), (104, "°F")])
def test_temperature_units_and_freshness_use_ha_report_time(tmp_path, temperature, unit):
    """Equal Celsius/Fahrenheit readings remain usable when unchanged. Future or unavailable states fail."""

    async def scenario():
        """Exercise real HA State objects without replacing their timestamps with model time."""
        hass = HomeAssistant(str(tmp_path))
        try:
            report(hass, temperature, unit)
            now = dt_util.utcnow()
            row = device(now)
            measured = reading(hass, row, now)
            assert measured["temperature_c"] == pytest.approx(40)
            assert measured["power_w"] == 2000
            assert reading(hass, row, now + timedelta(hours=8))["temperature_c"] == pytest.approx(
                40
            )
            assert reading(hass, row, now - timedelta(seconds=1)) is None
            hass.states.async_set("sensor.boiler_power", "unavailable")
            assert capture(coordinator(hass, row), {"devices": [row]}, now)["readings"] == []
        finally:
            await hass.async_stop()

    asyncio.run(scenario())


def test_water_heater_current_temperature_uses_installation_units(tmp_path):
    """Water heaters report their attribute, not the textual operating state or target temperature."""

    async def scenario():
        """Use a real HA source state with an explicit installation Fahrenheit unit system."""
        hass = HomeAssistant(str(tmp_path))
        try:
            report(hass)
            hass.states.async_set(
                "water_heater.tank", "electric", {"current_temperature": 104, "temperature": 140}
            )
            row = device(dt_util.utcnow())
            row["temperature_entity"] = "water_heater.tank"
            source = SimpleNamespace(
                states=hass.states,
                config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°F")),
            )
            assert reading(source, row, dt_util.utcnow())["temperature_c"] == pytest.approx(40)
            hass.states.async_set("water_heater.tank", "electric", {"temperature": 140})
            assert reading(source, row, dt_util.utcnow()) is None
        finally:
            await hass.async_stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        "revision",
        "binding",
        "ownership",
        "temperature",
        "power",
        "paused",
        "disabled",
        "closed",
        "target",
    ],
)
def test_permission_withdrawal_is_local_and_independent(tmp_path, failure):
    """Each missing authority or physical safety input independently withdraws a previously valid request."""

    async def scenario():
        """Prove the positive permission before changing exactly one prerequisite."""
        hass = HomeAssistant(str(tmp_path))
        try:
            report(hass)
            now = dt_util.utcnow()
            row = device(now)
            peer = coordinator(hass, row)
            assert command(peer, "tank") is True
            if failure == "revision":
                peer.schedule.document["thermal_context"]["revision"] = "c" * 64
            elif failure == "binding":
                peer.schedule.document["thermal_context"]["devices"][0]["temperature_entity"] = (
                    "sensor.other"
                )
            elif failure == "ownership":
                peer.consumer_forecasts["options"] = []
            elif failure == "temperature":
                report(hass, 65)
            elif failure == "power":
                hass.states.async_remove("sensor.boiler_power")
            elif failure == "paused":
                peer.schedule.project = lambda _now: SimpleNamespace(command=None)
            elif failure == "disabled":
                row["enabled"] = False
            elif failure == "closed":
                peer.closed = True
            else:
                peer.schedule.project = lambda _now: SimpleNamespace(
                    command=SimpleNamespace(
                        devices=[SimpleNamespace(device_id="tank", kind="thermal", power_w=1000)]
                    )
                )
            assert command(peer, "tank") is None
            assert ThermalHeating(peer, row, "requested_heating").is_on is False
            assert ThermalSensor(peer, row, "planned_power").native_value is None
        finally:
            await hass.async_stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("duration_hours", [0.5, 0.75])
def test_clipped_consumer_forecast_retains_heater_ownership(tmp_path, duration_hours):
    """Clipped coarse horizon blocks retain Energy membership and a guarded heating request."""
    from custom_components.open_energy_planner.consumer_sensors import validate

    async def scenario():
        """Accept the actual consumer projection before evaluating the independent local heater guard."""
        hass = HomeAssistant(str(tmp_path))
        try:
            report(hass, power=0)
            now = dt_util.utcnow()
            row = device(now)
            peer = coordinator(hass, row)
            peer.consumer_forecasts = validate(
                {
                    "selected": [{"entity_id": row["energy_entity"], "weather_sensitive": False}],
                    "options": [{"entity_id": row["energy_entity"], "name": "Boiler"}],
                    "result": {
                        "status": "history_or_weather_not_ready",
                        "devices": [
                            {
                                "entity_id": row["energy_entity"],
                                "status": "shadow_waiting_for_decision_evidence",
                                "forecast": {
                                    "model": "weekday_baseline",
                                    "unit": "kWh",
                                    "issued_at": now.isoformat(),
                                    "steps": [
                                        {
                                            "start": now.isoformat(),
                                            "duration_h": duration_hours,
                                            "kwh": 0.125,
                                        }
                                    ],
                                },
                            }
                        ],
                    },
                }
            )
            assert command(peer, "tank") is True
            assert ThermalHeating(peer, row, "requested_heating").is_on is True
            assert ThermalSensor(peer, row, "planned_power").native_value == 2000
        finally:
            await hass.async_stop()

    asyncio.run(scenario())


def test_native_controls_forward_intent_without_optimistic_or_physical_writes(tmp_path):
    """Native controls retain committed app values and separate measured heat from requested heat."""

    async def scenario():
        """Record authenticated app requests and verify that source states never change."""
        hass = HomeAssistant(str(tmp_path))
        try:
            report(hass, power=0.01)
            now = dt_util.utcnow()
            row = device(now)
            peer = coordinator(hass, row)
            minimum = ThermalMinimum(peer, row, "minimum_c")
            enabled = ThermalEnabled(peer, row, "enabled")
            assert ThermalHeating(peer, row, "requested_heating").is_on is True
            assert ThermalHeating(peer, row, "measured_heating").is_on is False
            assert ThermalSensor(peer, row, "temperature").native_value == 40
            assert ThermalSensor(peer, row, "predicted_temperature").native_value == 55
            await minimum.async_set_native_value(52.5)
            peer.client.request.assert_awaited_with(
                "POST",
                "/v1/thermal/intent",
                {"device_id": "tank", "field": "minimum_c", "value": 52.5},
            )
            assert minimum.native_value == 50
            await enabled.async_turn_off()
            peer.client.request.assert_awaited_with(
                "POST",
                "/v1/thermal/intent",
                {"device_id": "tank", "field": "enabled", "value": False},
            )
            assert enabled.is_on is True
            assert hass.states.get("sensor.boiler_power").state == "0.01"
            peer.client.request.side_effect = ValueError("rejected")
            with pytest.raises(HomeAssistantError):
                await enabled.async_turn_on()
            assert enabled.is_on is True
            with patch(
                "custom_components.open_energy_planner.thermal.dt_util.utcnow",
                return_value=now + timedelta(seconds=301),
            ):
                assert ThermalHeating(peer, row, "requested_heating").is_on is True
        finally:
            await hass.async_stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("heater_w", float("nan")),
        ("enabled", 1),
        ("power_entity", "switch.heater"),
        ("maximum_c", 30),
    ],
)
def test_projection_rejects_invalid_physical_configuration(field, value):
    """Invalid peer metadata cannot create controllable virtual devices."""
    now = dt_util.utcnow()
    document = coordinator(None, device(now)).thermal
    assert receive(document) == document
    document["devices"][0][field] = value
    with pytest.raises(ValueError):
        receive(document)


def test_real_service_projection_normalizes_full_settings_and_actual_identity():
    """The actual SQL-backed app document crosses the HA boundary without weakening field validation."""
    from sqlalchemy import create_engine

    from open_energy_planner.appliances.cycles.service import ApplianceService
    from open_energy_planner.appliances.ev.service import EVService
    from open_energy_planner.appliances.forecasting import ConsumerForecasts
    from open_energy_planner.appliances.thermal.service import ThermalService
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA

    engine = create_engine("sqlite://")
    try:
        SCHEMA.create_all(engine)
        now = dt_util.utcnow()
        identity = "00000000-0000-4000-8000-000000000010"
        metadata = {
            "entities": [
                {
                    "entity_id": "sensor.boiler_energy",
                    "device_class": "energy",
                    "unit": "kWh",
                    "device_name": "Boiler",
                },
                {
                    "entity_id": "sensor.boiler_temperature",
                    "device_class": "temperature",
                    "unit": "°C",
                },
                {"entity_id": "sensor.boiler_power", "device_class": "power", "unit": "W"},
            ],
            "suggestions": {"consumers": [{"entity_id": "sensor.boiler_energy", "upstream": None}]},
        }
        configured = {
            key: value
            for key, value in device(now).items()
            if key not in {"name", "actual", "issue", "planned"}
        }
        configured.update(
            id=identity,
            volume_litres=200,
            ambient_c=20,
            loss_w_per_c=1,
            draw_profile_kwh=[0.2] * 24,
        )
        documents = SqlDocuments(engine, "planning")
        consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
        consumers.sync(metadata)
        appliances = ApplianceService(documents, consumers)
        vehicles = EVService(documents, consumers, appliances)
        app = ThermalService(documents, consumers, appliances, vehicles)
        app.configure({"stores": [configured]}, metadata)
        app.receive(
            {
                "configuration_version": app.configuration_version,
                "at": now.isoformat(),
                "readings": [
                    {
                        "id": identity,
                        "temperature_c": 55,
                        "power_w": 0,
                        "observed_at": now.isoformat(),
                    }
                ],
            },
            now,
        )
        document = app.document(now, metadata)
        row = document["devices"][0]
        row.update(ending_target_c=55, ending_stock_shortfall_kwh=0.1, comfort_shortfall_kwh=None)
        assert row["actual"]["id"] == row["id"]
        accepted = receive(document)["devices"][0]
        assert accepted["actual"]["temperature_c"] == 55
        assert "id" not in accepted["actual"]
        assert "draw_profile_kwh" not in accepted
        assert "ending_target_c" not in accepted
        for mutation in ("unknown", "actual_id", "profile", "summary", "ambient", "incomplete"):
            corrupted = deepcopy(document)
            target = corrupted["devices"][0]
            if mutation == "unknown":
                target["unexpected"] = True
            elif mutation == "actual_id":
                target["actual"]["id"] = "other"
            elif mutation == "profile":
                target["draw_profile_kwh"][0] = float("nan")
            elif mutation == "summary":
                target["ending_stock_shortfall_kwh"] = float("inf")
            elif mutation == "ambient":
                target.update(ambient_c=55, maximum_c=50)
            else:
                del target["volume_litres"]
            with pytest.raises(ValueError):
                receive(corrupted)
    finally:
        engine.dispose()


def test_coordinator_outage_and_local_unavailability_emit_guarded_stop_without_revoking_other_targets(
    tmp_path,
):
    """Unchanged valid states preserve permission during HTTP loss. Unavailable local input stops only the heater."""
    from aiohttp import ClientError
    from homeassistant.core import callback

    from custom_components.open_energy_planner.coordinator import PlanCoordinator
    from custom_components.open_energy_planner.protocol import Command, DeviceCommand, Projection

    async def scenario():
        """Exercise the real coordinator synchronization failure and local refresh/event path."""
        hass = HomeAssistant(str(tmp_path))
        entry = SimpleNamespace(entry_id="thermal-outage", data={"url": "http://app"})
        peer = PlanCoordinator(
            hass, entry, SimpleNamespace(snapshot=AsyncMock(side_effect=ClientError("offline")))
        )
        events = []

        @callback
        def observe(event):
            """Capture the actual public command event emitted for HA automations."""
            events.append(event.data)

        remove_listener = hass.bus.async_listen("open_energy_planner_command", observe)
        try:
            report(hass)
            now = dt_util.utcnow()
            row = device(now)
            thermal_target = DeviceCommand("tank", "thermal", power_w=2000)
            battery_target = DeviceCommand("battery", "battery", power_w=-500)
            vehicle_target = DeviceCommand(
                "wallbox", "ev", power_w=7000, vehicle_id="car", session_id="connected-session"
            )
            accepted = Projection(
                "active", Command(False, (thermal_target, battery_target, vehicle_target))
            )
            peer.schedule = SimpleNamespace(
                document={"thermal_context": {"revision": "a" * 64, "devices": [deepcopy(row)]}},
                resume_after=now,
                project=lambda _now: accepted,
                next_boundary=lambda _now: None,
                plan=SimpleNamespace(plan_id="accepted-plan"),
            )
            available_ev(peer, now)
            peer.schedule.document["ev_context"] = {"revision": "ev-revision"}
            peer.thermal = coordinator(hass, row).thermal
            peer.consumer_forecasts = {"options": [{"entity_id": row["energy_entity"]}]}
            peer.refresh(now)
            await hass.async_block_till_done()
            await peer.sync()
            assert peer.issue == "synchronization_failed"
            assert peer.projection.command.devices == accepted.command.devices
            expiry = now + timedelta(seconds=301)
            with patch(
                "custom_components.open_energy_planner.thermal.dt_util.utcnow", return_value=expiry
            ):
                await peer.sync()
                await hass.async_block_till_done()
            assert peer.projection.command.devices == accepted.command.devices
            hass.states.async_set("sensor.boiler_power", "unavailable")
            peer.refresh(dt_util.utcnow())
            await hass.async_block_till_done()
            targets = {target.device_id: target for target in peer.projection.command.devices}
            assert targets["tank"].power_w is None
            assert targets["battery"] == battery_target and targets["wallbox"] == vehicle_target
            assert events[-1]["devices"]["tank"]["power_w"] is None
            assert events[-1]["devices"]["battery"]["power_w"] == -500
            assert events[-1]["devices"]["wallbox"]["power_w"] == 7000
            report(hass)
            peer.refresh(dt_util.utcnow())
            await hass.async_block_till_done()
            assert peer.projection.command.devices == accepted.command.devices
        finally:
            peer.closed = True
            for cancel in (peer.boundary_cancel, peer.thermal_cancel):
                if cancel:
                    cancel()
            remove_listener()
            await hass.async_stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["authentication_failed", "release_mismatch"])
def test_thermal_observation_identity_failure_uses_connection_attention(
    monkeypatch, caplog, failure
):
    """Authentication and build mismatches retain their identity category and locally valid plan."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.open_energy_planner.client import (
        AuthenticationError,
        ReleaseMismatchError,
    )
    from tests.custom_component.support.chain import running_chain

    async def scenario():
        """Reject the actual observation transfer after a valid thermal snapshot has been received."""
        async with running_chain() as (hass, entry, _url, _root):
            peer = entry.runtime_data
            snapshot, request = peer.client.snapshot, peer.client.request
            projection = coordinator(hass, device(dt_util.utcnow())).thermal
            accepted_plan = peer.schedule.plan
            error = (
                AuthenticationError if failure == "authentication_failed" else ReleaseMismatchError
            )

            async def configured_snapshot():
                """Keep a valid independent plan and configured thermal device in the received snapshot."""
                document = await snapshot()
                document["thermal"] = deepcopy(projection)
                return document

            async def rejected_observation(method, path, *args, **kwargs):
                """Reject only thermal peer identity so the coordinator preserves the error category."""
                if path == "/v1/thermal/observations":
                    raise error("peer identity rejected")
                return await request(method, path, *args, **kwargs)

            monkeypatch.setattr(peer.client, "snapshot", configured_snapshot)
            monkeypatch.setattr(peer.client, "request", rejected_observation)
            log_start = len(caplog.records)
            await peer.sync()
            await hass.async_block_till_done()
            assert peer.issue == failure
            assert peer.thermal["revision"] == projection["revision"]
            assert peer.schedule.plan == accepted_plan
            assert peer.projection.command is not None
            assert all(target.kind != "thermal" for target in peer.projection.command.devices)
            entity_id = er.async_get(hass).async_get_entity_id(
                "binary_sensor",
                "open_energy_planner",
                entry.entry_id + "_thermal_tank_requested_heating",
            )
            assert entity_id is not None
            assert hass.states.get(entity_id).state == "off"
            peer.refresh(accepted_plan.valid_until + timedelta(seconds=1))
            assert peer.projection.command is None
            assert not any(
                "Invalid thermal projection" in record.getMessage()
                for record in caplog.records[log_start:]
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["minimum_c", "enabled"])
def test_native_thermal_intent_timeout_preserves_committed_values(tmp_path, field):
    """A timed-out app request becomes a native HA error and never claims an unsaved setting was committed."""

    async def scenario():
        """Invoke the native number or switch with a real transport timeout and inspect committed values."""
        hass = HomeAssistant(str(tmp_path))
        try:
            row = device(dt_util.utcnow())
            peer = coordinator(hass, row)
            peer.client.request.side_effect = TimeoutError("app did not respond")
            entity = (
                ThermalMinimum(peer, row, field)
                if field == "minimum_c"
                else ThermalEnabled(peer, row, field)
            )
            with pytest.raises(HomeAssistantError):
                if field == "minimum_c":
                    await entity.async_set_native_value(52.5)
                else:
                    await entity.async_turn_off()
            assert row["minimum_c"] == 50 and row["enabled"] is True
            peer.sync.assert_not_awaited()
        finally:
            await hass.async_stop()

    asyncio.run(scenario())


def test_malformed_projection_retains_virtual_registry_and_user_name(monkeypatch):
    """A broken optional thermal reply withdraws permission without erasing configured HA entities or user names."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from tests.custom_component.support.chain import running_chain

    async def scenario():
        """Publish a valid device first, then corrupt only its next optional thermal projection."""
        async with running_chain() as (hass, entry, _url, _root):
            peer = entry.runtime_data
            snapshot, request = peer.client.snapshot, peer.client.request
            row = device(dt_util.utcnow())
            projection = coordinator(hass, row).thermal
            corrupt = False

            async def thermal_snapshot():
                """Keep the independently accepted plan while varying only thermal metadata."""
                data = await snapshot()
                data["thermal"] = {"broken": True} if corrupt else deepcopy(projection)
                data["consumers"] = {
                    "options": [{"entity_id": row["energy_entity"], "name": "Boiler"}],
                    "selected": [],
                    "result": None,
                }
                return data

            async def thermal_request(method, path, *args, **kwargs):
                """Echo valid readings metadata while preserving normal app traffic for other services."""
                if path == "/v1/thermal/observations":
                    return deepcopy(projection)
                return await request(method, path, *args, **kwargs)

            monkeypatch.setattr(peer.client, "snapshot", thermal_snapshot)
            monkeypatch.setattr(peer.client, "request", thermal_request)
            await peer.sync()
            await hass.async_block_till_done()
            registry = er.async_get(hass)
            entity_id = registry.async_get_entity_id(
                "binary_sensor",
                "open_energy_planner",
                entry.entry_id + "_thermal_tank_requested_heating",
            )
            assert entity_id is not None
            virtual = dr.async_get(hass).async_get(registry.async_get(entity_id).device_id)
            dr.async_get(hass).async_update_device(virtual.id, name_by_user="My boiler")
            plan_id = peer.schedule.plan.plan_id
            corrupt = True
            await peer.sync()
            await hass.async_block_till_done()
            assert peer.thermal["revision"] is None
            assert registry.async_get(entity_id).device_id == virtual.id
            assert dr.async_get(hass).async_get(virtual.id).name_by_user == "My boiler"
            assert peer.schedule.plan.plan_id == plan_id
            assert peer.projection.command is not None
            assert hass.states.get(entity_id).state == "off"

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["malformed", "missing", "revision", "binding", "withdrawn"])
def test_optional_thermal_projection_fault_keeps_real_mixed_plan_and_guards_only_heater(
    tmp_path, fault
):
    """A valid battery/EV/heater plan survives optional thermal faults, including acceptance of its next generation."""
    from homeassistant.core import callback

    from custom_components.open_energy_planner.const import RELEASE
    from custom_components.open_energy_planner.coordinator import PlanCoordinator

    async def scenario():
        """Use actual protocol parsing, membership checks and public events instead of a mocked Schedule."""
        hass = HomeAssistant(str(tmp_path))
        identity = "00000000-0000-4000-8000-000000000101"
        now = dt_util.utcnow()
        row = device(now)
        row["id"] = identity
        frozen = {
            key: row[key]
            for key in (
                "id",
                "energy_entity",
                "temperature_entity",
                "power_entity",
                "minimum_c",
                "maximum_c",
                "heater_w",
                "enabled",
            )
        }
        frozen.update(
            volume_litres=120, ambient_c=20, loss_w_per_c=1.2, draw_profile_kwh=[1] + [0] * 23
        )
        projection = {"configuration_version": "b" * 64, "revision": "a" * 64, "devices": [row]}
        plan = {
            "schema": 2,
            "release": RELEASE,
            "plan_id": "mixed-plan",
            "config_revision": "installation-revision",
            "strategy": "minimize cost",
            "solution_status": "optimal",
            "device_commands": True,
            "generated_at": (now - timedelta(seconds=2)).isoformat(),
            "valid_until": (now + timedelta(minutes=10)).isoformat(),
            "thermal_context": {"revision": "a" * 64, "devices": [frozen]},
            "ev_context": {
                "revision": "ev-revision",
                "work": [
                    {
                        "point_id": "wallbox",
                        "vehicle_id": "car",
                        "session_id": "connected-session",
                        "minimum_w": 1400,
                        "maximum_w": 7000,
                        "control": "variable",
                        "ready_at": (now - timedelta(minutes=1)).isoformat(),
                        "deadline": (now + timedelta(hours=1)).isoformat(),
                    }
                ],
            },
            "steps": [
                {
                    "start": (now - timedelta(seconds=1)).isoformat(),
                    "end": (now + timedelta(minutes=15)).isoformat(),
                    "grid_active": False,
                    "devices": {
                        "battery": {"kind": "battery", "power_w": -500},
                        "wallbox": {
                            "kind": "ev",
                            "power_w": 7000,
                            "vehicle_id": "car",
                            "session_id": "connected-session",
                        },
                        identity: {"kind": "thermal", "power_w": 2000},
                    },
                }
            ],
        }
        snapshot = {
            "release": RELEASE,
            "source": "synthetic",
            "config_revision": "installation-revision",
            "enabled": True,
            "installation": {"devices": [{"id": "battery", "kind": "battery"}]},
            "consumers": {
                "options": [{"entity_id": row["energy_entity"], "name": "Boiler"}],
                "selected": [],
                "result": None,
            },
            "plan": plan,
            "thermal": projection,
        }

        async def read_snapshot():
            """Return independently copied transport documents, preserving coordinator mutation ownership."""
            return deepcopy(snapshot)

        async def observations(_method, path, _body):
            """Echo only the optional valid projection, leaving the plan snapshot unchanged."""
            assert path == "/v1/thermal/observations"
            return deepcopy(snapshot["thermal"])

        client = SimpleNamespace(
            snapshot=AsyncMock(side_effect=read_snapshot),
            request=AsyncMock(side_effect=observations),
            acknowledge=AsyncMock(),
        )
        peer = PlanCoordinator(
            hass, SimpleNamespace(entry_id="mixed-thermal", data={"url": "http://app"}), client
        )
        available_ev(peer, now)
        peer.save = AsyncMock()
        events = []

        @callback
        def observe(event):
            """Record emitted targets after the coordinator's local safety filtering."""
            events.append(event.data)

        remove_listener = hass.bus.async_listen("open_energy_planner_command", observe)
        try:
            report(hass)
            await peer.sync()
            await hass.async_block_till_done()
            assert peer.schedule.plan.plan_id == "mixed-plan"
            assert command(peer, identity) is True
            assert events[-1]["devices"][identity]["power_w"] == 2000
            replacement = deepcopy(plan)
            replacement.update(plan_id="mixed-replacement", generated_at=now.isoformat())
            snapshot["plan"] = replacement
            if fault == "malformed":
                snapshot["thermal"] = {"broken": True}
            elif fault == "missing":
                snapshot.pop("thermal")
            elif fault == "revision":
                snapshot["thermal"]["revision"] = "c" * 64
            elif fault == "binding":
                snapshot["thermal"]["devices"][0]["temperature_entity"] = "sensor.other_temperature"
            else:
                snapshot["thermal"]["devices"] = []
                snapshot["thermal"]["revision"] = "c" * 64
            await peer.sync()
            await hass.async_block_till_done()
            assert peer.schedule.plan.plan_id == "mixed-replacement"
            assert command(peer, identity) is None
            targets = {target.device_id: target for target in peer.projection.command.devices}
            assert targets[identity].power_w is None
            assert targets["battery"].power_w == -500
            assert targets["wallbox"].power_w == 7000
            assert events[-1]["devices"][identity]["power_w"] is None
            assert events[-1]["devices"]["battery"]["power_w"] == -500
            assert events[-1]["devices"]["wallbox"]["power_w"] == 7000
            # Optional projection tolerance does not weaken malformed-plan validation.
            invalid = deepcopy(replacement)
            invalid["plan_id"] = "invalid-partial-heater-power"
            invalid["steps"][0]["devices"][identity]["power_w"] = 1000
            snapshot["plan"] = invalid
            await peer.sync()
            assert peer.schedule.plan.plan_id == "mixed-replacement"
            assert peer.issue == "synchronization_failed"
        finally:
            peer.closed = True
            for cancel in (peer.boundary_cancel, peer.thermal_cancel):
                if cancel:
                    cancel()
            remove_listener()
            await hass.async_stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "ui_url", [None, "/local_open_energy_planner", "homeassistant://app/local_open_energy_planner"]
)
def test_device_configuration_links_open_shared_settings_without_changing_identity(ui_url):
    """Standalone links open their category. Native HA app destinations and device IDs stay stable."""
    row = device(dt_util.utcnow())
    peer = coordinator(None, row)
    if ui_url:
        peer.entry.data["ui_url"] = ui_url
    thermal = ThermalMinimum(peer, row, "minimum_c")
    vehicle = EVTarget(peer, "vehicles", {"id": "car", "name": "Family car"}, "target_soc")
    expected = "homeassistant:/" + ui_url if ui_url and ui_url.startswith("/") else ui_url
    assert thermal.device_info["configuration_url"] == (
        expected or "http://app/appliance-settings/thermal"
    )
    assert vehicle.device_info["configuration_url"] == (
        expected or "http://app/appliance-settings/ev"
    )
    assert thermal.unique_id == "entry_thermal_tank_minimum_c"
    assert vehicle.unique_id == "entry_ev_car_target_soc"
