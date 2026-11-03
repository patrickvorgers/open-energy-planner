"""Transport bounded Energy-owned Recorder statistics without cycle or planning decisions."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from math import isfinite

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.util import dt as dt_util

from .client import AuthenticationError, ReleaseMismatchError

HISTORY_DAYS = 7
SAMPLE_MINUTES = 5
SAMPLE_SECONDS = SAMPLE_MINUTES * 60
REFRESH_SECONDS = 1800
HISTORY_TIMEOUT_SECONDS = 20
HISTORY_DISCOVERY_TIMEOUT_SECONDS = 60
HISTORY_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HOUR_SECONDS = 3600
DAY_SECONDS = 24 * HOUR_SECONDS
HOURLY_COMPILATION_DELAY_HOURS = 1
_LOGGER = logging.getLogger(__name__)


async def capture(hass, entity, power_entity, now):
    """Return paired fine statistics in kWh/W, keeping missing readings explicitly null."""
    end = now.replace(minute=now.minute // SAMPLE_MINUTES * SAMPLE_MINUTES, second=0, microsecond=0)
    start = end - timedelta(days=HISTORY_DAYS)
    return await capture_range(hass, entity, power_entity, start, end, "5minute")


async def capture_range(
    hass, entity, power_entity, start, end, period, *, timeout_seconds=HISTORY_TIMEOUT_SECONDS
):
    """Read one bounded range through Recorder, keeping missing and signed-power faults unknown."""
    sample_seconds = {"5minute": SAMPLE_SECONDS, "hour": HOUR_SECONDS, "day": DAY_SECONDS}[period]

    def read():
        """Query Recorder's public statistics API with its own unit conversion."""
        metadata = get_metadata(hass, statistic_ids={entity, power_entity})
        if (
            entity not in metadata
            or power_entity not in metadata
            or not metadata[entity][1]["has_sum"]
            or metadata[entity][1]["unit_of_measurement"] not in ("Wh", "kWh", "MWh")
            or metadata[power_entity][1]["unit_of_measurement"] not in ("W", "kW")
        ):
            return []
        history = statistics_during_period(
            hass,
            start,
            end,
            {entity, power_entity},
            period,
            {"energy": "kWh", "power": "W"},
            {"mean", "sum"},
        )
        energy = {}
        for row in history.get(entity, []):
            cumulative = row.get("sum")
            energy[row["start"]] = (
                cumulative if cumulative is None or isfinite(cumulative) else None
            )
        power = {}
        for row in history.get(power_entity, []):
            mean_power = row.get("mean")
            power[row["start"]] = (
                mean_power
                if mean_power is None or (isfinite(mean_power) and mean_power >= 0)
                else None
            )
        return [
            {
                "at": dt_util.utc_from_timestamp(stamp).isoformat(),
                "power_w": power.get(stamp),
                "kwh": energy.get(stamp),
            }
            for stamp in sorted(energy.keys() | power.keys())
            if stamp < end.timestamp()
            and (period == "day" or stamp + sample_seconds <= end.timestamp())
        ]

    return await asyncio.wait_for(get_instance(hass).async_add_executor_job(read), timeout_seconds)


async def synchronize(coordinator):
    """Schedule one cancellable background reader without holding command synchronization open."""
    previous = coordinator.appliance_history_task
    if coordinator.closed or (previous and not previous.done()):
        return
    # Consume actionable connection failures once through the coordinator's existing recovery handlers.
    coordinator.appliance_history_task = None
    if previous and not previous.cancelled():
        previous.result()
    coordinator.appliance_history_task = coordinator.hass.async_create_background_task(
        process(coordinator), "OEP appliance history learning"
    )


