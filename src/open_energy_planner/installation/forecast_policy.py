"""Freshness of complete forecasts admitted to installation planning."""

from datetime import timedelta

from ..planning_policy import FINE_INTERVAL_MINUTES

FORECAST_MAX_AGE_MINUTES = 30


def next_update(now, retry_after=None):
    """Forecast workers reconsider changed quarter keys even before an older failed key's retry."""
    boundary = now.replace(
        minute=now.minute // FINE_INTERVAL_MINUTES * FINE_INTERVAL_MINUTES, second=0, microsecond=0
    ) + timedelta(minutes=FINE_INTERVAL_MINUTES)
    return min(boundary, retry_after) if retry_after and retry_after > now else boundary
