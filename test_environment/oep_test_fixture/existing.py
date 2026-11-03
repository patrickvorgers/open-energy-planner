"""Seed labelled native-test data on existing Energy identities without replacing the dashboard."""

from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .calculation.recorder.history import quarter_start, rows
from .history import import_fine
from .policy import FINE_HISTORY_VERSION, QUARTER_HOURS, QUARTER_MINUTES


async def extend_installation(hass, entry, manager):
    """Permanently add missing native-test storage and a second PV meter without replacing existing Energy sources."""
    from . import DOMAIN

    sources = deepcopy((manager.data or {}).get("energy_sources", []))
    registry, devices = er.async_get(hass), dr.async_get(hass)
    ids, attributes = {}, {}
    for key in (
        "solar_garage",
        "battery_charge",
        "battery_discharge",
        "battery_power",
        "battery_soc",
    ):
        battery = key.startswith("battery")
        physical = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, "native_battery" if battery else "native_garage")},
            name="Test battery" if battery else "Test garage solar",
            manufacturer="Simulated laboratory",
        )
        entity = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            "native_" + key,
            config_entry=entry,
            device_id=physical.id,
            suggested_object_id="oep_test_" + key,
        )
        ids[key] = entity.entity_id
        energy = key not in ("battery_power", "battery_soc")
        attributes[entity.entity_id] = {
            "friendly_name": "Test " + key.replace("_", " "),
            "unit_of_measurement": "kWh" if energy else "%" if key.endswith("soc") else "W",
            "device_class": "energy" if energy else "battery" if key.endswith("soc") else "power",
            "state_class": "total_increasing" if energy else "measurement",
        }
        if not energy:
            hass.states.async_set(
                entity.entity_id, 50 if key.endswith("soc") else 0, attributes[entity.entity_id]
            )
    if sum(source_item.get("type") == "solar" for source_item in sources) < 2:
        sources.append(
            {
                "type": "solar",
                "stat_energy_from": ids["solar_garage"],
                "config_entry_solar_forecast": None,
            }
        )
    if not any(source_item.get("type") == "battery" for source_item in sources):
        sources.append(
            {
                "type": "battery",
                "stat_energy_from": ids["battery_discharge"],
                "stat_energy_to": ids["battery_charge"],
                "stat_rate": ids["battery_power"],
                "stat_soc": ids["battery_soc"],
                "capacity": 10,
            }
        )
    for source in sources:
        if (
            source.get("type") == "battery"
            and source.get("stat_energy_from") == ids["battery_discharge"]
        ):
            source.setdefault("stat_soc", ids["battery_soc"])
            source.setdefault("capacity", 10)
    if sources != (manager.data or {}).get("energy_sources", []):
        await manager.async_update({"energy_sources": sources})
    return ids, attributes


def channels(sources):
    """Keep each configured grid/PV/storage channel once, with its physical role and equal-share test weight."""
    result = {}
    for source in sources:
        kind = source.get("type")
        if kind == "grid":
            for key, legacy, role in (
                ("stat_energy_from", "flow_from", "grid_import"),
                ("stat_energy_to", "flow_to", "grid_export"),
            ):
                routes = [{key: source[key]}] if source.get(key) else source.get(legacy, [])
                for route in routes:
                    result[route[key]] = role
        elif kind == "solar":
            result[source["stat_energy_from"]] = "solar"
        elif kind == "battery":
            result[source["stat_energy_from"]] = "battery_discharge"
            result[source["stat_energy_to"]] = "battery_charge"
    if "grid_import" not in result.values():
        raise ValueError("Configure grid import in the test Energy dashboard first")
    return result


