"""Read compact settlement evidence through native Recorder APIs, never direct SQL."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from math import isfinite

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.util import dt as dt_util

from .recorder_batches import channel_batches

QUARTER_SECONDS = 900
STATISTIC_SECONDS = 300
HOUR_SECONDS = 3600
MAX_CAPTURE_DAYS = 1
MAX_CAPTURE_INTERVALS = 96
MAX_CAPTURE_REQUEST_BACKOFFS = MAX_CAPTURE_INTERVALS
MAX_SETTLEMENT_REPORT_BYTES = 192 * 1024
CAPTURE_TIMEOUT_SECONDS = 30
RETRY_MINUTES = 15
# One micro-Wh absorbs unit-conversion roundoff without admitting real counter resets.
COUNTER_ROUNDING_KWH = 1e-9
# Match the app's settlement wire bounds before accepting a frame into the batch.
MAX_SETTLEMENT_ENERGY_KWH = 1_000_000
MAX_SETTLEMENT_METER_KWH = 1_000_000_000
_LOGGER = logging.getLogger(__name__)


class SettlementBridge:
    """Transfer requested AC energy meters, with explicit reconstruction quality and no SoC dependency."""

    def __init__(self, hass):
        """Keep retry bookkeeping local. Financial arithmetic and persistence belong to the app."""
        self.hass, self.retry_after = hass, {}

    async def capture(self, request):
        """Read at most a day. Prefer five-minute sums and label hourly interpolation explicitly."""
        now = dt_util.utcnow()
        self.retry_after = {
            version: deadline for version, deadline in self.retry_after.items() if now < deadline
        }
        if request["version"] in self.retry_after:
            return None
        if len(self.retry_after) >= MAX_CAPTURE_REQUEST_BACKOFFS:
            self.retry_after.pop(next(iter(self.retry_after)))
        self.retry_after[request["version"]] = now + timedelta(minutes=RETRY_MINUTES)
        start, end = (datetime.fromisoformat(request[key]) for key in ("start", "end"))
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or not timedelta(0) < end - start <= timedelta(days=MAX_CAPTURE_DAYS)
            or end > now
        ):
            raise ValueError("Invalid settlement capture range")
        manager = await async_get_manager(self.hass)
        imports, exports = [], []
        for source in (manager.data or {}).get("energy_sources", []):
            if source.get("type") == "grid":
                imports.extend(
                    [source["stat_energy_from"]]
                    if source.get("stat_energy_from")
                    else [row_item["stat_energy_from"] for row_item in source.get("flow_from", [])]
                )
                exports.extend(
                    [source["stat_energy_to"]]
                    if source.get("stat_energy_to")
                    else [row_item["stat_energy_to"] for row_item in source.get("flow_to", [])]
                )
        bindings = request["bindings"]
        if "imports" in bindings and "exports" in bindings:
            imports, exports = bindings["imports"], bindings["exports"]
        entities = (
            {energy_term["entity_id"] for energy_term in bindings["terms"]}
            | set(imports)
            | set(exports)
        )
        entities.update(solar_flow["entity_id"] for solar_flow in bindings["solar"])
        for battery_item in bindings["batteries"]:
            if not battery_item.get("energy_entity"):
                _LOGGER.warning(
                    "Settlement capture stopped at %s: Battery energy meter energy_entity "
                    "is required for %s: %r",
                    start.isoformat(),
                    battery_item["soc_entity"],
                    battery_item.get("energy_entity"),
                )
                return None
        entities.update(battery_item["energy_entity"] for battery_item in bindings["batteries"])
        if "recorder" not in self.hass.config.components:
            return None

        def read():
            """Use Recorder-owned execution, units and sum-reset handling for measured energy."""
            metadata = {}
            for batch in channel_batches(entities):
                metadata.update(get_metadata(self.hass, statistic_ids=set(batch)))
            usable = {
                entity_item
                for entity_item in entities
                if entity_item in metadata
                and metadata[entity_item][1]["has_sum"]
                and metadata[entity_item][1]["unit_of_measurement"] in ("Wh", "kWh", "MWh")
            }
            datasets = []
            for period, seconds in (("5minute", STATISTIC_SECONDS), ("hour", HOUR_SECONDS)):
                rows = {}
                for batch in channel_batches(usable):
                    rows.update(
                        statistics_during_period(
                            self.hass,
                            datetime.fromtimestamp(
                                int(start.timestamp()) // seconds * seconds - HOUR_SECONDS,
                                start.tzinfo,
                            ),
                            end,
                            set(batch),
                            period,
                            {"energy": "kWh"},
                            {"sum", "state"},
                        )
                    )
                datasets.append(
                    (
                        {
                            entity_item: {
                                row["start"] + seconds: row
                                for row in rows.get(entity_item, ())
                                if row.get("sum") is not None
                            }
                            for entity_item in entities
                        },
                        seconds,
                    )
                )

            def endpoint(entity, instant, interpolate=False):
                """Use true sum endpoints first. Hourly fallback retains its estimated classification."""
                if entity not in usable:
                    raise ValueError(f"Energy statistics metadata is unusable: {entity}")
                target = instant.timestamp()
                for data, seconds in datasets:
                    rows = data[entity]
                    if target in rows:
                        row = rows[target]
                        return (
                            float(row["sum"]),
                            float(row["state"]) if row.get("state") is not None else None,
                            "estimated_hourly" if seconds == HOUR_SECONDS else "recorded",
                        )
                    if interpolate:
                        left = target // seconds * seconds
                        start_counter_entry, end_counter_entry = (
                            rows.get(left),
                            rows.get(left + seconds),
                        )
                        if (
                            start_counter_entry
                            and end_counter_entry
                            and start_counter_entry.get("state") is not None
                            and end_counter_entry.get("state") is not None
                            and end_counter_entry["sum"]
                            >= start_counter_entry["sum"] - COUNTER_ROUNDING_KWH
                        ):
                            part = (target - left) / seconds
                            return (
                                start_counter_entry["sum"]
                                + (end_counter_entry["sum"] - start_counter_entry["sum"]) * part,
                                start_counter_entry["state"]
                                + (end_counter_entry["state"] - start_counter_entry["state"])
                                * part,
                                "estimated_hourly"
                                if seconds == HOUR_SECONDS
                                else "estimated_boundary",
                            )
                raise ValueError(
                    f"Settlement statistics have a gap: {entity} at {instant.isoformat()}"
                )

            frames, cursor, unavailable = [], start, False
            base_report = {
                "request_version": request["version"],
                "imports": imports,
                "exports": exports,
                "frames": [],
                "scanned_until": end.isoformat(),
            }
            report_bytes = len(json.dumps(base_report).encode())
            for _ in range(MAX_CAPTURE_INTERVALS):
                if cursor >= end:
                    break
                boundary = datetime.fromtimestamp(
                    (int(cursor.timestamp()) // QUARTER_SECONDS + 1) * QUARTER_SECONDS,
                    cursor.tzinfo,
                )
                right = min(end, boundary)
                channels, meters, quality = {}, {}, "recorded"
                try:
                    for entity in sorted(entities):
                        start_counter, meter, qa = endpoint(entity, cursor, True)
                        end_counter, _, qb = endpoint(entity, right, True)
                        delta = end_counter - start_counter
                        if (
                            delta < -COUNTER_ROUNDING_KWH
                            or delta > MAX_SETTLEMENT_ENERGY_KWH
                            or not isfinite(delta)
                            or (
                                meter is not None
                                and (
                                    not isfinite(meter)
                                    or not 0 <= meter <= MAX_SETTLEMENT_METER_KWH
                                )
                            )
                        ):
                            raise ValueError(f"Invalid settlement sum: {entity}")
                        channels[entity] = max(0, delta)
                        if meter is not None:
                            meters[entity] = meter
                        for candidate in (qa, qb):
                            if candidate == "estimated_hourly" or (
                                candidate == "estimated_boundary" and quality == "recorded"
                            ):
                                quality = candidate
                    frame = {
                        "start": cursor.isoformat(),
                        "end": right.isoformat(),
                        "channels": channels,
                        "battery_start": {},
                        "battery_end": {},
                        "meter_start": meters,
                        "quality": quality,
                    }
                    added_bytes = len(json.dumps(frame, allow_nan=False).encode()) + (
                        2 if frames else 0
                    )
                    if report_bytes + added_bytes > MAX_SETTLEMENT_REPORT_BYTES:
                        break
                    frames.append(frame)
                    report_bytes += added_bytes
                except (ValueError, TypeError) as error:
                    if not unavailable:
                        _LOGGER.warning(
                            "Settlement interval unavailable at %s: %s. Later intervals continue. "
                            "Missing measurements remain uncovered and are retried during daily recovery.",
                            cursor.isoformat(),
                            error,
                        )
                    unavailable = True
                cursor = right
            return (
                {
                    "request_version": request["version"],
                    "imports": imports,
                    "exports": exports,
                    "frames": frames,
                    "scanned_until": cursor.isoformat(),
                }
                if cursor > start
                else None
            )

        try:
            return await asyncio.wait_for(
                get_instance(self.hass).async_add_executor_job(read), CAPTURE_TIMEOUT_SECONDS
            )
        except Exception:
            _LOGGER.warning(
                "Settlement history unavailable. Comparison coverage remains incomplete",
                exc_info=True,
            )
            return None
