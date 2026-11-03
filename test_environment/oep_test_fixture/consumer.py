"""Persistent forecast-only individual consumer for the designated HA laboratories."""

import logging
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    statistics_during_period,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .calculation.consumption import recorded_additions
from .calculation.recorder.history import quarter_start
from .history import import_fine
from .policy import PARTS_PER_QUARTER, QUARTER_MINUTES, QUARTER_WEIGHTS, STATISTIC_MINUTES

CONSUMER_HISTORY_DAYS = 35
CONSUMER_LOAD_SHARE = 0.2
CONSUMER_BASELINE_KWH = 100.0
CONSUMER_REFRESH_SECONDS = 30
SECONDS_PER_HOUR = 3600
CONSUMER_ENERGY_TOLERANCE_KWH = 0.00001
LOGGER = logging.getLogger(__name__)


async def setup_consumer(hass, entry):
    """Add only a labelled synthetic Energy consumer, preserving other devices and source history."""
    from . import DOMAIN, quarter_energy

    physical = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "forecast_heat_pump")},
        name="Test heat pump",
        manufacturer="Simulated laboratory",
    )
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "forecast_heat_pump_energy",
        config_entry=entry,
        device_id=physical.id,
        suggested_object_id="oep_test_heat_pump_energy",
    ).entity_id
    attributes = {
        "friendly_name": "Test heat pump energy",
        "unit_of_measurement": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
        "synthetic": True,
    }
    manager = await async_get_manager(hass)
    consumers = list((manager.data or {}).get("device_consumption", []))
    if not any(item["stat_consumption"] == entity for item in consumers):
        await manager.async_update(
            {"device_consumption": [*consumers, {"stat_consumption": entity}]}
        )
    end = quarter_start(dt_util.utcnow())
    store = Store(hass, 1, "oep_test_consumer." + entry.entry_id)
    saved = await store.async_load()
    anchor = (
        datetime.fromisoformat(saved["anchor"])
        if saved
        else end.replace(hour=0, minute=0) - timedelta(days=CONSUMER_HISTORY_DAYS)
    )
    if not saved:
        await store.async_save({"anchor": anchor.isoformat()})

    def sample(instant):
        """Measure a disjoint fraction of the existing conserved synthetic household demand."""
        values = quarter_energy(instant, include_dishwasher=False)
        household = (
            values["grid_import"]
            - values["grid_export"]
            + values["solar_roof"]
            + values["solar_garage"]
        )
        return {entity: household * CONSUMER_LOAD_SHARE}

    from .balance import setup_balance
    from .dishwasher import setup_dishwasher, setup_laundry
    from .ev import setup_ev
    from .existing import channels
    from .thermal import async_setup as setup_thermal

    await setup_dishwasher(hass, entry)
    await setup_laundry(hass, entry)
    await setup_ev(hass, entry)
    await setup_thermal(hass, entry)
    await setup_balance(hass, entry)

    roles = channels((manager.data or {}).get("energy_sources", []))
    controlled = {
        registered.entity_id
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id)
        if registered.platform == DOMAIN
        and (
            registered.unique_id == "dishwasher_energy"
            or registered.unique_id == "boiler_energy"
            or registered.unique_id.startswith("ev_wallbox_")
            and registered.unique_id.endswith("_energy")
        )
    }

    laundry = {
        fixture["ids"]["energy"]: datetime.fromisoformat(fixture["data"]["anchor"]).timestamp()
        for key in ("oep_test_washing_machine", "oep_test_dryer")
        if (fixture := hass.data.get(key))
    }

    def historical():
        """Derive only from complete valid measured household hours, preserving old offline gaps/resets."""
        rows = statistics_during_period(
            hass,
            anchor - timedelta(hours=1),
            end,
            {*roles, *controlled, *laundry, entity},
            "hour",
            {"energy": "kWh"},
            {"sum", "state"},
        )
        series = {
            source: {
                row["start"]: row["sum"]
                for row in rows.get(source, [])
                if row.get("sum") is not None
            }
            for source in (*roles, *controlled)
        }
        laundry_series = {
            source: {
                row["start"]: row["sum"]
                for row in rows.get(source, [])
                if row.get("sum") is not None
            }
            for source in laundry
        }
        stamps = sorted(set.intersection(*(set(values) for values in series.values())))
        previous = [row["state"] for row in rows.get(entity, []) if row.get("state") is not None]
        hourly, fine, total = [], [], CONSUMER_BASELINE_KWH
        for before, after in pairwise(stamps):
            if after - before != SECONDS_PER_HOUR:
                continue
            deltas = {source: values[after] - values[before] for source, values in series.items()}
            if min(deltas.values()) < 0:
                if previous and min(deltas.values()) < -CONSUMER_ENERGY_TOLERANCE_KWH:
                    # A partial cumulative replacement would mix old gap rows with a new sum origin.
                    LOGGER.warning(
                        "Repair simulated counter origins before rebuilding consumer history"
                    )
                    return [], [], {entity: previous[-1]}
                continue
            household = sum(
                amount * (-1 if roles[source] in ("grid_export", "battery_charge") else 1)
                for source, amount in deltas.items()
                if source in roles
            )
            extra = recorded_additions(laundry_series, laundry, before, after)
            if extra is None:
                continue
            household -= sum(deltas[source] for source in controlled) + extra
            if household < -CONSUMER_ENERGY_TOLERANCE_KWH:
                # Keep the whole previous archive until its inconsistent grid evidence is repaired.
                if not previous:
                    raise ValueError("Cannot preserve missing simulated consumer history")
                LOGGER.warning("Repair simulated grid balance before rebuilding consumer history")
                return [], [], {entity: previous[-1]}
            if household < 0:
                continue
            energy = household * CONSUMER_LOAD_SHARE
            start = datetime.fromtimestamp(after, UTC)
            initial = total
            for quarter, weight in enumerate(QUARTER_WEIGHTS):
                for part in range(PARTS_PER_QUARTER):
                    total += energy * weight / len(QUARTER_WEIGHTS) / PARTS_PER_QUARTER
                    fine.append(
                        {
                            "start": start
                            + timedelta(
                                minutes=quarter * QUARTER_MINUTES + part * STATISTIC_MINUTES
                            ),
                            "sum": total,
                            "state": total,
                        }
                    )
            total = initial + energy
            hourly.append({"start": start, "sum": total, "state": total})
        return hourly, fine, {entity: total}

    hourly, fine, totals = await get_instance(hass).async_add_executor_job(historical)
    metadata = {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": attributes["friendly_name"],
        "source": "recorder",
        "statistic_id": entity,
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
    }
    if hourly:
        await import_fine(hass, metadata, fine, hourly)
    cursor = end

    @callback
    def update(_now=None):
        """Advance the cumulative consumer on native quarter boundaries without affecting real devices."""
        nonlocal cursor
        boundary = quarter_start(dt_util.utcnow())
        while cursor < boundary:
            totals[entity] += sample(cursor)[entity]
            cursor += timedelta(minutes=QUARTER_MINUTES)
        hass.states.async_set(entity, totals[entity], attributes)

    update()
    entry.async_on_unload(
        async_track_time_interval(hass, update, timedelta(seconds=CONSUMER_REFRESH_SECONDS))
    )
