"""Bounded thermal readings and virtual permissions. Physical actuation remains in HA automations."""

import re
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from math import isfinite

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN, projection_id
from .protocol import (
    MAX_THERMAL_HOURLY_DRAW_KWH,
    THERMAL_CONFIGURATION_BOUNDS,
    THERMAL_PROFILE_HOURS,
    identifier,
    timestamp,
)
from .subentries import add_grouped_entities, retire_empty_groups
from .wire_limits import MAX_API_RESPONSE_BYTES, validate_projection_size

MAX_THERMAL_STEPS = 1024
MAX_THERMAL_POWER_W = 30000
MAX_THERMAL_ENERGY_KWH = 10000
MAX_THERMAL_TEXT_LENGTH = 160
MIN_TEMPERATURE_C = 0
MAX_TEMPERATURE_C = 120
MAX_SPACE_TEMPERATURE_C = 50
SPACE_TARGET_TOLERANCE_C = 0.01
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
    body = {
        "configuration_version": projection.get("configuration_version"),
        "at": now.isoformat(),
        "readings": readings,
    }
    if applications := projection.get("climate_bindings"):
        body["climate"] = [
            observed
            for application in applications
            if (observed := read_climate(coordinator.hass, application, now)) is not None
        ]
    return body


def read_climate(hass, application, now):
    """Capture current temperature and a single active setpoint without assuming a schedule."""
    entity_id = application["climate_entity"]
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", "off"):
        return None
    try:
        observed = state.last_reported
        if observed.utcoffset() is None or observed > now:
            return None
        values = {}
        for field, attribute in (
            ("temperature_c", "current_temperature"),
            ("target_c", "temperature"),
        ):
            value = state.attributes[attribute]
            if type(value) not in (float, int):
                return None
            if hass.config.units.temperature_unit == "°F":
                value = (value - 32) * 5 / 9
            elif hass.config.units.temperature_unit != "°C":
                return None
            values[field] = number(value, MIN_TEMPERATURE_C, MAX_SPACE_TEMPERATURE_C)
        return {
            "id": application["id"],
            "entity_id": entity_id,
            "observed_at": observed.isoformat(),
            "heat_mode": state.state == "heat",
            **values,
        }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


def _validate_projection_header(document):
    """Require the known projection fields and a revision, with a nullable configuration version."""
    required = {"configuration_version", "revision", "devices"}
    if (
        not isinstance(document, dict)
        or not required <= document.keys()
        or document.keys()
        - required
        - {
            "configuration",
            "catalogue",
            "energy_consumers",
            "issue",
            "applications",
            "shared_sources",
            "source_modes",
        }
    ):
        raise ValueError("Invalid thermal projection")
    version = document["configuration_version"]
    if version is not None:
        _validate_revision(version)
    _validate_revision(document["revision"])


def _validate_revision(value):
    """Reject values other than a lowercase, 64-character hexadecimal revision."""
    if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
        raise ValueError("Invalid thermal revision")


def receive(document):
    """Validate bounded app metadata and plans before creating entities or exposing control."""
    validate_projection_size(document, MAX_API_RESPONSE_BYTES)
    _validate_projection_header(document)
    normalized = receive_boiler_devices(document["devices"])
    shared_sources = receive_shared_sources(
        document.get("shared_sources", {}), document.get("source_modes", {})
    )
    climate_bindings = receive_climate_bindings(document.get("applications", []))
    return {
        "configuration_version": document["configuration_version"],
        "revision": document["revision"],
        "devices": normalized,
        **({"shared_sources": shared_sources} if shared_sources else {}),
        **({"climate_bindings": climate_bindings} if climate_bindings else {}),
    }


def receive_boiler_devices(devices):
    """Validate each projected boiler before exposing its measurements or plan."""
    if not isinstance(devices, list):
        raise TypeError("Invalid thermal device list")
    seen_ids, seen_meters = set(), set()
    return [_receive_boiler_device(device, seen_ids, seen_meters) for device in devices]


def _receive_boiler_device(device, seen_ids, seen_meters):
    """Validate and normalize one boiler projection."""
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
    validate_boiler_details(device, physical_fields, display_fields)
    identity = identifier(device["id"])
    if identity in seen_ids or device["energy_entity"] in seen_meters:
        raise ValueError("Duplicate thermal identity or meter")
    seen_ids.add(identity)
    _validate_boiler_sensors(device)
    seen_meters.add(device["energy_entity"])
    number(device["minimum_c"], MIN_TEMPERATURE_C, MAX_TEMPERATURE_C)
    number(device["maximum_c"], device["minimum_c"], MAX_TEMPERATURE_C)
    number(device["heater_w"], 1, MAX_THERMAL_POWER_W)
    if type(device["enabled"]) is not bool:
        raise ValueError("Invalid thermal enable state")
    _validate_boiler_text(device)
    actual = receive_boiler_actual(device["actual"], identity)
    validate_boiler_schedule(device)
    accepted = {field: deepcopy(device[field]) for field in required_fields}
    if actual is not None:
        accepted["actual"] = actual
    return accepted


