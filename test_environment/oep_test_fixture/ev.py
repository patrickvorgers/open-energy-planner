"""Persistent simulated cars and AC charge points. All commands target laboratory entities only."""

from datetime import datetime, timedelta
from math import isfinite

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

from .calculation.recorder.history import quarter_start, rows
from .history import import_fine

DATA_KEY = "oep_test_ev"
HISTORY_DAYS = 35
REFRESH_SECONDS = 5
CAR_CAPACITIES_KWH = (60, 40)
CAR_LIMITS_W = (11000, 7400)
POINT_LIMITS_W = (11000, 7400)
SIMULATED_CHARGING_EFFICIENCY = 0.9
SECONDS_PER_HOUR = 3600
WATTS_PER_KW = 1000


def finite_number(value):
    """Reject nonfinite service inputs before a simulated counter or SoC can be corrupted."""
    result = float(value)
    if not isfinite(result):
        raise vol.Invalid("Value must be finite")
    return result


def live_extra(hass):
    """Expose cumulative actual AC energy and current power, without reallocating earlier grid readings."""
    state = hass.data.get(DATA_KEY)
    if not state:
        return {}, 0
    return (
        {
            str(index): (point["energy_kwh"], state["initial"][index])
            for index, point in enumerate(state["data"]["points"])
        },
        sum(point["power_w"] for point in state["data"]["points"]),
    )


