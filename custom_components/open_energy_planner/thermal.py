"""Bounded thermal readings and virtual permissions. Physical actuation remains in HA automations."""

import re
from copy import deepcopy
from datetime import datetime
from math import isfinite

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .protocol import (
    MAX_THERMAL_HOURLY_DRAW_KWH,
    THERMAL_CONFIGURATION_BOUNDS,
    THERMAL_PROFILE_HOURS,
    identifier,
    timestamp,
)
from .subentries import add_grouped_entities, retire_empty_groups

MAX_THERMAL_DEVICES = 4
MAX_THERMAL_STEPS = 1024
MAX_THERMAL_POWER_W = 30000
MAX_THERMAL_ENERGY_KWH = 10000
MAX_THERMAL_TEXT_LENGTH = 160
MIN_TEMPERATURE_C = 0
MAX_TEMPERATURE_C = 120
SENSOR_PATTERN = re.compile(r"sensor\.[a-z0-9_]+")
TEMPERATURE_ENTITY_PATTERN = re.compile(r"(?:sensor|water_heater)\.[a-z0-9_]+")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


def number(value, minimum, maximum):
    """Reject boolean, nonfinite and out-of-range quantities at the peer boundary."""
    if type(value) not in (int, float) or not minimum <= value <= maximum or not isfinite(value):
        raise ValueError("Invalid thermal quantity")
    return value


def reading(hass, device, now):
    """Read current HA reports in Celsius and watts without inventing missing measurements."""
    try:
        temperature = hass.states.get(device["temperature_entity"])
        power = hass.states.get(device["power_entity"])
        if (
            temperature is None
            or power is None
            or any(state.state in ("unknown", "unavailable") for state in (temperature, power))
        ):
            return None
        reported = []
        for state in (temperature, power):
            observed_at = state.last_reported
            if (
                not isinstance(observed_at, datetime)
                or observed_at.utcoffset() is None
                or observed_at > now
            ):
                return None
            reported.append(observed_at)
        if device["temperature_entity"].startswith("water_heater."):
            temperature_value = temperature.attributes.get("current_temperature")
            temperature_unit = hass.config.units.temperature_unit
        else:
            if temperature.attributes.get("device_class") != "temperature":
                return None
            temperature_value = temperature.state
            temperature_unit = temperature.attributes.get("unit_of_measurement")
        if temperature_unit not in ("°C", "°F"):
            return None
        if isinstance(temperature_value, bool):
            return None
        temperature_c = float(temperature_value)
        if temperature_unit == "°F":
            temperature_c = (temperature_c - 32) * 5 / 9
        if power.attributes.get("device_class") != "power":
            return None
        power_factor = {"W": 1, "kW": 1000}.get(power.attributes.get("unit_of_measurement"))
        if power_factor is None:
            return None
        power_w = float(power.state) * power_factor
        number(temperature_c, MIN_TEMPERATURE_C, MAX_TEMPERATURE_C)
        number(power_w, 0, MAX_THERMAL_POWER_W)
        return {
            "id": device["id"],
            "temperature_c": temperature_c,
            "power_w": power_w,
            "observed_at": min(reported).isoformat(),
        }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


def capture(coordinator, projection, now):
    """Capture current valid HA states, retaining their original oldest report time as provenance."""
    readings = []
    for device in projection.get("devices", []):
        measured = reading(coordinator.hass, device, now)
        if measured is not None:
            readings.append(measured)
    return {
        "configuration_version": projection.get("configuration_version"),
        "at": now.isoformat(),
        "readings": readings,
    }