def samples(instant, roles, *, include_dishwasher=True):
    """Split the conserved synthetic household/PV balance among the existing tariff and inverter channels."""
    from . import quarter_energy

    values = quarter_energy(instant, include_dishwasher=include_dishwasher)
    values["solar"] = values["solar_roof"] + values["solar_garage"]
    hour = instant.astimezone(ZoneInfo("Europe/Amsterdam")).hour
    if "battery_charge" in roles.values():
        values["battery_charge"] = 0.4 * QUARTER_HOURS if 12 <= hour < 15 else 0
        values["battery_discharge"] = 0.4 * QUARTER_HOURS if 18 <= hour < 21 else 0
        net = (
            values["grid_import"]
            - values["grid_export"]
            + values["battery_charge"]
            - values["battery_discharge"]
        )
        values["grid_import"], values["grid_export"] = max(0, net), max(0, -net)
    return {
        entity: values[role] / list(roles.values()).count(role) for entity, role in roles.items()
    }


async def async_setup_existing(hass, entry):
    """Import 32 days via Recorder, retain older history/preferences and refresh only these test-VM states."""
    manager = await async_get_manager(hass)
    fixture_ids, fixture_attributes = await extend_installation(hass, entry, manager)
    roles = channels((manager.data or {}).get("energy_sources", []))
    from .balance import declare_sources

    declare_sources(hass, entry, roles, lambda instant: samples(instant, roles))
    store = Store(hass, 1, "oep_existing_energy_test")
    saved = await store.async_load()
    now = dt_util.utcnow()
    end = quarter_start(now)
    anchor = (
        datetime.fromisoformat(saved["anchor"])
        if saved
        else end.replace(minute=0) - timedelta(days=32)
    )
    metadata = {}
    for entity in roles:
        state = hass.states.get(entity)
        # Registry placeholders can omit units. Only our own fixture supplies authoritative defaults.
        attributes = {
            **(dict(state.attributes) if state else {}),
            **fixture_attributes.get(entity, {}),
        }
        if attributes.get("unit_of_measurement") not in ("Wh", "kWh", "MWh"):
            raise ValueError(
                "Existing test energy entities must expose their original energy units"
            )
        metadata[entity] = attributes
    units = {
        entity: {"Wh": 1000, "kWh": 1, "MWh": 0.001}[attrs["unit_of_measurement"]]
        for entity, attrs in metadata.items()
    }
    grid_power = {
        (source.get("power_config") or source)["stat_rate"]
        for source in (manager.data or {}).get("energy_sources", [])
        if source.get("type") == "grid"
        and isinstance((source.get("power_config") or source).get("stat_rate"), str)
    }
    power_entity = next(iter(grid_power)) if len(grid_power) == 1 else None
    power_state = hass.states.get(power_entity) if power_entity else None
    power_attributes = (
        dict(power_state.attributes)
        if power_state
        else {
            "friendly_name": "Test net grid power",
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
        }
    )
    baseline = (
        saved["baseline"]
        if saved
        else {
            entity: float(hass.states.get(entity).state)
            if hass.states.get(entity)
            else 1000 * units[entity]
            for entity in roles
        }
    )
    missing = set(roles) - baseline.keys()
    if missing - set(fixture_ids.values()) or set(baseline) - roles.keys():
        raise ValueError("Test dashboard changed. Remove/reconfigure this test fixture explicitly")
    baseline = {**baseline, **{entity: 1000 * units[entity] for entity in missing}}
    reseed = (
        not saved
        or not saved.get("seeded")
        or bool(missing)
        or saved.get("source_schema") != 2
        or saved.get("quarter_history_version") != FINE_HISTORY_VERSION
    )
    history, fine_history, totals = rows(
        anchor,
        end,
        baseline,
        lambda instant: {
            entity: value * units[entity] for entity, value in samples(instant, roles).items()
        },
    )

    async def seed(_event):
        """Run after startup to avoid blocking Recorder bootstrap. Repeat safely after an interrupted seed."""
        from .consumer import setup_consumer

        if not reseed:
            await setup_consumer(hass, entry)
            return
        await store.async_save(
            {"anchor": anchor.isoformat(), "baseline": baseline, "seeded": False}
        )
        for entity in roles:
            metadata_row = {
                "statistic_id": entity,
                "source": "recorder",
                "name": metadata[entity].get("friendly_name"),
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "unit_of_measurement": metadata[entity]["unit_of_measurement"],
                "unit_class": "energy",
            }
            await import_fine(hass, metadata_row, fine_history[entity], history[entity])
        await get_instance(hass).async_block_till_done()
        await store.async_save(
            {
                "anchor": anchor.isoformat(),
                "baseline": baseline,
                "seeded": True,
                "source_schema": 2,
                "quarter_history_version": FINE_HISTORY_VERSION,
            }
        )
        await setup_consumer(hass, entry)

    @callback
    def schedule(_event):
        """Schedule fixture import outside the startup task set."""
        task = hass.async_create_background_task(seed(None), "OEP existing Energy test history")
        entry.async_on_unload(task.cancel)

    if hass.is_running:
        schedule(None)
    else:
        entry.async_on_unload(hass.bus.async_listen_once("homeassistant_started", schedule))
    current_quarter = end

    from .grid import prepare_grid

    grid_readings = await prepare_grid(
        hass,
        entry,
        roles,
        units,
        totals,
        now,
        lambda instant: samples(instant, roles, include_dishwasher=False),
    )

    @callback
    def update(instant):
        """Advance test counters without calling physical services or changing Energy preferences."""
        nonlocal current_quarter, totals
        hour = quarter_start(instant)
        while current_quarter < hour:
            delta = samples(current_quarter, roles, include_dishwasher=False)
            totals = {
                entity: total + delta[entity] * units[entity] for entity, total in totals.items()
            }
            current_quarter += timedelta(minutes=QUARTER_MINUTES)
        delta = samples(hour, roles, include_dishwasher=False)
        from .dishwasher import laundry_extra, live_extra

        _, dishwasher_w = live_extra(hass)
        from .ev import live_extra as ev_extra

        _, ev_power_w = ev_extra(hass)
        _, laundry_w = laundry_extra(hass)
        from .thermal import live_extra as thermal_extra

        _, thermal_power_w = thermal_extra(hass)
        simulated_device_power_w = dishwasher_w + ev_power_w + laundry_w + thermal_power_w
        grid_values = grid_readings(instant)
        fraction = (instant - hour).total_seconds() / (QUARTER_MINUTES * 60)
        for entity in roles:
            hass.states.async_set(
                entity,
                round(
                    grid_values[entity]
                    if entity in grid_values
                    else totals[entity] + fraction * delta[entity] * units[entity],
                    6,
                ),
                metadata[entity],
            )
        battery_power = sum(
            delta[entity_id] for entity_id, role in roles.items() if role == "battery_discharge"
        ) - sum(delta[entity_id] for entity_id, role in roles.items() if role == "battery_charge")
        local_hour = (
            instant.astimezone(ZoneInfo("Europe/Amsterdam")).hour
            + (instant.minute + instant.second / 60) / 60
        )
        stored = min(3, max(0, local_hour - 12)) * 0.4 - min(3, max(0, local_hour - 18)) * 0.4
        for key, value in (
            ("battery_power", battery_power * 1000 / QUARTER_HOURS),
            ("battery_soc", 50 + stored * 10),
        ):
            state = hass.states.get(fixture_ids[key])
            hass.states.async_set(fixture_ids[key], round(value, 3), state.attributes)
        if power_entity and power_attributes.get("unit_of_measurement") in ("W", "kW"):
            net_kw = sum(
                delta[entity] for entity, role in roles.items() if role == "grid_import"
            ) - sum(delta[entity] for entity, role in roles.items() if role == "grid_export")
            hass.states.async_set(
                power_entity,
                round(
                    net_kw
                    / QUARTER_HOURS
                    * (1000 if power_attributes["unit_of_measurement"] == "W" else 1)
                    + simulated_device_power_w
                    * (1 if power_attributes["unit_of_measurement"] == "W" else 0.001),
                    3,
                ),
                power_attributes,
            )

    update(now)
    entry.async_on_unload(async_track_time_interval(hass, update, timedelta(seconds=15)))
    return True