def _validate_boiler_sensors(device):
    """Check the allowed Home Assistant entity domain for each boiler input."""
    for field in ("energy_entity", "temperature_entity", "power_entity"):
        entity = identifier(device[field])
        pattern = TEMPERATURE_ENTITY_PATTERN if field == "temperature_entity" else SENSOR_PATTERN
        if not pattern.fullmatch(entity):
            raise ValueError("Invalid thermal sensor identity")


def _validate_boiler_text(device):
    """Check the display name and optional readiness message."""
    for field in ("name", "issue"):
        value = device[field]
        if value is None and field == "issue":
            continue
        if not isinstance(value, str) or len(value) > MAX_THERMAL_TEXT_LENGTH:
            raise ValueError("Invalid thermal text")


def validate_boiler_details(device, physical_fields, display_fields):
    """Check optional boiler physics and display summaries."""
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


def receive_boiler_actual(actual, identity):
    """Keep only validated measurement fields from a boiler observation."""
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
        return {field: deepcopy(actual[field]) for field in actual_fields}
    return None


def validate_boiler_schedule(device):
    """Check chronological order, fixed power and temperatures of scheduled intervals."""
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


def receive_shared_sources(shared, modes):
    """Validate shared Energy meters and their possible and reported modes."""
    if not isinstance(shared, dict) or not isinstance(modes, dict):
        raise TypeError("Invalid thermal source modes")
    return [_receive_shared_source(meter, source, modes) for meter, source in shared.items()]


def _receive_shared_source(meter, source, modes):
    """Validate one shared meter and the current reported mode."""
    if (
        not SENSOR_PATTERN.fullmatch(identifier(meter))
        or not isinstance(source, dict)
        or set(source) != {"meter", "name", "options"}
        or source["meter"] != meter
    ):
        raise ValueError("Invalid shared thermal source")
    name, rows = source["name"], source["options"]
    if not isinstance(name, str) or len(name) > MAX_THERMAL_TEXT_LENGTH:
        raise ValueError("Invalid shared thermal source")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("Invalid shared thermal source")
    options = receive_mode_options(rows)
    mode = _receive_reported_mode(modes.get(meter), options)
    return {
        "id": "source_" + sha256(meter.encode()).hexdigest(),
        "meter": meter,
        "name": source["name"],
        "options": options,
        "mode": deepcopy(mode),
    }


def _receive_reported_mode(mode, options):
    """Accept no report or a timestamped report naming an available mode."""
    if mode is not None:
        if not isinstance(mode, dict) or set(mode) != {"application_id", "observed_at"}:
            raise ValueError("Invalid reported thermal mode")
        timestamp(mode["observed_at"])
        if mode["application_id"] not in options:
            raise ValueError("Unknown reported thermal mode")
    return mode


def receive_mode_options(rows):
    """Keep the stable identities and bounded display names of a shared source's modes."""
    options = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "name"}
            or not isinstance(row["name"], str)
            or len(row["name"]) > MAX_THERMAL_TEXT_LENGTH
        ):
            raise ValueError("Invalid thermal mode option")
        option_id = identifier(row["id"])
        if option_id in options:
            raise ValueError("Duplicate thermal mode option")
        options[option_id] = row["name"]
    return options


def receive_climate_bindings(applications):
    """Extract the climate bindings admitted by the application projection."""
    if not isinstance(applications, list):
        raise TypeError("Invalid thermal applications")
    climate_bindings = []
    for application in applications:
        if not isinstance(application, dict):
            raise TypeError("Invalid thermal application")
        if application.get("kind") != "space_heating" or application.get("climate_entity") is None:
            continue
        identity = identifier(application.get("id"))
        climate = identifier(application["climate_entity"])
        if not re.fullmatch(r"climate\.[a-z0-9_]+", climate):
            raise ValueError("Invalid climate entity")
        binding = {"id": identity, "climate_entity": climate}
        binding.update(_receive_room_scheduling(application))
        climate_bindings.append(binding)
    return climate_bindings


