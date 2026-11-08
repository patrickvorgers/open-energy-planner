"""Read-only HA setup metadata adapter. No wizard, persistence or planning."""

from collections import Counter
from math import isfinite

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration

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


def energy_display_names(preferences):
    """Project explicit Energy names without forwarding unrelated preference fields."""
    energy_names = {}
    for source in (
        *(preferences or {}).get("energy_sources", ()),
        *(preferences or {}).get("device_consumption", ()),
        *(preferences or {}).get("device_consumption_water", ()),
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
    return energy_names


def preferred_entities(suggestions):
    """Identify configured Energy sources and their related measurement channels."""
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
    return preferred - {None}


def energy_entities(preferences):
    """Retain explicit Energy bindings even when an incomplete balance cannot produce suggestions."""
    identities = set()
    for source in (
        *(preferences or {}).get("energy_sources", ()),
        *(preferences or {}).get("device_consumption", ()),
    ):
        for route in (
            source,
            source.get("power_config", {}),
            *source.get("power", ()),
            *source.get("flow_from", ()),
            *source.get("flow_to", ()),
        ):
            for field in (
                "stat_energy_from",
                "stat_energy_to",
                "stat_consumption",
                "stat_rate",
                "stat_soc",
                "included_in_stat",
            ):
                identity = route.get(field)
                if (
                    isinstance(identity, str)
                    and identity.startswith("sensor.")
                    and len(identity) <= MAX_ENTITY_ID_LENGTH
                ):
                    identities.add(identity)
    return identities


def ordered_entities(hass, entities, suggestions):
    """Retain deterministic Energy-first order without truncating ordinary sources."""
    registry = er.async_get(hass)
    preferred = preferred_entities(suggestions)
    # Keep connection sensors on Energy devices ahead of unrelated household sensors.
    preferred_devices = {
        entry.device_id
        for entity_id in preferred
        if (entry := registry.async_get(entity_id)) and entry.device_id
    }
    return sorted(
        entities,
        key=lambda entity: (
            entity["entity_id"] not in preferred,
            entity["device_id"] not in preferred_devices,
            entity["entity_id"].startswith("binary_sensor."),
            entity["entity_id"],
        ),
    )


def integration_metadata(hass, entry, integration):
    """Project the loaded integration's public name separately from its configured account title.

    Registry-only helpers may lack a loaded manifest. Their platform remains an
    explicit type identity, without loading integrations or exposing provider data.
    """
    if entry is None:
        return {}
    domain = integration.domain if integration else entry.platform
    try:
        name = async_get_loaded_integration(hass, domain).name
    except (IntegrationNotLoaded, KeyError):
        name = domain.replace("_", " ").capitalize()
    return {
        "integration_domain": domain[:MAX_CATALOGUE_TEXT_LENGTH],
        "integration_display_name": name[:MAX_CATALOGUE_TEXT_LENGTH],
    }


def catalogue_entity(hass, state, energy_names):
    """Read one sanitized metadata projection on HA's event loop, excluding owned virtual devices."""
    if len(state.entity_id) > MAX_ENTITY_ID_LENGTH:
        return None
    entry = er.async_get(hass).async_get(state.entity_id)
    if entry and entry.platform == "open_energy_planner":
        return None
    physical = dr.async_get(hass).async_get(entry.device_id) if entry and entry.device_id else None
    integration = (
        hass.config_entries.async_get_entry(entry.config_entry_id)
        if entry and entry.config_entry_id
        else None
    )
    return {
        "entity_id": state.entity_id,
        "name": str(state.name)[:MAX_CATALOGUE_TEXT_LENGTH],
        **(
            {"energy_name": energy_names[state.entity_id]}
            if energy_names.get(state.entity_id)
            else {}
        ),
        "device_name": str(
            (physical.name_by_user or physical.name or state.name) if physical else state.name
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
        "state_class": str(state.attributes.get("state_class", ""))[:MAX_CATALOGUE_TEXT_LENGTH],
        "device_id": entry.device_id if entry and entry.device_id else "",
        **(
            {"device_entry_type": physical.entry_type.value}
            if physical and physical.entry_type is not None
            else {}
        ),
        **integration_metadata(hass, entry, integration),
        **(
            {
                "integration_id": integration.entry_id,
                "integration_name": integration.title[:MAX_CATALOGUE_TEXT_LENGTH],
            }
            if integration
            else {}
        ),
    }


async def setup_catalogue(hass, selected=None, *, include_history=True):
    """Capture selected active sources or all supported metadata for a deliberate candidate query.

    Country remains an optional bidding-area hint. Complete bounded transfer,
    including explicit incomplete optional geometry, belongs to the serializer.
    """
    from homeassistant.components.energy.data import async_get_manager

    suggestions = await energy_suggestions(hass)
    manager = await async_get_manager(hass)
    names = energy_display_names(manager.data)
    states = (
        [
            *hass.states.async_all("sensor"),
            *hass.states.async_all("binary_sensor"),
            *hass.states.async_all("water_heater"),
        ]
        if selected is None
        else [state for identity in selected if (state := hass.states.get(identity)) is not None]
    )
    present = {state.entity_id for state in states}
    # These placeholders never enter HA's measurement state machine.
    states.extend(
        State(entity_id, STATE_UNKNOWN)
        for entity_id in (preferred_entities(suggestions) if selected is None else selected)
        - present
        if entity_id.startswith(("sensor.", "binary_sensor.", "water_heater."))
    )
    entities = [
        metadata
        for state in states
        if (metadata := catalogue_entity(hass, state, names)) is not None
    ]
    from .setup_history import setup_history

    body = {
        "entities": ordered_entities(hass, entities, suggestions),
        "suggestions": suggestions,
        "timezone": hass.config.time_zone,
        "country": hass.config.country,
        "history": await setup_history(hass, suggestions["terms"])
        if include_history and hass.is_running
        else [],
    }
    return body


async def source_choices(hass, scope):
    """Capture a complete finite chooser scope without retaining unrelated metadata or reading history."""
    if scope not in ("ev", "thermal"):
        raise ValueError("Unsupported source choice scope")
    domains = ("sensor", "binary_sensor") if scope == "ev" else ("sensor", "water_heater")
    identities = set()
    for domain in domains:
        for state in hass.states.async_all(domain):
            kind = state.attributes.get("device_class")
            unit = state.attributes.get("unit_of_measurement")
            if (
                domain != "sensor"
                or kind == "power"
                and unit in ("W", "kW")
                or (scope == "ev" and unit == "%" and kind in (None, "", "battery"))
                or (scope == "thermal" and kind == "temperature" and unit in ("°C", "°F"))
            ):
                identities.add(state.entity_id)
    return await setup_catalogue(hass, selected=identities, include_history=False)
