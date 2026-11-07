"""Read-only HA setup metadata adapter. No wizard, persistence or planning."""

from collections import Counter
from math import isfinite

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .solar_adapters import solar_configuration_suggestion

MAX_CATALOGUE_TEXT_LENGTH = 160

MAX_ENTITY_ID_LENGTH = 127
MAX_BATTERY_CAPACITY_KWH = 1000000.0


async def energy_suggestions(hass):
    """Read Energy sources independently of current measurements and Recorder history.

    Count original provider associations before normalizing geometry. An invalid
    group must not make another meter's shared forecast appear uniquely assigned.
    """
    result = {
        "terms": [],
        "soc": [],
        "grid_power": None,
        "grid_power_entities": [],
        "solar": False,
        "solar_sources": [],
        "solar_configurations": [],
        "energy_status": "missing",
        "batteries": [],
        "consumers": [],
        "grid_export": None,
        "settlement": {"imports": [], "exports": []},
    }
    from homeassistant.components.energy.data import async_get_manager

    manager = await async_get_manager(hass)
    if not (manager.data or {}).get("energy_sources"):
        return result
    result["energy_status"] = "incomplete"

    def sensor_id(entity):
        """Keep bounded sensor identities. Unusable Energy links remain explicitly missing in setup."""
        return (
            entity
            if isinstance(entity, str)
            and entity.startswith("sensor.")
            and len(entity) <= MAX_ENTITY_ID_LENGTH
            else None
        )

    def term(entity, sign):
        """Retain every configured sensor channel. Unit validation belongs to setup preview and measurements."""
        if (
            isinstance(entity, str)
            and entity.startswith("sensor.")
            and len(entity) <= MAX_ENTITY_ID_LENGTH
        ):
            result["terms"].append({"entity_id": entity, "sign": sign})
            return True
        return False

    def configured_power(source):
        """Reuse HA's normalized signed-power entity, including its generated invert/combine sensor."""
        entity = source.get("stat_rate") or source.get("power_config", {}).get("stat_rate")
        return (
            entity
            if isinstance(entity, str)
            and entity.startswith("sensor.")
            and len(entity) <= MAX_ENTITY_ID_LENGTH
            else None
        )

    complete = True
    consumer_entities = set()
    for consumer in (manager.data or {}).get("device_consumption", []):
        entity = sensor_id(consumer.get("stat_consumption"))
        if entity and entity not in consumer_entities:
            consumer_entities.add(entity)
            result["consumers"].append(
                {
                    "entity_id": entity,
                    "upstream": sensor_id(consumer.get("included_in_stat")),
                    **({"power_entity": power} if (power := configured_power(consumer)) else {}),
                }
            )
    imports = exports = 0
    selected_forecasts = {}
    export_channels = []
    explicit_power_channels = set()
    storage = False
    for source in (manager.data or {}).get("energy_sources", []):
        kind = source.get("type")
        if kind == "grid":
            connection_power = set()
            for power_source in (source, *source.get("power", [])):
                if power := configured_power(power_source):
                    connection_power.add(power)
            explicit_power_channels.update(connection_power)
            # Current HA stores one import/export pair per source. Older configurations
            # group routes. Prefer the current shape without counting either channel twice.
            incoming = (
                [{"stat_energy_from": source["stat_energy_from"]}]
                if source.get("stat_energy_from") is not None
                else source.get("flow_from", [])
            )
            outgoing = (
                [{"stat_energy_to": source["stat_energy_to"]}]
                if source.get("stat_energy_to") is not None
                else source.get("flow_to", [])
            )
            complete &= bool(incoming)
            for route in incoming:
                entity = route.get("stat_energy_from")
                valid = term(entity, 1)
                complete &= valid
                imports += int(valid)
                if valid:
                    result["settlement"]["imports"].append(entity)
            for route in outgoing:
                valid = term(route.get("stat_energy_to"), -1)
                complete &= valid
                exports += int(valid)
                if valid:
                    export_channels.append(route["stat_energy_to"])
                    result["settlement"]["exports"].append(route["stat_energy_to"])
        elif kind == "solar":
            result["solar"] = True
            entity = sensor_id(source.get("stat_energy_from"))
            selected_entry_ids = source.get("config_entry_solar_forecast")
            if entity and isinstance(selected_entry_ids, list):
                selected_forecasts.setdefault(entity, set()).update(
                    entry_id for entry_id in selected_entry_ids if isinstance(entry_id, str)
                )
            if source.get("stat_energy_from") in result["solar_sources"]:
                complete = False
                matching = next(
                    row
                    for row in result["solar_configurations"]
                    if row["entity_id"] == source["stat_energy_from"]
                )
                matching.update(providers=[], issue="solar_configuration_incomplete")
                continue
            valid = term(source.get("stat_energy_from"), 1)
            complete &= valid
            if valid:
                result["solar_sources"].append(source["stat_energy_from"])
                result["solar_configurations"].append(solar_configuration_suggestion(hass, source))
        elif kind == "battery":
            storage = True
            complete &= term(source.get("stat_energy_from"), 1)
            complete &= term(source.get("stat_energy_to"), -1)
            if (
                isinstance(source.get("stat_energy_from"), str)
                and len(source["stat_energy_from"]) <= MAX_ENTITY_ID_LENGTH
            ):
                soc = sensor_id(source.get("stat_soc"))
                capacity = source.get("capacity")
                if (
                    type(capacity) not in (int, float)
                    or not isfinite(capacity)
                    or not 0 < capacity <= MAX_BATTERY_CAPACITY_KWH
                ):
                    capacity = None
                if soc:
                    result["soc"].append(soc)
                result["batteries"].append(
                    {
                        "energy_entity": source["stat_energy_from"],
                        "charge_energy_entity": sensor_id(source.get("stat_energy_to")),
                        "capacity_kwh": capacity,
                        "soc_entity": soc,
                        "power_entity": configured_power(source),
                    }
                )
    assignments = Counter(
        entry_id
        for selected_entry_ids in selected_forecasts.values()
        for entry_id in selected_entry_ids
    )
    for suggestion in result["solar_configurations"]:
        if any(
            assignments[entry_id] > 1
            for entry_id in selected_forecasts.get(suggestion["entity_id"], ())
        ):
            suggestion["providers"] = []
            suggestion["issue"] = "solar_configuration_ambiguous"
    selected_power = explicit_power_channels
    result["grid_power_entities"] = sorted(selected_power)
    result["grid_power"] = next(iter(selected_power)) if len(selected_power) == 1 else None
    result["grid_export"] = export_channels[0] if export_channels else None
    if (
        not complete
        or not imports
        or ((result["solar"] or storage) and not exports)
        or len({energy_term["entity_id"] for energy_term in result["terms"]})
        != len(result["terms"])
    ):
        result["terms"] = []
    else:
        result["energy_status"] = "ready"
    if not result["terms"]:
        result["settlement"] = {"imports": [], "exports": []}
    return result


