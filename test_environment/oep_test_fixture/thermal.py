"""Persistent laboratory electric boiler. Never addresses physical equipment."""

import asyncio
from datetime import timedelta
from math import isfinite
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .calculation.recorder.history import quarter_start, rows
from .history import import_fine

DATA_KEY = "oep_test_boiler"
VOLUME_LITRES = 200
HEATER_W = 2000
AMBIENT_C = 20
MAXIMUM_C = 65
LOSS_W_PER_C = 1
HEAT_CAPACITY_KWH_PER_C = VOLUME_LITRES * 4.186 / 3600
REFRESH_SECONDS = 5
HISTORY_DAYS = 35
# Thermal withdrawal per local quarter. This is not electrical consumption.
DRAW_PROFILE_KWH = {28: 0.8, 29: 0.6, 76: 1.2, 77: 0.6}


def live_extra(hass):
    """Expose live electrical energy and power once to the conserved installation balance."""
    simulation = hass.data.get(DATA_KEY)
    if simulation is None:
        return 0.0, 0
    return simulation.energy_kwh - simulation.initial_kwh, simulation.power_w


class BoilerSimulation:
    """Integrate measured laboratory electricity, standing losses and hot-water withdrawals."""

    def __init__(self, hass, entry, store, saved, ids):
        """Restore only tank temperature and cumulative energy. Heater requests never survive restart."""
        self.hass = hass
        self.entry = entry
        self.store = store
        self.ids = ids
        self.temperature_c = self._saved_number(saved, "temperature_c", 55, AMBIENT_C, MAXIMUM_C)
        self.energy_kwh = self._saved_number(saved, "energy_kwh", 0, 0, 1e12)
        self.initial_kwh = self.energy_kwh
        self.requested = False
        self.thermostat_on = False
        self.power_w = 0
        self.at = dt_util.utcnow()
        self.timezone = ZoneInfo(hass.config.time_zone)

    @staticmethod
    def _saved_number(saved, key, default, minimum, maximum):
        """Keep invalid fixture storage from producing invalid HA measurements."""
        value = (saved or {}).get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return value if minimum <= value <= maximum and isfinite(value) else default

    def advance(self, at):
        """Use actual elapsed time with bounded steps across local quarter boundaries."""
        while self.at < at:
            self._update_power()
            local = self.at.astimezone(self.timezone)
            quarter_remaining = 900 - (
                local.minute % 15 * 60 + local.second + local.microsecond / 1e6
            )
            seconds = min(REFRESH_SECONDS, quarter_remaining, (at - self.at).total_seconds())
            hours = seconds / 3600
            draw_kwh = DRAW_PROFILE_KWH.get(local.hour * 4 + local.minute // 15, 0) * hours * 4
            loss_kwh = max(0, self.temperature_c - AMBIENT_C) * LOSS_W_PER_C / 1000 * hours
            withdrawn_kwh = min(
                max(0, self.temperature_c - AMBIENT_C) * HEAT_CAPACITY_KWH_PER_C,
                draw_kwh + loss_kwh,
            )
            heater_kwh = (
                min(
                    HEATER_W / 1000 * hours,
                    (MAXIMUM_C - self.temperature_c) * HEAT_CAPACITY_KWH_PER_C + withdrawn_kwh,
                )
                if self.power_w
                else 0
            )
            self.temperature_c = min(
                MAXIMUM_C,
                max(
                    AMBIENT_C,
                    self.temperature_c + (heater_kwh - withdrawn_kwh) / HEAT_CAPACITY_KWH_PER_C,
                ),
            )
            self.energy_kwh += heater_kwh
            self.at += timedelta(seconds=seconds)
        self._update_power()
        self.render()

    def _update_power(self):
        """Keep the independent upper thermostat off until its two-degree deadband clears."""
        if not self.requested or self.temperature_c >= MAXIMUM_C:
            self.thermostat_on = False
        elif self.temperature_c <= MAXIMUM_C - 2:
            self.thermostat_on = True
        self.power_w = HEATER_W if self.requested and self.thermostat_on else 0

    @callback
    def render(self):
        """Report fresh simulation measurements without replacing any existing meter history."""
        for key, value, attributes in (
            (
                "temperature",
                self.temperature_c,
                {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "state_class": "measurement",
                },
            ),
            (
                "power",
                self.power_w,
                {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
            ),
            (
                "energy",
                self.energy_kwh,
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            ),
            ("heater", "on" if self.requested else "off", {}),
        ):
            self.hass.states.async_set(
                self.ids[key],
                value,
                {"friendly_name": "Test electric boiler " + key, **attributes},
                force_update=True,
            )

    async def save(self):
        """Persist thermal state and metered electricity only, never a stale heater permission."""
        await self.store.async_save(
            {"temperature_c": self.temperature_c, "energy_kwh": self.energy_kwh}
        )


async def async_setup(hass, entry):
    """Serialize initial seeding so concurrent consumer setup cannot import the same rows twice."""
    lock = hass.data.setdefault(DATA_KEY + "_setup_lock", asyncio.Lock())
    async with lock:
        return await _setup(hass, entry)


async def _setup(hass, entry):
    """Add an isolated boiler and its Energy meter while preserving the rest of the installation."""
    from . import DOMAIN

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "boiler")},
        name="Test electric boiler",
        manufacturer="Simulated laboratory",
    )
    registry = er.async_get(hass)
    ids = {
        key: registry.async_get_or_create(
            "switch" if key == "heater" else "sensor",
            DOMAIN,
            "boiler_" + key,
            config_entry=entry,
            device_id=device.id,
            suggested_object_id="oep_test_boiler_" + key,
        ).entity_id
        for key in ("temperature", "power", "energy", "heater")
    }
    store = Store(hass, 1, "oep_test_boiler." + entry.entry_id)
    saved = await store.async_load()
    if saved is None:
        end = quarter_start(dt_util.utcnow())
        initial = {ids["energy"]: 0}
        hourly, fine, _ = rows(
            end.replace(hour=0, minute=0) - timedelta(days=HISTORY_DAYS),
            end,
            initial,
            lambda _instant: dict(initial),
        )
        await import_fine(
            hass,
            {
                "statistic_id": ids["energy"],
                "source": "recorder",
                "name": "Test electric boiler energy",
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "unit_of_measurement": "kWh",
                "unit_class": "energy",
            },
            fine[ids["energy"]],
            hourly[ids["energy"]],
        )
        await get_instance(hass).async_block_till_done()
    simulation = BoilerSimulation(hass, entry, store, saved, ids)
    hass.data[DATA_KEY] = simulation
    manager = await async_get_manager(hass)
    consumers = list((manager.data or {}).get("device_consumption", []))
    if not any(item["stat_consumption"] == ids["energy"] for item in consumers):
        await manager.async_update(
            {"device_consumption": [*consumers, {"stat_consumption": ids["energy"]}]}
        )

    async def heater(call):
        """Apply an immediate laboratory-only request after integrating the preceding measurement."""
        simulation.advance(dt_util.utcnow())
        simulation.requested = call.data["enabled"]
        simulation.advance(simulation.at)
        await simulation.save()

    hass.services.async_register(
        DOMAIN, "boiler_heater", heater, schema=vol.Schema({vol.Required("enabled"): vol.Boolean()})
    )

    @callback
    def tick(at):
        """Measure real elapsed heating and schedule durable state persistence."""
        simulation.advance(at)
        entry.async_create_task(hass, simulation.save(), "Save simulated boiler state")

    remove_timer = async_track_time_interval(hass, tick, timedelta(seconds=REFRESH_SECONDS))

    async def stop(_event=None):
        """Withdraw heating on unload or shutdown without restoring an old request next startup."""
        remove_timer()
        simulation.advance(dt_util.utcnow())
        simulation.requested = False
        simulation.advance(simulation.at)
        await simulation.save()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))

    @callback
    def unload():
        """Remove fixture callbacks and schedule a final safe state write."""
        remove_timer()
        hass.services.async_remove(DOMAIN, "boiler_heater")
        hass.data.pop(DATA_KEY, None)
        hass.async_create_task(stop())

    entry.async_on_unload(unload)
    simulation.render()
    await simulation.save()
    return simulation
