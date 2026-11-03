"""Laboratory-only HA meters and seed history. Never controls or discovers physical equipment."""

from copy import deepcopy
from datetime import datetime, timedelta

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.core import CoreState, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .calculation.energy import quarter_energy
from .calculation.recorder.history import quarter_start, rows
from .history import import_fine
from .policy import DOMAIN, ENERGY, FINE_HISTORY_VERSION, QUARTER_HOURS, QUARTER_MINUTES


async def async_setup(hass, config):
    """Import one persistent test-only entry from the dedicated laboratory YAML."""
    if DOMAIN not in config:
        return True
    hass.async_create_task(
        hass.config_entries.flow.async_init(DOMAIN, context={"source": "import"}, data={})
    )
    return True


async def async_setup_entry(hass, entry):
    """Register simulated meters, seed Recorder once and refresh readings without executing any service."""
    if hass.state is not CoreState.running:

        @callback
        def after_start(_event):
            """Keep Recorder-dependent consumer initialization outside the HA bootstrap task set."""
            task = hass.async_create_background_task(
                async_setup_entry(hass, entry), "OEP laboratory initialization"
            )
            entry.async_on_unload(task.cancel)

        entry.async_on_unload(async_at_started(hass, after_start))
        return True
    from .consumer import setup_consumer

    if entry.data.get("existing_energy"):
        from .existing import async_setup_existing

        return await async_setup_existing(hass, entry)
    store = Store(hass, 1, "oep_test_fixture")
    saved = await store.async_load()
    now = dt_util.utcnow()
    anchor = (
        datetime.fromisoformat(saved["anchor"])
        if saved
        else now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=32)
    )
    registry, devices = er.async_get(hass), dr.async_get(hass)
    ids = {}
    specs = {}
    for key in (*ENERGY, "grid_power", "battery_power", "battery_soc"):
        device = (
            "battery"
            if key.startswith("battery")
            else "roof"
            if key == "solar_roof"
            else "garage"
            if key == "solar_garage"
            else "grid"
        )
        physical = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device)},
            name="Test " + device,
            manufacturer="Simulated laboratory",
        )
        entity = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            key,
            config_entry=entry,
            device_id=physical.id,
            suggested_object_id="oep_test_" + key,
        )
        ids[key] = entity.entity_id
        specs[key] = {
            "friendly_name": "Test " + key.replace("_", " "),
            "unit_of_measurement": "kWh" if key in ENERGY else "%" if key.endswith("soc") else "W",
            "device_class": "energy"
            if key in ENERGY
            else "battery"
            if key.endswith("soc")
            else "power",
            "state_class": "total_increasing" if key in ENERGY else "measurement",
        }
    end = quarter_start(now)
    history, fine_history, totals = rows(anchor, end, dict.fromkeys(ENERGY, 1000.0), quarter_energy)
    from .balance import declare_sources

    declare_sources(
        hass,
        entry,
        {ids[key]: "solar" if key.startswith("solar_") else key for key in ENERGY},
        lambda instant: {
            ids[key]: value for key, value in quarter_energy(instant).items() if key in ENERGY
        },
    )
    manager = await async_get_manager(hass)
    configured = deepcopy((manager.data or {}).get("energy_sources", []))
    for source in configured:
        if source.get("type") == "grid" and source.get("stat_energy_from") == ids["grid_import"]:
            source.setdefault("stat_rate", ids["grid_power"])
        if (
            source.get("type") == "battery"
            and source.get("stat_energy_from") == ids["battery_discharge"]
        ):
            source.setdefault("stat_rate", ids["battery_power"])
            source.setdefault("stat_soc", ids["battery_soc"])
            source.setdefault("capacity", 10)
    if configured != (manager.data or {}).get("energy_sources", []):
        await manager.async_update({"energy_sources": configured})
    if saved and saved.get("seeded") and "energy_schema" not in saved:
        saved = {**saved, "energy_schema": 1}
        await store.async_save(saved)
    initialize_energy_dashboard = (
        not saved or not saved.get("seeded") or saved.get("energy_schema") != 1
    )
    if initialize_energy_dashboard or saved.get("quarter_history_version") != FINE_HISTORY_VERSION:
        await store.async_save(
            {
                **(saved or {}),
                "anchor": anchor.isoformat(),
                "seeded": not initialize_energy_dashboard,
            }
        )

        async def seed(_event):
            """Import statistics after HA startup so Recorder can process its queue without a bootstrap deadlock."""
            for key in ENERGY:
                metadata = {
                    "statistic_id": ids[key],
                    "source": "recorder",
                    "name": specs[key]["friendly_name"],
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "unit_of_measurement": "kWh",
                    "unit_class": "energy",
                }
                # Version 3 realigns both resolutions. Dashboard initialization is a separate concern.
                await import_fine(hass, metadata, fine_history[key], history[key])
            await get_instance(hass).async_block_till_done()
            if initialize_energy_dashboard:
                manager = await async_get_manager(hass)
                await manager.async_update(
                    {
                        "energy_sources": [
                            {
                                "type": "grid",
                                "stat_energy_from": ids["grid_import"],
                                "stat_energy_to": ids["grid_export"],
                                "stat_rate": ids["grid_power"],
                                "stat_cost": None,
                                "entity_energy_price": None,
                                "number_energy_price": None,
                                "stat_compensation": None,
                                "entity_energy_price_export": None,
                                "number_energy_price_export": None,
                                "cost_adjustment_day": 0,
                            },
                            *[
                                {
                                    "type": "solar",
                                    "stat_energy_from": ids[key],
                                    "config_entry_solar_forecast": None,
                                }
                                for key in ("solar_roof", "solar_garage")
                            ],
                            {
                                "type": "battery",
                                "stat_energy_from": ids["battery_discharge"],
                                "stat_energy_to": ids["battery_charge"],
                                "stat_rate": ids["battery_power"],
                                "stat_soc": ids["battery_soc"],
                                "capacity": 10,
                            },
                        ],
                        "device_consumption": (manager.data or {}).get("device_consumption", []),
                    }
                )
            await store.async_save(
                {
                    "anchor": anchor.isoformat(),
                    "seeded": True,
                    "energy_schema": 1,
                    "quarter_history_version": FINE_HISTORY_VERSION,
                }
            )
            await get_instance(hass).async_block_till_done()
            await setup_consumer(hass, entry)

        @callback
        def schedule_seed(_event):
            """Run Recorder initialization outside the bootstrap task set so startup can finish first."""
            task = hass.async_create_background_task(seed(None), "OEP test history seed")
            entry.async_on_unload(task.cancel)

        if hass.is_running:
            schedule_seed(None)
        else:
            entry.async_on_unload(
                hass.bus.async_listen_once("homeassistant_started", schedule_seed)
            )
    else:
        await setup_consumer(hass, entry)
    current_quarter = end

    from .grid import prepare_grid

    grid_readings = await prepare_grid(
        hass,
        entry,
        {ids["grid_import"]: "grid_import", ids["grid_export"]: "grid_export"},
        {ids["grid_import"]: 1, ids["grid_export"]: 1},
        {ids["grid_import"]: totals["grid_import"], ids["grid_export"]: totals["grid_export"]},
        now,
        lambda instant: {
            ids[key]: value
            for key, value in quarter_energy(instant, include_dishwasher=False).items()
            if key in ("grid_import", "grid_export")
        },
    )

    @callback
    def update(instant):
        """Advance cumulative energy and publish current simulated measurements. Sliders affect no physical devices."""
        nonlocal current_quarter, totals
        hour = quarter_start(instant)
        while current_quarter < hour:
            delta = quarter_energy(current_quarter, include_dishwasher=False)
            totals = {key: totals[key] + delta[key] for key in ENERGY}
            current_quarter += timedelta(minutes=QUARTER_MINUTES)
        delta = quarter_energy(hour, include_dishwasher=False)
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
        for key in ENERGY:
            hass.states.async_set(
                ids[key],
                round(
                    grid_values[ids[key]]
                    if key in ("grid_import", "grid_export")
                    else totals[key] + fraction * delta[key],
                    6,
                ),
                specs[key],
            )
        available = hass.states.get("input_boolean.oep_test_grid_available")
        grid = (
            "unavailable"
            if available and available.state == "off"
            else round(
                1000 * (delta["grid_import"] - delta["grid_export"]) / QUARTER_HOURS
                + simulated_device_power_w
            )
        )
        soc = hass.states.get("input_number.oep_test_soc")
        hass.states.async_set(ids["grid_power"], grid, specs["grid_power"])
        hass.states.async_set(ids["battery_power"], 0, specs["battery_power"])
        hass.states.async_set(
            ids["battery_soc"],
            soc.state if soc and soc.state not in ("unknown", "unavailable") else 50,
            specs["battery_soc"],
        )

    update(now)
    entry.async_on_unload(async_track_time_interval(hass, update, timedelta(seconds=15)))
    return True


async def async_remove_entry(hass, entry):
    """Clear only the existing-source fixture baseline after explicit entry removal. Retain Recorder history."""
    if entry.data.get("existing_energy"):
        await Store(hass, 1, "oep_existing_energy_test").async_remove()