async def setup_ev(hass, entry):
    """Add two cars and two Energy-owned points once. Preserve all existing fixture/configuration/history."""
    from . import DOMAIN

    if DATA_KEY in hass.data:
        return
    store = Store(hass, 1, "oep_test_ev." + entry.entry_id)
    saved = await store.async_load()
    now = dt_util.utcnow()
    data = saved or {
        "cars": [{"soc": 35}, {"soc": 45}],
        "points": [
            {"vehicle": None, "power_w": 0, "energy_kwh": 0},
            {"vehicle": None, "power_w": 0, "energy_kwh": 0},
        ],
        "at": now.isoformat(),
    }
    # No simulated charging occurs during an HA shutdown or before a fresh virtual command.
    for point in data["points"]:
        point["power_w"] = 0
    data["at"] = now.isoformat()
    registry, devices = er.async_get(hass), dr.async_get(hass)
    cars, points = [], []
    for kind, count, fields in (
        ("car", len(data["cars"]), (("sensor", "soc"),)),
        (
            "wallbox",
            len(data["points"]),
            (
                ("sensor", "energy"),
                ("sensor", "power"),
                ("binary_sensor", "connected"),
                ("sensor", "vehicle"),
            ),
        ),
    ):
        for index in range(count):
            suffix = chr(ord("a") + index)
            device = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, "ev_" + kind + "_" + suffix)},
                name="Test " + kind + " " + suffix.upper(),
                manufacturer="Simulated laboratory",
            )
            ids = {
                key: registry.async_get_or_create(
                    domain,
                    DOMAIN,
                    "ev_" + kind + "_" + suffix + "_" + key,
                    config_entry=entry,
                    device_id=device.id,
                    suggested_object_id="oep_test_" + kind + "_" + suffix + "_" + key,
                ).entity_id
                for domain, key in fields
            }
            (cars if kind == "car" else points).append(ids)
    manager = await async_get_manager(hass)
    consumers = list((manager.data or {}).get("device_consumption", []))
    for point in points:
        if not any(row["stat_consumption"] == point["energy"] for row in consumers):
            consumers.append({"stat_consumption": point["energy"]})
    if consumers != (manager.data or {}).get("device_consumption", []):
        await manager.async_update({"device_consumption": consumers})
    if saved is None:
        end = quarter_start(now)
        initial = {point["energy"]: 0 for point in points}
        hourly, fine, _ = rows(
            end.replace(hour=0, minute=0) - timedelta(days=HISTORY_DAYS),
            end,
            initial,
            lambda _instant: dict(initial),
        )
        for point in points:
            await import_fine(
                hass,
                {
                    "statistic_id": point["energy"],
                    "source": "recorder",
                    "name": "Test EV charging energy",
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "unit_of_measurement": "kWh",
                    "unit_class": "energy",
                },
                fine[point["energy"]],
                hourly[point["energy"]],
            )
        await get_instance(hass).async_block_till_done()
    state = {
        "data": data,
        "initial": [point["energy_kwh"] for point in data["points"]],
        "cars": cars,
        "points": points,
    }
    hass.data[DATA_KEY] = state
    entry.async_on_unload(lambda: hass.data.pop(DATA_KEY, None))

    def render(at):
        """Integrate only actual elapsed simulated power and bound each vehicle's physical SoC at 100%."""
        hours = max(0, (at - datetime.fromisoformat(data["at"])).total_seconds()) / SECONDS_PER_HOUR
        for index, point in enumerate(data["points"]):
            vehicle = point["vehicle"]
            if vehicle is None:
                point["power_w"] = 0
            else:
                car = data["cars"][vehicle]
                headroom = (100 - car["soc"]) * CAR_CAPACITIES_KWH[vehicle] / 100
                energy = min(
                    headroom / SIMULATED_CHARGING_EFFICIENCY,
                    point["power_w"] / WATTS_PER_KW * hours,
                )
                point["energy_kwh"] += energy
                car["soc"] = min(
                    100,
                    car["soc"]
                    + energy * SIMULATED_CHARGING_EFFICIENCY / CAR_CAPACITIES_KWH[vehicle] * 100,
                )
                if car["soc"] >= 100:
                    point["power_w"] = 0
            for key, value, attributes in (
                (
                    "energy",
                    point["energy_kwh"],
                    {
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                ),
                (
                    "power",
                    point["power_w"],
                    {
                        "unit_of_measurement": "W",
                        "device_class": "power",
                        "state_class": "measurement",
                    },
                ),
                ("connected", "off" if vehicle is None else "on", {"device_class": "plug"}),
                ("vehicle", "unknown" if vehicle is None else "car_" + chr(ord("a") + vehicle), {}),
            ):
                hass.states.async_set(
                    points[index][key],
                    value,
                    {
                        "friendly_name": "Test wallbox " + chr(ord("A") + index) + " " + key,
                        "synthetic": True,
                        **attributes,
                    },
                )
        for index, car in enumerate(data["cars"]):
            hass.states.async_set(
                cars[index]["soc"],
                round(car["soc"], 6),
                {
                    "friendly_name": "Test car " + chr(ord("A") + index) + " state of charge",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "state_class": "measurement",
                    "synthetic": True,
                },
            )
        data["at"] = at.isoformat()

    async def action(call):
        """Change only simulated connection/power/SoC. Never invoke a vendor or physical service."""
        render(dt_util.utcnow())
        if call.service == "ev_soc":
            data["cars"][call.data["vehicle"]]["soc"] = call.data["soc"]
        else:
            point = data["points"][call.data["point"]]
            if call.service == "ev_connect":
                vehicle = call.data["vehicle"] if call.data["connected"] else None
                if vehicle is not None and any(
                    other is not point and other["vehicle"] == vehicle for other in data["points"]
                ):
                    raise ValueError("A simulated car cannot connect to two points")
                point.update(vehicle=vehicle, power_w=0)
            else:
                power = call.data["power_w"]
                vehicle = point["vehicle"]
                if power and (
                    vehicle is None
                    or power > min(POINT_LIMITS_W[call.data["point"]], CAR_LIMITS_W[vehicle])
                ):
                    raise ValueError("Simulated charging power exceeds connection/vehicle limits")
                point["power_w"] = power
        render(dt_util.utcnow())
        await store.async_save(data)

    for name, schema in (
        (
            "ev_connect",
            {
                vol.Required("point"): vol.In(range(len(points))),
                vol.Required("vehicle"): vol.In(range(len(cars))),
                vol.Required("connected"): bool,
            },
        ),
        (
            "ev_power",
            {
                vol.Required("point"): vol.In(range(len(points))),
                vol.Required("power_w"): vol.All(
                    finite_number,
                    vol.Range(min=0, max=max(POINT_LIMITS_W)),
                ),
            },
        ),
        (
            "ev_soc",
            {
                vol.Required("vehicle"): vol.In(range(len(cars))),
                vol.Required("soc"): vol.All(finite_number, vol.Range(min=0, max=100)),
            },
        ),
    ):
        hass.services.async_register(DOMAIN, name, action, schema=vol.Schema(schema))
        entry.async_on_unload(lambda service=name: hass.services.async_remove(DOMAIN, service))

    @callback
    def tick(at):
        """Persist energy/SoC at real timer ticks. Restart never replays an old simulated power command."""
        render(at)
        entry.async_create_task(hass, store.async_save(data), "Save simulated EV state")

    tick(now)
    entry.async_on_unload(async_track_time_interval(hass, tick, timedelta(seconds=REFRESH_SECONDS)))
