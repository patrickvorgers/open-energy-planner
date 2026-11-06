"""Read compact settlement evidence through native Recorder APIs, never direct SQL."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from math import ceil, isfinite

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.util import dt as dt_util

from .recorder_batches import channel_batches

QUARTER_SECONDS = 900
STATISTIC_SECONDS = 300
HOUR_SECONDS = 3600
MAX_CAPTURE_DAYS = 1
MAX_CAPTURE_INTERVALS = 96
MAX_SETTLEMENT_REPORT_BYTES = 192 * 1024
CAPTURE_TIMEOUT_SECONDS = 30
RETRY_MINUTES = 15
FULL_SOC_PERCENT = 100
# One micro-Wh absorbs unit-conversion roundoff without admitting real counter resets.
COUNTER_ROUNDING_KWH = 1e-9
MAX_IDLE_SOC_GAP = timedelta(hours=1)
_LOGGER = logging.getLogger(__name__)


class SettlementBridge:
    """Transfer only requested Energy meters and battery endpoints, with explicit reconstruction quality."""

    def __init__(self, hass):
        """Keep retry bookkeeping local. Financial arithmetic and persistence belong to the app."""
        self.hass, self.version, self.retry_after = hass, None, None

    async def capture(self, request):
        """Read at most a day. Prefer five-minute sums and label hourly interpolation explicitly."""
        now = dt_util.utcnow()
        if self.version == request["version"] and self.retry_after and now < self.retry_after:
            return None
        self.version, self.retry_after = request["version"], now + timedelta(minutes=RETRY_MINUTES)
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
        batteries = bindings["batteries"]

        def read():
            """Use Recorder-owned execution, units, sum-reset handling and historical SoC state."""
            metadata = {}
            for batch in channel_batches(entities):
                metadata.update(get_metadata(self.hass, statistic_ids=set(batch)))
            for entity_item in sorted(entities):
                if (
                    entity_item not in metadata
                    or not metadata[entity_item][1]["has_sum"]
                    or metadata[entity_item][1]["unit_of_measurement"] not in ("Wh", "kWh", "MWh")
                ):
                    _LOGGER.warning(
                        "Settlement capture stopped at %s: Energy statistics metadata is unusable: %s",
                        start.isoformat(),
                        entity_item,
                    )
                    return None
            datasets = []
            for period, seconds in (("5minute", STATISTIC_SECONDS), ("hour", HOUR_SECONDS)):
                rows = {}
                for batch in channel_batches(entities):
                    rows.update(
                        statistics_during_period(
                            self.hass,
                            start - timedelta(hours=1),
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
            soc = {}
            for batch in channel_batches(battery_item["soc_entity"] for battery_item in batteries):
                soc.update(
                    get_significant_states(
                        self.hass,
                        start - MAX_IDLE_SOC_GAP,
                        end + timedelta(seconds=1),
                        batch,
                        include_start_time_state=True,
                        significant_changes_only=False,
                    )
                )

            def endpoint(entity, instant, interpolate=False):
                """Use true sum endpoints first. Hourly fallback retains its estimated classification."""
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

            def soc_percentage(state, entity):
                """Validate a recorded SoC sample and identify its source in every failure.

                Args:
                    state: Historical HA state, or None when no as-of sample exists.
                    entity: Configured battery SoC entity ID used only for diagnostics.

                Returns:
                    A finite percentage within the battery's physical 0..100 range.

                Raises:
                    ValueError: The sample or percentage unit is missing, or its
                        value is nonnumeric, nonfinite or outside the physical range.
                """
                if state is None or state.attributes.get("unit_of_measurement") != "%":
                    raise ValueError(f"Historical battery state is missing: {entity}")
                try:
                    value = float(state.state)
                except (TypeError, ValueError):
                    raise ValueError(f"Historical battery state is invalid: {entity}") from None
                if not isfinite(value) or not 0 <= value <= FULL_SOC_PERCENT:
                    raise ValueError(f"Historical battery state is invalid: {entity}")
                return value

            def stored(instant):
                """Read historical SoC at an aware frame boundary.

                Bridge unknown or unavailable spans up to MAX_IDLE_SOC_GAP only
                with equal valid bounding percentages and unchanged charge and
                discharge sums over the enclosing five-minute boundaries.

                Args:
                    instant: Aware historical frame boundary, never a current-state lookup.

                Returns:
                    Stored kWh by virtual battery ID and a boolean indicating
                    whether any battery endpoint was reconstructed.

                Raises:
                    ValueError: Historical SoC or required meter evidence is
                        missing, invalid or inconsistent, or the outage is too long.
                """
                result, reconstructed = {}, False
                for battery in batteries:
                    history = soc.get(battery["soc_entity"], ())
                    state = next(
                        (
                            source_item
                            for source_item in reversed(history)
                            if source_item.last_updated <= instant
                        ),
                        None,
                    )
                    if state is not None and state.state in ("unknown", "unavailable"):
                        before = next(
                            (
                                soc_state
                                for soc_state in reversed(history)
                                if soc_state.last_updated < state.last_updated
                                and soc_state.state not in ("unknown", "unavailable")
                            ),
                            None,
                        )
                        after = next(
                            (
                                soc_state
                                for soc_state in history
                                if soc_state.last_updated > instant
                                and soc_state.state not in ("unknown", "unavailable")
                            ),
                            None,
                        )
                        missing_since = next(
                            (
                                soc_state.last_updated
                                for soc_state in history
                                if before is not None
                                and before.last_updated < soc_state.last_updated <= instant
                                and soc_state.state in ("unknown", "unavailable")
                            ),
                            state.last_updated,
                        )
                        if (
                            before is None
                            or after is None
                            or after.last_updated - missing_since > MAX_IDLE_SOC_GAP
                            or soc_percentage(before, battery["soc_entity"])
                            != soc_percentage(after, battery["soc_entity"])
                        ):
                            raise ValueError(
                                f"Historical battery state is missing: {battery['soc_entity']}"
                            )
                        left = datetime.fromtimestamp(
                            int(missing_since.timestamp()) // STATISTIC_SECONDS * STATISTIC_SECONDS,
                            instant.tzinfo,
                        )
                        right = datetime.fromtimestamp(
                            ceil(after.last_updated.timestamp() / STATISTIC_SECONDS)
                            * STATISTIC_SECONDS,
                            instant.tzinfo,
                        )
                        for meter_key, meter_entity in (
                            ("charge_energy_entity", battery.get("charge_energy_entity")),
                            ("energy_entity", battery["energy_entity"]),
                        ):
                            if meter_entity not in entities:
                                raise ValueError(
                                    f"Battery energy meter {meter_key} is required to prove "
                                    f"idle SoC for {battery['soc_entity']}: {meter_entity!r}"
                                )
                            initial, _, _ = endpoint(meter_entity, left, True)
                            final, _, _ = endpoint(meter_entity, right, True)
                            if (
                                not isfinite(final - initial)
                                or abs(final - initial) > COUNTER_ROUNDING_KWH
                            ):
                                raise ValueError(
                                    f"Battery energy changed during unavailable SoC: {meter_entity}"
                                )
                        state, reconstructed = before, True
                    value = soc_percentage(state, battery["soc_entity"])
                    result[battery["device_id"]] = (
                        value / FULL_SOC_PERCENT * battery["capacity_kwh"]
                    )
                return result, reconstructed

            frames, cursor = [], start
            base_report = {
                "request_version": request["version"],
                "imports": imports,
                "exports": exports,
                "frames": [],
            }
            report_bytes = len(json.dumps(base_report).encode())
            while cursor < end and len(frames) < MAX_CAPTURE_INTERVALS:
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
                        if delta < -COUNTER_ROUNDING_KWH or not isfinite(delta):
                            raise ValueError(f"Invalid settlement sum: {entity}")
                        channels[entity] = max(0, delta)
                        if meter is not None:
                            meters[entity] = meter
                        for candidate in (qa, qb):
                            if candidate == "estimated_hourly" or (
                                candidate == "estimated_boundary" and quality == "recorded"
                            ):
                                quality = candidate
                    battery_start, start_reconstructed = stored(cursor)
                    battery_end, end_reconstructed = stored(right)
                    frame = {
                        "start": cursor.isoformat(),
                        "end": right.isoformat(),
                        "channels": channels,
                        "battery_start": battery_start,
                        "battery_end": battery_end,
                        "meter_start": meters,
                        "quality": "estimated_soc"
                        if start_reconstructed or end_reconstructed
                        else quality,
                    }
                    added_bytes = len(json.dumps(frame, allow_nan=False).encode()) + (
                        2 if frames else 0
                    )
                    if report_bytes + added_bytes > MAX_SETTLEMENT_REPORT_BYTES:
                        break
                    frames.append(frame)
                    report_bytes += added_bytes
                except (ValueError, TypeError) as error:
                    _LOGGER.warning(
                        "Settlement capture stopped at %s: %s", cursor.isoformat(), error
                    )
                    break
                cursor = right
            return (
                {
                    "request_version": request["version"],
                    "imports": imports,
                    "exports": exports,
                    "frames": frames,
                }
                if frames
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
