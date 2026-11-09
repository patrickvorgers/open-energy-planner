"""Compare complete local forecast days independently of models, storage and prices."""

from datetime import timedelta
from math import fsum

from open_energy_planner.core.calendar import day_window

HOUR = timedelta(hours=1)
MIN_ACCURACY_DAYS = 3
MAX_ACCURACY_LOOKBACK_DAYS = 30
MIN_RELATIVE_ACCURACY_GAIN = 0.05
MIN_ABSOLUTE_ACCURACY_GAIN_KWH = 0.001


def comparison_hours(day, timezone):
    """Return complete Recorder UTC hours inside a local day, including DST transitions."""
    start, end = day_window(day.isoformat(), timezone)
    cursor = start.replace(minute=0, second=0, microsecond=0)
    if cursor < start:
        cursor += HOUR
    hours = []
    while cursor + HOUR <= end:
        hours.append(cursor)
        cursor += HOUR
    return tuple(hours), end


def mean_absolute_error(predicted, actual):
    """Score identical complete hourly energy vectors in kWh without hiding missing values."""
    if not actual:
        raise ValueError("Complete daily measurements required")
    return fsum(
        abs(expected - measured) for expected, measured in zip(predicted, actual, strict=True)
    ) / len(actual)


def improves(candidate, baseline):
    """Require a material absolute and relative accuracy gain before changing models."""
    return baseline - candidate > max(
        MIN_ABSOLUTE_ACCURACY_GAIN_KWH, MIN_RELATIVE_ACCURACY_GAIN * baseline
    )
