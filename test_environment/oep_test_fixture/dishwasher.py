"""Persistent synthetic appliance cycles for the two laboratories. Never dispatches physical equipment."""

from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .calculation.appliances import historical_energy, integral
from .calculation.recorder.history import quarter_start, rows
from .history import import_fine
from .profiles import HISTORY_CYCLE_HOUR_UTC, PHASE_SECONDS, POWERS_W, PROFILES

HISTORY_DAYS = 32
REFRESH_SECONDS = 15
ENERGY_BASELINE_KWH = 50.0
DATA_KEY = "oep_test_dishwasher"


def live_extra(hass):
    """Expose only live energy added since fixture startup, for the primary grid meter's conserved balance."""
    value = hass.data.get(DATA_KEY)
    if not value:
        return 0.0, 0.0
    return value["data"]["total_kwh"] - value["initial_kwh"], value.get("power_w", 0)


async def setup_appliance(hass, entry, identity, name, powers_w, *, seed_history=False):
    """Register a durable simulated cycle. New laundry fixtures never backfill or rewrite meter history."""
    from . import DOMAIN

    data_key = "oep_test_" + identity
    duration_seconds = len(powers_w) * PHASE_SECONDS
    if data_key in hass.data:
        return
    physical = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, identity)},
        name=name,
        manufacturer="Simulated laboratory",
    )
    registry = er.async_get(hass)
    ids = {
        key: registry.async_get_or_create(
            "sensor",
            DOMAIN,
            identity + "_" + key,
            config_entry=entry,
            device_id=physical.id,
            suggested_object_id="oep_test_" + identity + "_" + key,
        ).entity_id
        for key in ("energy", "power", "status")
    }
    manager = await async_get_manager(hass)
    consumers = list((manager.data or {}).get("device_consumption", []))
    if not any(item["stat_consumption"] == ids["energy"] for item in consumers):
        await manager.async_update(
            {
                "device_consumption": [
                    *consumers,
                    {"stat_consumption": ids["energy"], "stat_rate": ids["power"]},
                ]
            }
        )
    store = Store(hass, 1, "oep_test_" + identity + "." + entry.entry_id)
    data = await store.async_load()
    if (not data or not data.get("energy_power_link_initialized")) and any(
        item["stat_consumption"] == ids["energy"] and not item.get("stat_rate")
        for item in consumers
    ):
        await manager.async_update(
            {
                "device_consumption": [
                    {**item, "stat_rate": ids["power"]}
                    if item["stat_consumption"] == ids["energy"]
                    else item
                    for item in consumers
                ]
            }
        )
    end = quarter_start(dt_util.utcnow())
    anchor = (
        datetime.fromisoformat(data["anchor"])
        if data
        else end.replace(hour=0, minute=0) - timedelta(days=HISTORY_DAYS)
        if seed_history
        else end
    )
    if not data:
        if seed_history:
            hourly, fine, totals = rows(
                anchor,
                end,
                {ids["energy"]: ENERGY_BASELINE_KWH},
                lambda at: {ids["energy"]: historical_energy(at)},
            )
        data = {
            "energy_power_link_initialized": True,
            "anchor": anchor.isoformat(),
            "total_kwh": totals[ids["energy"]] if seed_history else ENERGY_BASELINE_KWH,
            "status": "idle",
            "started_at": None,
            "progress": 0,
            "offset": 0,
            "delay_seconds": 0,
            "fail": False,
            "withhold_status": False,
        }
        metadata = {
            "statistic_id": ids["energy"],
            "source": "recorder",
            "name": name + " energy",
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "unit_of_measurement": "kWh",
            "unit_class": "energy",
        }
        if seed_history:
            await import_fine(hass, metadata, fine[ids["energy"]], hourly[ids["energy"]])
            await get_instance(hass).async_block_till_done()
        await store.async_save(data)
    if not data.get("energy_power_link_initialized"):
        data["energy_power_link_initialized"] = True
        await store.async_save(data)
    state = {"data": data, "initial_kwh": data["total_kwh"], "power_w": 0, "ids": ids, "starts": 0}
    hass.data[data_key] = state
    entry.async_on_unload(lambda: hass.data.pop(data_key, None))

    def render(now):
        """Advance only an uncancelled simulated run and expose trustworthy state separately from power."""
        before = (data["progress"], data["status"])
        if data["status"] in ("queued", "running"):
            elapsed = max(
                0,
                (now - datetime.fromisoformat(data["started_at"])).total_seconds()
                + data["offset"]
                - data["delay_seconds"],
            )
            if (
                data["status"] == "queued"
                and (now - datetime.fromisoformat(data["started_at"])).total_seconds()
                + data["offset"]
                >= data["delay_seconds"]
            ):
                data["status"] = "cancelled" if data["fail"] else "running"
            if data["status"] == "running":
                progress = min(duration_seconds, elapsed)
                data["total_kwh"] += integral(progress, powers_w) - integral(
                    data["progress"], powers_w
                )
                data["progress"] = progress
                if progress >= duration_seconds:
                    data["status"] = "completed"
        power = (
            powers_w[min(len(powers_w) - 1, int(data["progress"] // PHASE_SECONDS))]
            if data["status"] == "running"
            else 0
        )
        state["power_w"] = power
        energy_attributes = {
            "friendly_name": name + " energy",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
            "synthetic": True,
        }
        hass.states.async_set(ids["energy"], round(data["total_kwh"], 9), energy_attributes)
        hass.states.async_set(
            ids["power"],
            power,
            {
                "friendly_name": name + " power",
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "synthetic": True,
            },
        )
        hass.states.async_set(
            ids["status"],
            "unavailable" if data["withhold_status"] else data["status"],
            {
                "friendly_name": name + " status",
                "synthetic": True,
                "elapsed_seconds": data["progress"],
                "fixture_start_count": state["starts"],
                **({"history_cycle_hour_utc": HISTORY_CYCLE_HOUR_UTC} if seed_history else {}),
            },
        )
        return before != (data["progress"], data["status"])

    async def action(call):
        """Persist cancellation before acknowledging it. Cancelled delayed execution never resumes after restart."""
        render(dt_util.utcnow())
        if call.service == identity + "_start":
            if data["status"] in ("queued", "running"):
                raise ValueError("The simulated appliance is already queued or running")
            data.update(
                status="queued", started_at=dt_util.utcnow().isoformat(), progress=0, offset=0
            )
            state["starts"] += 1
        elif call.service == identity + "_cancel":
            if data["status"] == "running":
                raise ValueError(
                    "Cancellation only invalidates queued starts. It never interrupts a running cycle"
                )
            data["status"] = "cancelled"
        elif call.service == identity + "_advance":
            # Test-only accelerated progress is explicit. Production has no clock/advance endpoint.
            data["offset"] += call.data["seconds"]
        else:
            data.update(call.data)
        render(dt_util.utcnow())
        await store.async_save(data)

    for service_name, schema in (
        (identity + "_start", {}),
        (identity + "_cancel", {}),
        (
            identity + "_advance",
            {
                vol.Required("seconds"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=duration_seconds)
                )
            },
        ),
        (
            identity + "_options",
            {
                vol.Optional("delay_seconds"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=duration_seconds)
                ),
                vol.Optional("fail"): bool,
                vol.Optional("withhold_status"): bool,
            },
        ),
    ):
        hass.services.async_register(DOMAIN, service_name, action, schema=vol.Schema(schema))
        entry.async_on_unload(lambda name=service_name: hass.services.async_remove(DOMAIN, name))

    @callback
    def update(now):
        """Persist progress after each real timer tick. Startup resumes only the same simulated run."""
        if render(now):
            entry.async_create_task(
                hass, store.async_save(data), "Save simulated appliance progress"
            )

    update(dt_util.utcnow())
    entry.async_on_unload(
        async_track_time_interval(hass, update, timedelta(seconds=REFRESH_SECONDS))
    )


async def setup_dishwasher(hass, entry):
    """Keep the original dishwasher identity, history and service contract unchanged."""
    await setup_appliance(hass, entry, "dishwasher", "Test dishwasher", POWERS_W, seed_history=True)


LAUNDRY_PROFILES = {
    identity: (PROFILES[identity]["name"], tuple(PROFILES[identity]["power_w"]))
    for identity in ("washing_machine", "dryer")
}


async def setup_laundry(hass, entry):
    """Add two independent simulated devices without manufacturing historical meter readings."""
    for identity, (name, powers_w) in LAUNDRY_PROFILES.items():
        await setup_appliance(hass, entry, identity, name, powers_w)


def laundry_extra(hass):
    """Return newly observed laundry kWh and watts for the existing conserved grid meter."""
    states = {identity: hass.data.get("oep_test_" + identity) for identity in LAUNDRY_PROFILES}
    return (
        {
            "oep_test_" + identity: (state["data"]["total_kwh"], state["initial_kwh"])
            for identity, state in states.items()
            if state
        },
        sum(state.get("power_w", 0) for state in states.values() if state),
    )