def receive(document):
    """Validate bounded app metadata and plans before creating entities or exposing control."""
    required = {"configuration_version", "revision", "devices"}
    if (
        not isinstance(document, dict)
        or not required <= document.keys()
        or document.keys() - required - {"configuration", "catalogue", "energy_consumers", "issue"}
    ):
        raise ValueError("Invalid thermal projection")
    for field in ("configuration_version", "revision"):
        if document[field] is None and field == "configuration_version":
            continue
        if not isinstance(document[field], str) or not HASH_PATTERN.fullmatch(document[field]):
            raise ValueError("Invalid thermal revision")
    devices = document["devices"]
    if not isinstance(devices, list) or len(devices) > MAX_THERMAL_DEVICES:
        raise ValueError("Invalid thermal device list")
    seen_ids, seen_meters = set(), set()
    normalized = []
    for device in devices:
        required_fields = {
            "id",
            "energy_entity",
            "temperature_entity",
            "power_entity",
            "minimum_c",
            "maximum_c",
            "heater_w",
            "enabled",
            "name",
            "actual",
            "issue",
            "planned",
        }
        physical_fields = {"volume_litres", "ambient_c", "loss_w_per_c", "draw_profile_kwh"}
        display_fields = {
            "ending_target_c",
            "ending_stock_shortfall_kwh",
            "comfort_shortfall_kwh",
            "unmet_draw_kwh",
        }
        if (
            not isinstance(device, dict)
            or not required_fields <= device.keys()
            or device.keys() - required_fields - physical_fields - display_fields
        ):
            raise ValueError("Invalid thermal device")
        if physical_fields & device.keys():
            if not physical_fields <= device.keys():
                raise ValueError("Incomplete thermal physical configuration")
            for field, (minimum, maximum) in THERMAL_CONFIGURATION_BOUNDS.items():
                number(device[field], minimum, maximum)
            if device["ambient_c"] > device["maximum_c"]:
                raise ValueError("Tank surroundings exceed the supported planning maximum")
            if not float(device["heater_w"]).is_integer():
                raise ValueError("Thermal heater power must use whole watts")
            profile = device["draw_profile_kwh"]
            if not isinstance(profile, list) or len(profile) != THERMAL_PROFILE_HOURS:
                raise ValueError("Invalid thermal demand profile")
            for quantity in profile:
                number(quantity, 0, MAX_THERMAL_HOURLY_DRAW_KWH)
            if sum(profile) <= 0:
                raise ValueError("Thermal demand profile must include heat demand")
        for field in display_fields & device.keys():
            if device[field] is not None:
                number(
                    device[field],
                    0,
                    MAX_TEMPERATURE_C if field == "ending_target_c" else MAX_THERMAL_ENERGY_KWH,
                )
        identity = identifier(device["id"])
        if identity in seen_ids or device["energy_entity"] in seen_meters:
            raise ValueError("Duplicate thermal identity or meter")
        seen_ids.add(identity)
        for field in ("energy_entity", "temperature_entity", "power_entity"):
            entity = identifier(device[field])
            pattern = (
                TEMPERATURE_ENTITY_PATTERN if field == "temperature_entity" else SENSOR_PATTERN
            )
            if not pattern.fullmatch(entity):
                raise ValueError("Invalid thermal sensor identity")
        seen_meters.add(device["energy_entity"])
        number(device["minimum_c"], MIN_TEMPERATURE_C, MAX_TEMPERATURE_C)
        number(device["maximum_c"], device["minimum_c"], MAX_TEMPERATURE_C)
        number(device["heater_w"], 1, MAX_THERMAL_POWER_W)
        if type(device["enabled"]) is not bool:
            raise ValueError("Invalid thermal enable state")
        for field in ("name", "issue"):
            value = device[field]
            if value is None and field == "issue":
                continue
            if not isinstance(value, str) or len(value) > MAX_THERMAL_TEXT_LENGTH:
                raise ValueError("Invalid thermal text")
        actual = device["actual"]
        if actual is not None:
            actual_fields = {
                "temperature_c",
                "power_w",
                "observed_at",
            }
            if (
                not isinstance(actual, dict)
                or not actual_fields <= actual.keys()
                or actual.keys() - actual_fields - {"id"}
            ):
                raise ValueError("Invalid thermal actuals")
            if "id" in actual and actual["id"] != identity:
                raise ValueError("Thermal actual identity differs from configured device")
            number(actual["temperature_c"], MIN_TEMPERATURE_C, MAX_TEMPERATURE_C)
            number(actual["power_w"], 0, MAX_THERMAL_POWER_W)
            timestamp(actual["observed_at"])
        planned = device["planned"]
        if not isinstance(planned, list) or len(planned) > MAX_THERMAL_STEPS:
            raise ValueError("Invalid thermal schedule")
        previous_end = None
        for step in planned:
            if not isinstance(step, dict) or set(step) != {
                "start",
                "end",
                "power_w",
                "end_c",
                "shortfall_kwh",
            }:
                raise ValueError("Invalid thermal step")
            start, end = timestamp(step["start"]), timestamp(step["end"])
            if start >= end or previous_end is not None and start != previous_end:
                raise ValueError("Invalid thermal interval order")
            previous_end = end
            number(step["power_w"], 0, device["heater_w"])
            if step["power_w"] not in (0, device["heater_w"]):
                raise ValueError("Thermal heating must use fixed power")
            number(step["end_c"], MIN_TEMPERATURE_C, device["maximum_c"])
            number(step["shortfall_kwh"], 0, MAX_THERMAL_ENERGY_KWH)
        accepted = {field: deepcopy(device[field]) for field in required_fields}
        if actual is not None:
            accepted["actual"] = {field: deepcopy(actual[field]) for field in actual_fields}
        normalized.append(accepted)
    return {
        "configuration_version": document["configuration_version"],
        "revision": document["revision"],
        "devices": normalized,
    }


