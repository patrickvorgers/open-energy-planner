"""Recorder-owned fine history for the disposable simulator, without direct database access."""

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_short_term_statistics,
    get_last_statistics,
)

from .calculation.recorder.history import align_statistics
from .policy import STATISTIC_MINUTES


async def import_fine(hass, metadata, statistics, hourly=None):
    """Queue validated simulator fine history and optional aligned hourly rows.

    Reuse the latest short-term sum-minus-state origin, falling back to retained
    hourly history. Only a new meter without either history uses negative final
    state as its offset. Recorder owns all queued writes. Source rows are copied."""
    if any(
        row["start"].tzinfo is None
        or row["start"].minute % STATISTIC_MINUTES
        or row["start"].second
        or row["start"].microsecond
        for row in statistics
    ):
        raise ValueError("Simulated fine statistics must start on aware five-minute boundaries")
    if not statistics:
        return
    recorder = get_instance(hass)
    latest = await recorder.async_add_executor_job(
        get_last_short_term_statistics,
        hass,
        1,
        metadata["statistic_id"],
        False,
        {"sum", "state"},
    )
    previous = latest.get(metadata["statistic_id"], [])
    if not previous:
        retained = await recorder.async_add_executor_job(
            get_last_statistics, hass, 1, metadata["statistic_id"], False, {"sum", "state"}
        )
        previous = retained.get(metadata["statistic_id"], [])
    offset = previous[0]["sum"] - previous[0]["state"] if previous else -statistics[-1]["state"]
    recorder.async_import_statistics(
        metadata, align_statistics(statistics, offset), StatisticsShortTerm
    )
    if hourly is not None:
        async_import_statistics(hass, metadata, align_statistics(hourly, offset))
