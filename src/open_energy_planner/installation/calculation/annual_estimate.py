"""Validate compact setup history and estimate a provisional annual household demand."""

from datetime import UTC, datetime
from math import isfinite

from open_energy_planner.core.units import SECONDS_PER_DAY

MIN_ANNUAL_ESTIMATE_DAYS = 7
ANNUALIZATION_DAYS = 365
ESTIMATE_LOOKBACK_DAYS = ANNUALIZATION_DAYS
SUM_ENDPOINTS = 2
REFERENCE_ROLLOVER_GRACE_DAYS = 1


def _setup_endpoint(row):
    """Read one finite cumulative sum at an exact UTC day boundary."""
    if not isinstance(row, list) or len(row) != 2:
        raise ValueError("Invalid setup history endpoint")
    if any(type(value) not in (int, float) or not isfinite(value) for value in row):
        raise ValueError("Invalid setup history endpoint")
    timestamp, cumulative_kwh = row
    if timestamp % SECONDS_PER_DAY:
        raise ValueError("Invalid setup history endpoint")
    return timestamp, cumulative_kwh


def _setup_points(points):
    """Validate the two chronological endpoints of one bounded meter history."""
    if not isinstance(points, list) or len(points) != SUM_ENDPOINTS:
        raise ValueError("Invalid setup history points")
    start, first_kwh = _setup_endpoint(points[0])
    end, last_kwh = _setup_endpoint(points[1])
    if end <= start:
        raise ValueError("Invalid setup history endpoint")
    if end - start > ESTIMATE_LOOKBACK_DAYS * SECONDS_PER_DAY:
        raise ValueError("Setup history exceeds one year")
    return {start: first_kwh, end: last_kwh}


def _setup_channels(terms, history):
    """Read at most one channel for each requested household meter."""
    if not isinstance(history, list):
        raise TypeError("Invalid setup history")
    series = {}
    for item in history:
        if not isinstance(item, dict) or set(item) != {"entity_id", "points"}:
            raise ValueError("Invalid setup history channel")
        entity_id = item["entity_id"]
        if (
            entity_id not in {energy_term["entity_id"] for energy_term in terms}
            or entity_id in series
        ):
            raise ValueError("Unexpected setup history channel")
        series[entity_id] = _setup_points(item["points"])
    return series


def annual_estimate(terms, history, *, now=None):
    """Scale import+PV+discharge-export-charge from two common cumulative-sum endpoints.

    A complete 365-day period supplies measured consumption. Shorter periods are
    annualized. All meters must describe the same period. Corrections inside it
    are included in each end-minus-start difference. Negative period deltas are
    unusable. Detailed hourly forecasting remains a separate post-save workflow.
    """
    series = _setup_channels(terms, history)
    if not terms or set(series) != {energy_term["entity_id"] for energy_term in terms}:
        return None
    today = int((now or datetime.now(UTC)).timestamp()) // SECONDS_PER_DAY * SECONDS_PER_DAY
    start, end = next(iter(series.values()))
    if any(tuple(points) != (start, end) for points in series.values()):
        return None
    # A still-fresh catalogue can cross UTC midnight before its next refresh.
    if not (
        today - (ESTIMATE_LOOKBACK_DAYS + REFERENCE_ROLLOVER_GRACE_DAYS) * SECONDS_PER_DAY
        <= start
        < end
        <= today
    ):
        return None
    days = int((end - start) / SECONDS_PER_DAY)
    if days < MIN_ANNUAL_ESTIMATE_DAYS:
        return None
    deltas = {entity: points[end] - points[start] for entity, points in series.items()}
    household_kwh = sum(
        deltas[energy_term["entity_id"]] * energy_term["sign"] for energy_term in terms
    )
    if any(not isfinite(delta) or delta < 0 for delta in deltas.values()):
        return None
    if not isfinite(household_kwh) or household_kwh < 0:
        return None
    annual_kwh = round(household_kwh / days * ANNUALIZATION_DAYS, 1)
    if not isfinite(annual_kwh):
        return None
    return {
        "annual_kwh": annual_kwh,
        "days": days,
        "start": start,
        "end": end,
        "extrapolated": days < ANNUALIZATION_DAYS,
    }
