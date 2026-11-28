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
ENERGY_DISCOVERY_BASES = ("solar_history_range", "energy_history_range")
ENERGY_CHUNK_BASES = ("solar_history_chunk", "energy_history_chunk")

_LOGGER = logging.getLogger(__name__)


def _history_meters(request, entity):
    """Admit distinct sensor IDs while retaining the caller's channel order."""
    entities = request.get("entity_ids", [entity])
    if not isinstance(entities, list) or not entities:
        raise ValueError("Invalid history meter set")
    if any(not isinstance(item, str) or not item.startswith("sensor.") for item in entities):
        raise ValueError("Invalid history meter set")
    if len(set(entities)) != len(entities):
        raise ValueError("Invalid history meter set")
    return entities


def _validate_range(start, end, discovery):
    """Allow epoch-based monthly discovery or one bounded aware hourly range."""
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Invalid history request range")
    if discovery:
        valid = start == HISTORY_EPOCH and end > start
    else:
        valid = timedelta(0) < end - start <= timedelta(days=MAX_HISTORY_REQUEST_DAYS)
    if not valid:
        raise ValueError("Invalid history request range")


def _energy_metadata(hass, entities):
    """Fetch bounded batches of metadata and require supported cumulative energy."""
    metadata = {}
    for batch in channel_batches(entities):
        metadata.update(get_metadata(hass, statistic_ids=set(batch)))
    if any(
        not metadata.get(item)
        or not metadata[item][1]["has_sum"]
        or metadata[item][1]["unit_of_measurement"] not in ("Wh", "kWh", "MWh")
        for item in entities
    ):
        return None
    return metadata


def _statistics_identity(record):
    """Expose Recorder identity and source units independently of normalized row units."""
    metadata = record[1]
    return {
        "metadata_id": record[0],
        "source": metadata["source"],
        "unit": metadata["unit_of_measurement"],
    }


def _finite_number(value):
    """Require an actual finite Recorder number, excluding booleans."""
    return type(value) in (int, float) and isfinite(value)


