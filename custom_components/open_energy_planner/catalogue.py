"""Read-only HA setup metadata adapter. No wizard, persistence or planning."""

from collections import Counter
from math import isfinite

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration

from .solar_adapters import solar_configuration_suggestion

MAX_CATALOGUE_TEXT_LENGTH = 160

MAX_ENTITY_ID_LENGTH = 127
MAX_BATTERY_CAPACITY_KWH = 1000000.0


def _sensor_id(entity):
    """Keep bounded sensor identities without requiring state or Recorder history."""
    if (
        isinstance(entity, str)
        and entity.startswith("sensor.")
        and len(entity) <= MAX_ENTITY_ID_LENGTH
    ):
        return entity
    return None


def _add_energy_term(terms, entity, sign):
    """Keep configured Energy channels regardless of their current units or state."""
    if _sensor_id(entity) is None:
        return False
    terms.append({"entity_id": entity, "sign": sign})
    return True


def _configured_power(source):
    """Reuse HA's normalized signed-power entity, including generated invert/combine sensors."""
    return _sensor_id(source.get("stat_rate") or source.get("power_config", {}).get("stat_rate"))


def _grid_suggestions(source, terms):
    """Read the signed power channels and admit each configured import/export term once."""
    connection_power = {
        power
        for power_source in (source, *source.get("power", []))
        if (power := _configured_power(power_source))
    }
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
    complete = bool(incoming)
    imports, exports = [], []
    for routes, field, sign, collected in (
        (incoming, "stat_energy_from", 1, imports),
        (outgoing, "stat_energy_to", -1, exports),
    ):
        for route in routes:
            entity = route.get(field)
            valid = _add_energy_term(terms, entity, sign)
            complete &= valid
            if valid:
                collected.append(entity)
    return complete, imports, exports, connection_power


def _consumer_suggestions(preferences):
    """List each configured consumption meter once and retain its upstream and power links."""
    consumers = []
    seen = set()
    for source in preferences.get("device_consumption", []):
        entity = _sensor_id(source.get("stat_consumption"))
        if not entity or entity in seen:
            continue
        seen.add(entity)
        consumer = {
            "entity_id": entity,
            "upstream": _sensor_id(source.get("included_in_stat")),
        }
        if power := _configured_power(source):
            consumer["power_entity"] = power
        consumers.append(consumer)
    return consumers


def _add_solar_suggestion(hass, source, result, selected_forecasts):
    """Record raw provider ownership before evaluating the meter and normalized geometry."""
    result["solar"] = True
    entity = _sensor_id(source.get("stat_energy_from"))
    if entity is None:
        return False
    selected_entry_ids = source.get("config_entry_solar_forecast")
    if isinstance(selected_entry_ids, list):
        selected_forecasts.setdefault(entity, set()).update(
            entry_id for entry_id in selected_entry_ids if isinstance(entry_id, str)
        )
    if entity in result["solar_sources"]:
        matching = next(row for row in result["solar_configurations"] if row["entity_id"] == entity)
        matching.update(providers=[], issue="solar_configuration_incomplete")
        return False
    _add_energy_term(result["terms"], entity, 1)
    result["solar_sources"].append(entity)
    result["solar_configurations"].append(solar_configuration_suggestion(hass, source))
    return True


def _invalidate_shared_solar_providers(suggestions, selected_forecasts):
    """Mark every meter with a shared provider as ambiguous, even if another geometry is invalid."""
    assignments = Counter(
        entry_id
        for selected_entry_ids in selected_forecasts.values()
        for entry_id in selected_entry_ids
    )
    for suggestion in suggestions:
        if any(
            assignments[entry_id] > 1
            for entry_id in selected_forecasts.get(suggestion["entity_id"], ())
        ):
            suggestion.update(providers=[], issue="solar_configuration_ambiguous")


def _battery_capacity(capacity):
    """Return the declared finite battery capacity in kWh if this setting can be used."""
    if (
        type(capacity) not in (int, float)
        or not isfinite(capacity)
        or not 0 < capacity <= MAX_BATTERY_CAPACITY_KWH
    ):
        return None
    return capacity


