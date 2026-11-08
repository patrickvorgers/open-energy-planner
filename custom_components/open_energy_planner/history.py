"""Optional read-only Recorder adapter, isolated from financial calculations."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from math import isfinite

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.util import dt as dt_util

from .recorder_batches import channel_batches

HISTORY_TIMEOUT_SECONDS = 15

HISTORY_RETRY_MINUTES = 15
MAX_HISTORY_REQUEST_DAYS = 367

MAX_HISTORY_HOURS = 9000
HISTORY_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HISTORY_DISCOVERY_TIMEOUT_SECONDS = 60
HOUR_SECONDS = 3600
MONTHS_PER_YEAR = 12
MAX_CALENDAR_MONTH_DAYS = 31

_LOGGER = logging.getLogger(__name__)


class HistoryBridge:
    """Read normalized hourly energy statistics only when the app requests missing evidence."""

    def __init__(self, hass):
        """Keep entry-local retry state. The app owns baseline persistence and arithmetic."""
        self.hass = hass
        self.last_version = None
        self.retry_after = None

    async def capture(self, request):
        """Return compact kWh rows or an explicit failure, with a fifteen-minute retry delay.

        Recorder owns the executor and its database. No SQL, HA credentials or
        database connection details cross the local app boundary.
        """
        now = dt_util.utcnow()
        if self.last_version == request["version"] and self.retry_after and now < self.retry_after:
            return None
        self.last_version = request["version"]
        self.retry_after = now + timedelta(minutes=HISTORY_RETRY_MINUTES)
        result = {"request_version": request["version"]}
        if "recorder" not in self.hass.config.components:
            return {**result, "error": "recorder_unavailable"}
        entity = request["entity_id"]
        entities = request.get("entity_ids", [entity])
        if (
            not isinstance(entities, list)
            or not entities
            or any(not isinstance(item, str) or not item.startswith("sensor.") for item in entities)
            or len(set(entities)) != len(entities)
        ):
            raise ValueError("Invalid history meter set")
        start, end = (datetime.fromisoformat(request[key]) for key in ("start", "end"))
        solar_discovery = request.get("basis") == "solar_history_range"
        solar_chunk = request.get("basis") == "solar_history_chunk"
        if (solar_discovery or solar_chunk) and entities != [entity]:
            raise ValueError("Solar history requires one meter")
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or (
                not (start == HISTORY_EPOCH and end > start)
                if solar_discovery
                else not timedelta(0) < end - start <= timedelta(days=MAX_HISTORY_REQUEST_DAYS)
            )
        ):
            raise ValueError("Invalid history request range")

        def read():
            """Read only supported energy sums through HA's own Recorder statistics API."""
            metadata = {}
            for batch in channel_batches(entities):
                metadata.update(get_metadata(self.hass, statistic_ids=set(batch)))
            if any(
                not metadata.get(item)
                or not metadata[item][1]["has_sum"]
                or metadata[item][1]["unit_of_measurement"] not in ("Wh", "kWh", "MWh")
                for item in entities
            ):
                return {**result, "error": "unsupported_statistics"}
            if solar_discovery:
                monthly = statistics_during_period(
                    self.hass, start, end, {entity}, "month", {"energy": "kWh"}, {"sum"}
                ).get(entity, [])
                expected_months = (
                    (end.year - start.year) * MONTHS_PER_YEAR + end.month - start.month + 2
                )
                if len(monthly) > expected_months:
                    return {**result, "error": "history_unavailable"}
                stamps = []
                for row in monthly:
                    stamp, cumulative = row.get("start"), row.get("sum")
                    if (
                        type(stamp) not in (int, float)
                        or not isfinite(stamp)
                        or not (start - timedelta(days=MAX_CALENDAR_MONTH_DAYS)).timestamp()
                        <= stamp
                        < end.timestamp()
                    ):
                        return {**result, "error": "history_unavailable"}
                    if type(cumulative) in (int, float) and isfinite(cumulative):
                        stamps.append(stamp)
                first = (
                    max(
                        HISTORY_EPOCH,
                        datetime.fromtimestamp(min(stamps) // HOUR_SECONDS * HOUR_SECONDS, UTC),
                    )
                    if stamps
                    else None
                )
                return {
                    **result,
                    "basis": "solar_history_range",
                    "entity_id": entity,
                    "statistics_identity": {
                        "metadata_id": metadata[entity][0],
                        "source": metadata[entity][1]["source"],
                        "unit": metadata[entity][1]["unit_of_measurement"],
                    },
                    "available_start": first.isoformat() if first else None,
                }
            channels = {}
            for batch in channel_batches(entities):
                channels.update(
                    statistics_during_period(
                        self.hass,
                        start,
                        end,
                        set(batch),
                        "hour",
                        {"energy": "kWh"},
                        {"sum", "state"},
                    )
                )
            if request.get("basis") == "statistics_sum":
                if any(not channels.get(item) for item in entities):
                    return {**result, "error": "history_missing"}
                return {
                    **result,
                    "statistics": [
                        {
                            "statistic_id": item,
                            "metadata_id": metadata[item][0],
                            "source": metadata[item][1]["source"],
                            "unit": metadata[item][1]["unit_of_measurement"],
                            "hours": [
                                [row["start"], row.get("sum"), row.get("state")]
                                for row in channels[item]
                            ],
                        }
                        for item in entities
                    ],
                }
            rows = channels.get(entities[0], [])
            if (not rows and not solar_chunk) or len(rows) > MAX_HISTORY_HOURS:
                return {**result, "error": "history_unavailable"}
            if solar_chunk:
                if any(
                    type(row.get("start")) not in (int, float)
                    or not isfinite(row["start"])
                    or row["start"] % HOUR_SECONDS
                    or not start.timestamp() <= row["start"] < end.timestamp()
                    for row in rows
                ):
                    return {**result, "error": "history_unavailable"}
                # Absent/nonfinite endpoints stay gaps. They cannot become zero energy.
                rows = [
                    row
                    for row in rows
                    if all(
                        type(row.get(field)) in (int, float) and isfinite(row[field])
                        for field in ("sum", "state")
                    )
                ]
            if len(entities) > 1:
                if any(
                    [row["start"] for row in channels.get(item, [])]
                    != [row["start"] for row in rows]
                    for item in entities
                ):
                    return {**result, "error": "history_unavailable"}
                if any(
                    type(row.get(field)) not in (int, float) or not isfinite(row[field])
                    for item in entities
                    for row in channels[item]
                    for field in ("sum", "state")
                ):
                    return {**result, "error": "history_unavailable"}
                if any(
                    previous["sum"] > following["sum"]
                    for item in entities
                    for previous, following in zip(channels[item], channels[item][1:])
                ):
                    return {**result, "error": "history_unavailable"}
                rows = [
                    {
                        "start": row["start"],
                        "sum": sum(channels[item][index]["sum"] for item in entities),
                        "state": sum(channels[item][index]["state"] for item in entities),
                    }
                    for index, row in enumerate(rows)
                ]
            return {
                **result,
                **(
                    {"component_meters": [[item, channels[item][-1]["state"]] for item in entities]}
                    if len(entities) > 1
                    else {}
                ),
                **(
                    {
                        "basis": "solar_history_chunk",
                        "statistics_identity": {
                            "metadata_id": metadata[entity][0],
                            "source": metadata[entity][1]["source"],
                            "unit": metadata[entity][1]["unit_of_measurement"],
                        },
                    }
                    if solar_chunk
                    else {}
                ),
                "entity_id": entity,
                "unit": "kWh",
                "hours": [[row["start"], row.get("sum"), row.get("state")] for row in rows],
            }

        try:
            return await asyncio.wait_for(
                get_instance(self.hass).async_add_executor_job(read),
                HISTORY_DISCOVERY_TIMEOUT_SECONDS if solar_discovery else HISTORY_TIMEOUT_SECONDS,
            )
        except Exception:
            _LOGGER.warning(
                "Recorder history read failed. Manual bonus baseline remains available",
                exc_info=True,
            )
            return {**result, "error": "history_unavailable"}