def _first_available_hour(monthly, start, end):
    """Take the earliest finite monthly sum, allowing the Recorder's edge month."""
    lower = (start - timedelta(days=MAX_CALENDAR_MONTH_DAYS)).timestamp()
    upper = end.timestamp()
    stamps = []
    for row in monthly:
        stamp, cumulative = row.get("start"), row.get("sum")
        if not _finite_number(stamp) or not lower <= stamp < upper:
            continue
        if _finite_number(cumulative):
            stamps.append(stamp)
    if not stamps:
        return None
    first = datetime.fromtimestamp(min(stamps) // HOUR_SECONDS * HOUR_SECONDS, UTC)
    return max(HISTORY_EPOCH, first).isoformat()


def _discover_history(hass, entity, start, end, record, basis):
    """Read a bounded monthly projection of availability without returning raw rows."""
    monthly = statistics_during_period(
        hass, start, end, {entity}, "month", {"energy": "kWh"}, {"sum"}
    ).get(entity, [])
    expected_months = (end.year - start.year) * MONTHS_PER_YEAR + end.month - start.month + 2
    if len(monthly) > expected_months:
        return {"error": "history_unavailable"}
    available_start = _first_available_hour(monthly, start, end)
    return {
        "basis": basis,
        "entity_id": entity,
        "statistics_identity": _statistics_identity(record),
        "available_start": available_start,
    }


def _hourly_channels(hass, entities, start, end, chunk):
    """Read kWh sums and states, retaining one boundary hour on either side of a chunk."""
    query_start = max(HISTORY_EPOCH, start - timedelta(hours=1)) if chunk else start
    query_end = end + timedelta(hours=1) if chunk else end
    channels = {}
    for batch in channel_batches(entities):
        channels.update(
            statistics_during_period(
                hass,
                query_start,
                query_end,
                set(batch),
                "hour",
                {"energy": "kWh"},
                {"sum", "state"},
            )
        )
    return channels


def _hour_values(rows):
    """Keep Recorder order and the original cumulative sum and state of each hour."""
    return [[row["start"], row.get("sum"), row.get("state")] for row in rows]


def _statistics_history(channels, metadata, entities):
    """Return independent statistics, with an explicit missing-meter result."""
    if any(not channels.get(item) for item in entities):
        return {"error": "history_missing"}
    return {
        "statistics": [
            {
                "statistic_id": item,
                **_statistics_identity(metadata[item]),
                "hours": _hour_values(channels[item]),
            }
            for item in entities
        ],
    }


def _finite_chunk_rows(rows):
    """Forward every finite source value and leave alignment and admission to the app."""
    return [
        {
            field: row.get(field) if _finite_number(row.get(field)) else None
            for field in ("start", "sum", "state")
        }
        for row in rows
    ]


def _combined_meter_rows(channels, entities, rows):
    """Aggregate only aligned, finite and nondecreasing per-meter cumulative sums."""
    starts = [row["start"] for row in rows]
    if any([row["start"] for row in channels.get(item, [])] != starts for item in entities):
        return None
    if any(
        not _finite_number(row.get(field))
        for item in entities
        for row in channels[item]
        for field in ("sum", "state")
    ):
        return None
    if any(
        previous["sum"] > following["sum"]
        for item in entities
        for previous, following in zip(channels[item], channels[item][1:])
    ):
        return None
    return [
        {
            "start": row["start"],
            "sum": sum(channels[item][index]["sum"] for item in entities),
            "state": sum(channels[item][index]["state"] for item in entities),
        }
        for index, row in enumerate(rows)
    ]


def _meter_history(channels, metadata, entities, entity, chunk_basis):
    """Format the ordinary meter reply or a normalized single-meter energy chunk."""
    rows = channels.get(entities[0], [])
    if (not rows and not chunk_basis) or len(rows) > MAX_HISTORY_HOURS:
        return {"error": "history_unavailable"}
    evidence = {}
    if chunk_basis:
        rows = _finite_chunk_rows(rows)
    if len(entities) > 1:
        rows = _combined_meter_rows(channels, entities, rows)
        if rows is None:
            return {"error": "history_unavailable"}
        evidence["component_meters"] = [[item, channels[item][-1]["state"]] for item in entities]
    if chunk_basis:
        evidence.update(
            basis=chunk_basis, statistics_identity=_statistics_identity(metadata[entity])
        )
    return {**evidence, "entity_id": entity, "unit": "kWh", "hours": _hour_values(rows)}


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
        entities = _history_meters(request, entity)
        start, end = (datetime.fromisoformat(request[key]) for key in ("start", "end"))
        basis = request.get("basis")
        discovery = basis in ENERGY_DISCOVERY_BASES
        chunk = basis in ENERGY_CHUNK_BASES
        if (discovery or chunk) and entities != [entity]:
            raise ValueError("Energy history requires one meter")
        _validate_range(start, end, discovery)

        def read():
            """Dispatch to the matching statistics projection inside Recorder's executor."""
            metadata = _energy_metadata(self.hass, entities)
            if metadata is None:
                return {"error": "unsupported_statistics"}
            if discovery:
                return _discover_history(self.hass, entity, start, end, metadata[entity], basis)
            channels = _hourly_channels(self.hass, entities, start, end, chunk)
            if basis == "statistics_sum":
                return _statistics_history(channels, metadata, entities)
            return _meter_history(channels, metadata, entities, entity, basis if chunk else None)

        try:
            evidence = await asyncio.wait_for(
                get_instance(self.hass).async_add_executor_job(read),
                HISTORY_DISCOVERY_TIMEOUT_SECONDS if discovery else HISTORY_TIMEOUT_SECONDS,
            )
            return {**result, **evidence}
        except Exception:
            _LOGGER.warning(
                "Recorder energy history read failed. No history was returned for this request."
                if discovery or chunk
                else "Recorder history read failed. Manual bonus baseline remains available",
                exc_info=True,
            )
            return {**result, "error": "history_unavailable"}
