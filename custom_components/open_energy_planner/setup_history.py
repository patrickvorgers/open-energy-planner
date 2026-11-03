"""Read shared start/end energy sums for setup through native Recorder APIs."""

import asyncio
import logging
from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.util import dt as dt_util

SECONDS_PER_DAY = 86400
HISTORY_TIMEOUT_SECONDS = 15

SETUP_HISTORY_LOOKBACK_DAYS = 365
SETUP_SUM_ENDPOINTS = 2
HOURLY_STATISTIC_PERIOD = timedelta(hours=1)

_LOGGER = logging.getLogger(__name__)


async def setup_history(hass, terms):
    """Return only shared start/end sums per AC meter. The app owns annual-demand estimation.

    Probe the two year boundaries first. Missing boundaries require a bounded
    history search for the widest common period, not an assumption of zero use.
    """
    if not terms or "recorder" not in hass.config.components:
        return []
    end = dt_util.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=SETUP_HISTORY_LOOKBACK_DAYS)
    ids = {term["entity_id"] for term in terms}

    def read():
        """Use HA's executor and unit conversion. Missing statistics remain absent rather than zero."""
        metadata = get_metadata(hass, statistic_ids=ids)
        if any(
            entity not in metadata
            or not metadata[entity][1]["has_sum"]
            or metadata[entity][1]["unit_of_measurement"] not in ("Wh", "kWh", "MWh")
            for entity in ids
        ):
            return []

        def points_between(first_reference, last_reference):
            """Keep real UTC midnight sums in kWh without interpolating missing references."""
            rows = statistics_during_period(
                hass, first_reference, last_reference, ids, "hour", {"energy": "kWh"}, {"sum"}
            )
            return {
                entity: {
                    row["end"]: row["sum"]
                    for row in rows.get(entity, [])
                    if row.get("sum") is not None and row["end"] % SECONDS_PER_DAY == 0
                }
                for entity in ids
            }

        points = {entity: {} for entity in ids}
        for boundary in (start, end):
            # Recorder's cumulative sum belongs to the completed hour's end.
            for entity, values in points_between(
                boundary - HOURLY_STATISTIC_PERIOD, boundary
            ).items():
                points[entity].update(values)
        if any(len(values) != SETUP_SUM_ENDPOINTS for values in points.values()):
            points = points_between(start - HOURLY_STATISTIC_PERIOD, end)
        common = set.intersection(*(set(values) for values in points.values()))
        if len(common) < SETUP_SUM_ENDPOINTS:
            return []
        first, last = min(common), max(common)
        return [
            {
                "entity_id": entity,
                "points": [[instant, points[entity][instant]] for instant in (first, last)],
            }
            for entity in sorted(ids)
        ]

    try:
        return await asyncio.wait_for(
            get_instance(hass).async_add_executor_job(read), HISTORY_TIMEOUT_SECONDS
        )
    except Exception:
        _LOGGER.debug("Setup history unavailable. Annual estimate may be required", exc_info=True)
        return []