def _add_battery_suggestion(source, result):
    """Keep readable battery setup metadata even when one Energy meter is unusable."""
    discharge = source.get("stat_energy_from")
    charge = source.get("stat_energy_to")
    valid_discharge = _add_energy_term(result["terms"], discharge, 1)
    valid_charge = _add_energy_term(result["terms"], charge, -1)
    if isinstance(discharge, str) and len(discharge) <= MAX_ENTITY_ID_LENGTH:
        soc = _sensor_id(source.get("stat_soc"))
        capacity = _battery_capacity(source.get("capacity"))
        if soc:
            result["soc"].append(soc)
        result["batteries"].append(
            {
                "energy_entity": discharge,
                "charge_energy_entity": _sensor_id(charge),
                "capacity_kwh": capacity,
                "soc_entity": soc,
                "power_entity": _configured_power(source),
            }
        )
    return valid_discharge and valid_charge


def _complete_energy_balance(result, complete, requires_export):
    """Require every signed channel and disallow counting one meter in multiple roles."""
    grid = result["settlement"]
    if not complete or not grid["imports"]:
        return False
    if requires_export and not grid["exports"]:
        return False
    terms = result["terms"]
    return len({term["entity_id"] for term in terms}) == len(terms)


def _finish_energy_suggestions(result, complete, grid_power, storage):
    """Publish balance terms only for complete Energy metadata, retaining setup hints."""
    grid = result["settlement"]
    result["grid_power_entities"] = sorted(grid_power)
    result["grid_power"] = next(iter(grid_power)) if len(grid_power) == 1 else None
    result["grid_export"] = grid["exports"][0] if grid["exports"] else None
    if _complete_energy_balance(result, complete, result["solar"] or storage):
        result["energy_status"] = "ready"
    else:
        result["terms"] = []
    if not result["terms"]:
        result["settlement"] = {"imports": [], "exports": []}


async def energy_suggestions(hass):
    """Read Energy sources independently of current measurements and Recorder history."""
    from homeassistant.components.energy.data import async_get_manager

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
    manager = await async_get_manager(hass)
    preferences = manager.data or {}
    sources = preferences.get("energy_sources")
    if not sources:
        return result
    result["energy_status"] = "incomplete"
    result["consumers"] = _consumer_suggestions(preferences)

    complete = True
    storage = False
    grid = result["settlement"]
    selected_forecasts = {}
    grid_power = set()
    for source in sources:
        kind = source.get("type")
        if kind == "grid":
            valid, imports, exports, connection_power = _grid_suggestions(source, result["terms"])
            complete &= valid
            grid_power.update(connection_power)
            grid["imports"].extend(imports)
            grid["exports"].extend(exports)
        elif kind == "solar":
            complete &= _add_solar_suggestion(hass, source, result, selected_forecasts)
        elif kind == "battery":
            storage = True
            complete &= _add_battery_suggestion(source, result)
    _invalidate_shared_solar_providers(result["solar_configurations"], selected_forecasts)
    _finish_energy_suggestions(result, complete, grid_power, storage)
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


def catalogue_areas(hass):
    """Expose stable HA area identities and their current display names."""
    return [
        {"id": area.id, "name": area.name[:MAX_CATALOGUE_TEXT_LENGTH]}
        for area in sorted(ar.async_get(hass).async_list_areas(), key=lambda area: area.id)
    ]


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
    area_id = (entry.area_id if entry else None) or (physical.area_id if physical else None)
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
        **({"area_id": area_id} if area_id else {}),
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
            *hass.states.async_all("device_tracker"),
            *hass.states.async_all("climate"),
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
        if entity_id.startswith(
            ("sensor.", "binary_sensor.", "water_heater.", "device_tracker.", "climate.")
        )
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
        "areas": catalogue_areas(hass),
        "history": await setup_history(hass, suggestions["terms"])
        if include_history and hass.is_running
        else [],
    }
    return body


async def source_choices(hass, scope):
    """Capture a complete finite chooser scope without retaining unrelated metadata or reading history."""
    if scope not in ("ev", "thermal"):
        raise ValueError("Unsupported source choice scope")
    domains = (
        ("sensor", "binary_sensor", "device_tracker")
        if scope == "ev"
        else ("sensor", "water_heater", "climate")
    )
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