async def setup_catalogue(hass):
    """Publish bounded sensor metadata and Energy hints, without arbitrary attributes.

    Country is optional: None when unset, otherwise HA's two-letter code. The
    app uses it only as a bidding-area hint. This integration never infers an area.
    Entity metadata retains preferred-first ordering without a count cutoff.
    The catalogue serializer owns bounded pages for the complete snapshot,
    including compact history and suggestions. It marks an optional solar
    configuration incomplete when its full group cannot fit a transfer page.
    """
    registry = er.async_get(hass)
    suggestions = await energy_suggestions(hass)
    from homeassistant.components.energy.data import async_get_manager

    manager = await async_get_manager(hass)
    energy_names = {}
    for source in (
        *(manager.data or {}).get("energy_sources", ()),
        *(manager.data or {}).get("device_consumption", ()),
        *(manager.data or {}).get("device_consumption_water", ()),
    ):
        name = source.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        for key in (
            "stat_energy_from",
            "stat_energy_to",
            "stat_consumption",
            "stat_rate",
            "stat_soc",
        ):
            entity = source.get(key)
            if isinstance(entity, str):
                label = name.strip()[:MAX_CATALOGUE_TEXT_LENGTH]
                energy_names[entity] = label if energy_names.get(entity, label) == label else ""
    preferred = {term["entity_id"] for term in suggestions["terms"]}
    preferred.update(suggestions["soc"])
    preferred.add(suggestions["grid_power"])
    preferred.update(suggestions["grid_power_entities"])
    preferred.update(suggestions["solar_sources"])
    preferred.update(item["entity_id"] for item in suggestions["consumers"])
    preferred.update(
        item["power_entity"] for item in suggestions["consumers"] if item.get("power_entity")
    )
    for battery in suggestions["batteries"]:
        preferred.update(value for value in battery.values() if isinstance(value, str))
    # Keep connection sensors on Energy devices ahead of unrelated household sensors.
    preferred_devices = {
        entry.device_id
        for entity_id in preferred
        if (entry := registry.async_get(entity_id)) and entry.device_id
    }
    states = [
        *hass.states.async_all("sensor"),
        *hass.states.async_all("binary_sensor"),
        *hass.states.async_all("water_heater"),
    ]
    present = {state.entity_id for state in states}
    related = {
        state.entity_id
        for state in states
        if (entry := registry.async_get(state.entity_id)) and entry.device_id in preferred_devices
    }
    # These metadata-only placeholders never enter HA's measurement state machine.
    states.extend(
        State(entity_id, STATE_UNKNOWN)
        for entity_id in preferred - present
        if entity_id and entity_id.startswith("sensor.")
    )
    entities = []
    for state in sorted(
        states,
        key=lambda sensor_state: (
            sensor_state.entity_id not in preferred,
            sensor_state.entity_id not in related,
            sensor_state.entity_id.startswith("binary_sensor."),
            sensor_state.entity_id,
        ),
    ):
        if len(state.entity_id) > MAX_ENTITY_ID_LENGTH:
            continue
        entry = registry.async_get(state.entity_id)
        if entry and entry.platform == "open_energy_planner":
            continue
        physical = (
            dr.async_get(hass).async_get(entry.device_id) if entry and entry.device_id else None
        )
        integration = (
            hass.config_entries.async_get_entry(entry.config_entry_id)
            if entry and entry.config_entry_id
            else None
        )
        entities.append(
            {
                "entity_id": state.entity_id,
                "name": str(state.name)[:MAX_CATALOGUE_TEXT_LENGTH],
                **(
                    {"energy_name": energy_names[state.entity_id]}
                    if energy_names.get(state.entity_id)
                    else {}
                ),
                "device_name": str(
                    (physical.name_by_user or physical.name or state.name)
                    if physical
                    else state.name
                )[:MAX_CATALOGUE_TEXT_LENGTH],
                "unit": str(
                    hass.config.units.temperature_unit
                    if state.domain == "water_heater"
                    else state.attributes.get("unit_of_measurement", "")
                )[:MAX_CATALOGUE_TEXT_LENGTH],
                "device_class": str(
                    "temperature"
                    if state.domain == "water_heater"
                    else state.attributes.get("device_class", "")
                )[:MAX_CATALOGUE_TEXT_LENGTH],
                "state_class": str(state.attributes.get("state_class", ""))[
                    :MAX_CATALOGUE_TEXT_LENGTH
                ],
                "device_id": entry.device_id if entry and entry.device_id else "",
                **(
                    {
                        "integration_id": integration.entry_id,
                        "integration_name": integration.title[:MAX_CATALOGUE_TEXT_LENGTH],
                    }
                    if integration
                    else {}
                ),
            }
        )
    from .setup_history import setup_history

    body = {
        "entities": entities,
        "suggestions": suggestions,
        "timezone": hass.config.time_zone,
        "country": hass.config.country,
        "history": await setup_history(hass, suggestions["terms"]) if hass.is_running else [],
    }
    return body
