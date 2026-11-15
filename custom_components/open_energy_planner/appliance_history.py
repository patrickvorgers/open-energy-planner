"""Transport bounded Energy-owned Recorder statistics without cycle or planning decisions."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from math import isfinite

from aiohttp import ClientError
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.util import dt as dt_util

from .client import AuthenticationError, ReleaseMismatchError

HISTORY_DAYS = 7
SAMPLE_MINUTES = 5
SAMPLE_SECONDS = SAMPLE_MINUTES * 60
REFRESH_SECONDS = 300
RETRY_SECONDS = 60
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
    if "recorder" not in hass.config.components:
        return []
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


async def synchronize(coordinator, _now=None):
    """Run independently of command synchronization and drain the previous bounded reader."""
    previous = coordinator.appliance_history_task
    if coordinator.closed or (previous and not previous.done()):
        return
    coordinator.appliance_history_task = None
    if previous and not previous.cancelled():
        try:
            previous.result()
        except (AuthenticationError, ReleaseMismatchError):
            raise
        except (ClientError, OSError, ValueError, RuntimeError):
            _LOGGER.warning(
                "Appliance history refresh failed. Retained profiles remain available. Retrying in one minute"
            )
    coordinator.appliance_history_task = coordinator.hass.async_create_background_task(
        process(coordinator), "OEP appliance history learning"
    )


async def process(coordinator):
    """Refresh recent evidence first, then one bounded batch per source and resolution fairly."""
    if coordinator.closed:
        return
    now = dt_util.utcnow()
    consumers = await coordinator.client.request("GET", "/v1/consumers?view=settings")
    state = await coordinator.client.request("GET", "/v1/appliances")
    specialized = set(consumers.get("charging_meters", [])) | set(
        consumers.get("thermal_meters", [])
    )
    options = {row["entity_id"]: row for row in consumers.get("options", [])}
    devices = {row["entity_id"]: row for row in state["devices"]}
    for selected in consumers.get("selected", []):
        if coordinator.closed:
            return
        entity = selected["entity_id"]
        if entity in specialized or selected.get("weather_sensitive"):
            continue
        power = options.get(entity, {}).get("power_entity")
        if not power:
            continue
        key = (entity, power)
        attempts = coordinator.appliance_history_attempts
        if key in attempts and now < attempts[key]:
            continue
        device = devices.get(entity, {})
        if device and device.get("power_entity") != power:
            continue
        states = (coordinator.hass.states.get(entity), coordinator.hass.states.get(power))
        synthetic = any(
            item is not None and item.attributes.get("synthetic") is True for item in states
        )
        attempts[key] = now + timedelta(seconds=RETRY_SECONDS)
        try:
            # Recent hourly evidence is useful before years of older batches finish.
            hourly_end = now.replace(minute=0, second=0, microsecond=0)
            for period, start, end in (
                ("hour", hourly_end - timedelta(days=HISTORY_DAYS), hourly_end),
                (
                    "5minute",
                    now.replace(
                        minute=now.minute // SAMPLE_MINUTES * SAMPLE_MINUTES,
                        second=0,
                        microsecond=0,
                    )
                    - timedelta(days=HISTORY_DAYS),
                    now.replace(
                        minute=now.minute // SAMPLE_MINUTES * SAMPLE_MINUTES,
                        second=0,
                        microsecond=0,
                    ),
                ),
            ):
                recent_key = (*key, period)
                if recent_key in attempts and now < attempts[recent_key]:
                    continue
                try:
                    rows = await capture_range(coordinator.hass, entity, power, start, end, period)
                    await coordinator.client.request(
                        "POST",
                        "/v1/appliances/history",
                        {
                            "entity_id": entity,
                            "power_entity": power,
                            "rows": rows,
                            "synthetic": synthetic,
                            "resolution": period,
                        },
                    )
                    attempts[recent_key] = now + timedelta(seconds=REFRESH_SECONDS)
                except (AuthenticationError, ReleaseMismatchError):
                    raise
                except (ClientError, OSError, ValueError, RuntimeError):
                    _LOGGER.debug(
                        "Recent %s appliance history unavailable for %s. Retaining accepted evidence and continuing historical scan",
                        period,
                        entity,
                    )
            raw_key = (*key, "raw_states")
            if raw_key not in attempts or now >= attempts[raw_key]:
                from .appliance_raw_history import capture as capture_raw

                attempts[raw_key] = now + timedelta(seconds=REFRESH_SECONDS)
                try:
                    raw = await capture_raw(coordinator, entity, power, synthetic)
                    if raw is not None and not coordinator.closed:
                        await coordinator.client.request("POST", "/v1/appliances/raw_history", raw)
                except (AuthenticationError, ReleaseMismatchError):
                    raise
                except (ClientError, OSError, ValueError, RuntimeError):
                    _LOGGER.debug(
                        "Raw appliance state history unavailable for %s. Retaining statistics and continuing historical scan",
                        entity,
                    )
            pending = await historical(coordinator, device, entity, power, synthetic, now)
            attempts[key] = now + timedelta(seconds=RETRY_SECONDS if pending else REFRESH_SECONDS)
        except (AuthenticationError, ReleaseMismatchError):
            raise
        except (ClientError, OSError, ValueError, RuntimeError):
            _LOGGER.warning(
                "Appliance history import failed for %s. Retained profiles remain available. Check its Energy sensor pair. Retrying in one minute",
                entity,
            )
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
            except (AuthenticationError, ReleaseMismatchError):
                raise
            except (ClientError, OSError, ValueError, RuntimeError):
                _LOGGER.debug("Unable to report appliance history failure for %s", entity)


async def historical(coordinator, device, entity, power, synthetic, now):
    """Discover paired coverage and scan hourly forward and fine backward without assuming retention."""
    client, hass = coordinator.client, coordinator.hass
    progress = device.get("history_import") or {}
    available_end = now.replace(minute=0, second=0, microsecond=0)
    target = available_end - timedelta(hours=HOURLY_COMPILATION_DELAY_HOURS)

    async def status(phase, start=None, end=None, *, resolution="hour"):
        """Report source-bound checkpoints through the authenticated appliance API."""
        return await client.request(
            "POST",
            "/v1/appliances/history_scan",
            {
                "entity_id": entity,
                "power_entity": power,
                "phase": phase,
                "start": start,
                "end": end,
                "resolution": resolution,
            },
        )

    first = progress.get("start")
    if not first:
        await status("discovering")
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
            return False
        earliest = datetime.fromisoformat(paired[0]["at"])
        first = datetime.fromtimestamp(
            int(earliest.timestamp()) // HOUR_SECONDS * HOUR_SECONDS, UTC
        ).isoformat()
        if datetime.fromisoformat(first) >= target:
            await status("unavailable")
            return False
    pending_work = False
    for resolution, request_field, operation in (
        ("hour", "history_request", "hourly_history"),
        ("5minute", "fine_history_request", "fine_history"),
    ):
        if coordinator.closed:
            return pending_work
        response = await status("processing", first, target.isoformat(), resolution=resolution)
        current = next(
            (row for row in response["appliances"]["devices"] if row["entity_id"] == entity), {}
        )
        pending = current.get(request_field)
        if not pending:
            continue
        start, end = (datetime.fromisoformat(pending[name]) for name in ("read_start", "read_end"))
        try:
            rows = await capture_range(
                hass, entity, power, start, min(end, available_end), resolution
            )
            response = await client.request(
                "POST",
                "/v1/appliances/" + operation,
                {
                    "entity_id": entity,
                    "power_entity": power,
                    "rows": rows,
                    "synthetic": synthetic,
                    **pending,
                },
            )
            current = next(
                (row for row in response["appliances"]["devices"] if row["entity_id"] == entity), {}
            )
            pending_work |= bool(current.get(request_field))
        except (AuthenticationError, ReleaseMismatchError):
            raise
        except (ClientError, OSError, ValueError, RuntimeError):
            await status("failed", resolution=resolution)
            pending_work = True
    return pending_work