async def process(coordinator):
    """Resume durable hourly batches and refresh recent fine cycles, one selected device at a time."""
    now = dt_util.utcnow()
    attempts = coordinator.appliance_history_attempts
    specialized = set(coordinator.consumer_forecasts.get("charging_meters", [])) | set(
        coordinator.consumer_forecasts.get("thermal_meters", [])
    )
    options = {row["entity_id"]: row for row in coordinator.consumer_forecasts.get("options", [])}
    for selected in coordinator.consumer_forecasts.get("selected", []):
        if coordinator.closed:
            return
        entity = selected["entity_id"]
        if entity in specialized:
            continue
        power = options.get(entity, {}).get("power_entity")
        if not power:
            continue
        key = (entity, power)
        if key in attempts and (now - attempts[key]).total_seconds() < REFRESH_SECONDS:
            continue
        attempts[key] = now
        try:
            state = await coordinator.client.request("GET", "/v1/appliances")
            device = next((row for row in state["devices"] if row["entity_id"] == entity), None)
            if device is not None and device.get("power_entity") != power:
                continue
            states = (coordinator.hass.states.get(entity), coordinator.hass.states.get(power))
            synthetic = any(
                state is not None and state.attributes.get("synthetic") is True for state in states
            )
            try:
                rows = await capture(coordinator.hass, entity, power, now)
                await coordinator.client.request(
                    "POST",
                    "/v1/appliances/history",
                    {
                        "entity_id": entity,
                        "power_entity": power,
                        "rows": rows,
                        "synthetic": synthetic,
                    },
                )
            except (AuthenticationError, ReleaseMismatchError):
                raise
            except Exception:
                _LOGGER.debug("Fine appliance history unavailable for %s", entity, exc_info=True)
            await historical(coordinator, device or {}, entity, power, synthetic, now)
        except (AuthenticationError, ReleaseMismatchError):
            raise
        except Exception:
            _LOGGER.debug("Appliance history unavailable for %s", entity, exc_info=True)
            try:
                await coordinator.client.request(
                    "POST",
                    "/v1/appliances/history_scan",
                    {
                        "entity_id": entity,
                        "power_entity": power,
                        "phase": "failed",
                        "start": None,
                        "end": None,
                    },
                )
            except Exception:
                _LOGGER.debug("Unable to report appliance history failure", exc_info=True)


async def historical(coordinator, device, entity, power, synthetic, now):
    """Discover actual hourly coverage once, then follow app-owned checkpoint requests until complete."""
    client, hass = coordinator.client, coordinator.hass
    progress = device.get("history_import") or {}
    # Reserve one completed hour for closing quiet evidence at the final core boundary.
    available_end = now.replace(minute=0, second=0, microsecond=0)
    target = available_end - timedelta(hours=HOURLY_COMPILATION_DELAY_HOURS)

    async def status(phase, start=None, end=None):
        """Report only typed source-bound progress through the ordinary authenticated appliance API."""
        return await client.request(
            "POST",
            "/v1/appliances/history_scan",
            {
                "entity_id": entity,
                "power_entity": power,
                "phase": phase,
                "start": start,
                "end": end,
            },
        )

    first = progress.get("start")
    if not first:
        await status("discovering")
        # Daily aggregation discovers the available range without transporting years of hourly rows.
        available = await capture_range(
            hass,
            entity,
            power,
            HISTORY_EPOCH,
            target,
            "day",
            timeout_seconds=HISTORY_DISCOVERY_TIMEOUT_SECONDS,
        )
        paired = [row for row in available if row["power_w"] is not None and row["kwh"] is not None]
        if not paired:
            await status("unavailable")
            return
        earliest = datetime.fromisoformat(paired[0]["at"])
        first = datetime.fromtimestamp(
            int(earliest.timestamp()) // HOUR_SECONDS * HOUR_SECONDS, UTC
        ).isoformat()
        if datetime.fromisoformat(first) >= target:
            await status("unavailable")
            return
    response = await status("processing", first, target.isoformat())
    while not coordinator.closed:
        current = next(
            (row for row in response["appliances"]["devices"] if row["entity_id"] == entity), None
        )
        pending = current.get("history_request") if current else None
        if not pending:
            return
        start, end = (datetime.fromisoformat(pending[name]) for name in ("read_start", "read_end"))
        rows = await capture_range(
            hass,
            entity,
            power,
            start,
            min(end, available_end),
            "hour",
        )
        response = await client.request(
            "POST",
            "/v1/appliances/hourly_history",
            {
                "entity_id": entity,
                "power_entity": power,
                "rows": rows,
                "synthetic": synthetic,
                **pending,
            },
        )
        await asyncio.sleep(0)