def command(coordinator, device_id):
    """Return current heating permission only while plan, ownership and local evidence agree."""
    try:
        now = dt_util.utcnow()
        document = coordinator.thermal
        device = next(row for row in document["devices"] if row["id"] == device_id)
        owned = {row["entity_id"] for row in coordinator.consumer_forecasts.get("options", [])}
        plan = coordinator.schedule.document
        context = plan.get("thermal_context", {}) if plan else {}
        if (
            coordinator.closed
            or not device["enabled"]
            or device["energy_entity"] not in owned
            or not plan
            or context.get("revision") != document["revision"]
        ):
            return None
        frozen = next(row for row in context.get("devices", []) if row.get("id") == device_id)
        if any(
            frozen.get(field) != device[field]
            for field in (
                "id",
                "energy_entity",
                "temperature_entity",
                "power_entity",
                "minimum_c",
                "maximum_c",
                "heater_w",
                "enabled",
            )
        ):
            return None
        measured = reading(coordinator.hass, device, now)
        if measured is None or measured["temperature_c"] >= device["maximum_c"]:
            return None
        projection = coordinator.schedule.project(now)
        if projection.command is None:
            return None
        target = next(
            target
            for target in projection.command.devices
            if target.kind == "thermal" and target.device_id == device_id
        )
        if type(target.power_w) not in (int, float) or target.power_w not in (
            0,
            device["heater_w"],
        ):
            return None
        return target.power_w > 0
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration, OverflowError):
        return None


def register_entities(coordinator, async_add_entities, platform):
    """Reconcile virtual thermal entities without changing user-assigned names or source devices."""
    from .thermal_controls import ThermalEnabled, ThermalHeating, ThermalMinimum, ThermalSensor

    definitions = {
        "sensor": [
            (key, ThermalSensor)
            for key in (
                "temperature",
                "planned_power",
                "predicted_temperature",
                "comfort_shortfall",
            )
        ],
        "binary_sensor": [
            ("requested_heating", ThermalHeating),
            ("measured_heating", ThermalHeating),
        ],
        "number": [("minimum_c", ThermalMinimum)],
        "switch": [("enabled", ThermalEnabled)],
    }.get(platform, [])
    entities, removing = {}, set()
    hass, entry = coordinator.hass, coordinator.entry

    async def remove(key, entity):
        """Remove withdrawn virtual entities and only their now-empty virtual device."""
        registry = er.async_get(hass)
        registered = registry.async_get(entity.entity_id)
        device_id = registered.device_id if registered else None
        await entity.async_remove()
        if registry.async_get(entity.entity_id):
            registry.async_remove(entity.entity_id)
        if device_id and not er.async_entries_for_device(registry, device_id):
            devices = dr.async_get(hass)
            if devices.async_get(device_id):
                devices.async_remove_device(device_id)
        entities.pop(key, None)
        removing.discard(key)
        update()
        retire_empty_groups(coordinator)

    @callback
    def update():
        """Create stable app-owned entities and reconcile changed catalogue membership."""
        desired = {
            (row["id"], key): (row, factory)
            for row in getattr(coordinator, "thermal", {}).get("devices", [])
            for key, factory in definitions
        }
        additions = []
        for key, (row, factory) in desired.items():
            device = dr.async_get(hass).async_get_device(
                identifiers={(DOMAIN, entry.entry_id + "_thermal_" + row["id"])}
            )
            if device and device.name != row["name"]:
                dr.async_get(hass).async_update_device(device.id, name=row["name"])
            if key not in entities and key not in removing:
                entities[key] = factory(coordinator, row, key[1])
                additions.append(entities[key])
        if additions:
            add_grouped_entities(coordinator, async_add_entities, additions)
        for key, entity in list(entities.items()):
            if key not in desired and key not in removing:
                removing.add(key)
                entry.async_create_task(hass, remove(key, entity), "Remove thermal entity")

    entry.async_on_unload(coordinator.subscribe(update))
    update()