def _receive_room_scheduling(application):
    """Validate the additional fields required for active room scheduling."""
    sources = application.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
        raise TypeError("Invalid room heat sources")
    if len(sources) != 1 or sources[0].get("allow_scheduling") is not True:
        return {}
    meter = identifier(sources[0].get("energy_entity"))
    name = application.get("name")
    if (
        not SENSOR_PATTERN.fullmatch(meter)
        or not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_THERMAL_TEXT_LENGTH
    ):
        raise ValueError("Invalid room energy meter")
    return {
        "name": name,
        "energy_entity": meter,
        "maximum_preheat_c": number(application["maximum_preheat_c"], 0, 3),
    }


def command(coordinator, device_id):
    """Return current heating permission only while plan, ownership and local evidence agree."""
    try:
        if any(row["id"] == device_id for row in coordinator.thermal.get("climate_bindings", [])):
            room = room_command(coordinator, device_id)
            return room[0] if room else None
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
        power = _requested_thermal_power(
            coordinator.schedule.project(now), device_id, device["heater_w"]
        )
        return power > 0 if power is not None else None
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration, OverflowError):
        return None


def room_command(coordinator, identity):
    """Resolve a confirmed room request only while the original HA thermostat target is current."""
    try:
        document = coordinator.thermal
        binding = next(row for row in document.get("climate_bindings", []) if row["id"] == identity)
        plan = coordinator.schedule.document
        context = plan.get("thermal_context", {}) if plan else {}
        if coordinator.closed or context.get("revision") != document["revision"]:
            return None
        frozen = next(row for row in context["devices"] if row["id"] == identity)
        if frozen.get("kind") != "space_heating" or any(
            frozen.get(field) != binding.get(field)
            for field in ("energy_entity", "climate_entity", "maximum_preheat_c")
        ):
            return None
        meter = binding["energy_entity"]
        if meter not in {
            row["entity_id"] for row in coordinator.consumer_forecasts.get("options", [])
        } or any(source["meter"] == meter for source in document.get("shared_sources", [])):
            return None
        now = dt_util.utcnow()
        if now < timestamp(frozen["ready_at"]):
            return None
        reading = read_climate(coordinator.hass, binding, now)
        limit = frozen["target_c"] + frozen["maximum_preheat_c"]
        if not _room_reading_allows_request(reading, frozen["target_c"], limit):
            return None
        power = _requested_thermal_power(
            coordinator.schedule.project(now), identity, frozen["heater_w"]
        )
        if power is None or (power == 0 and reading["temperature_c"] < reading["target_c"]):
            return None
        return power > 0, limit
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration, OverflowError):
        return None


def _requested_thermal_power(projection, identity, heater_w):
    """Read the fixed heater target from the current command, if present and valid."""
    if projection.command is None:
        return None
    for target in projection.command.devices:
        if target.kind == "thermal" and target.device_id == identity:
            power = target.power_w
            return power if type(power) in (int, float) and power in (0, heater_w) else None
    return None


def _room_reading_allows_request(reading, original_target, limit):
    """Require heating mode, an unchanged thermostat target and room below the ceiling."""
    return (
        reading
        and reading["heat_mode"]
        and abs(reading["target_c"] - original_target) < SPACE_TARGET_TOLERANCE_C
        and reading["temperature_c"] < limit
    )


def register_entities(coordinator, async_add_entities, platform):
    """Reconcile virtual thermal entities without changing user-assigned names or source devices."""
    from .thermal_controls import (
        RoomHeatRequested,
        RoomPreheatTarget,
        ThermalEnabled,
        ThermalHeating,
        ThermalMinimum,
        ThermalOperatingMode,
        ThermalSensor,
    )

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

    def desired_entities():
        """Collect the eligible virtual entities for this platform."""
        desired = {
            (row["id"], key): (row, factory)
            for row in getattr(coordinator, "thermal", {}).get("devices", [])
            for key, factory in definitions
        }
        if platform == "select":
            desired.update(
                {
                    (row["id"], "operating_mode"): (row, ThermalOperatingMode)
                    for row in getattr(coordinator, "thermal", {}).get("shared_sources", [])
                }
            )
        if platform == "sensor":
            desired.update(
                {
                    (row["id"], "preheat_target"): (row, RoomPreheatTarget)
                    for row in getattr(coordinator, "thermal", {}).get("climate_bindings", [])
                    if "energy_entity" in row
                }
            )
        if platform == "binary_sensor":
            desired.update(
                {
                    (row["id"], "room_heat_requested"): (row, RoomHeatRequested)
                    for row in getattr(coordinator, "thermal", {}).get("climate_bindings", [])
                    if "energy_entity" in row
                }
            )
        return desired

    @callback
    def update():
        """Create stable app-owned entities and reconcile changed catalogue membership."""
        desired = desired_entities()
        additions = []
        for key, (row, factory) in desired.items():
            device = dr.async_get(hass).async_get_device(
                identifiers={(DOMAIN, projection_id(entry) + "_thermal_" + row["id"])}
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
