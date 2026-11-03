"""Shared planning horizon, forecast freshness and resolution. Providers retain their access limits."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.core.units import SECONDS_PER_HOUR

PLANNING_HORIZON_HOURS = 48
FINE_RESOLUTION_HOURS = 24
FINE_INTERVAL_MINUTES = 15
COARSE_INTERVAL_MINUTES = 60
TERMINAL_VALUATION_HOURS = 24
COMMAND_GRACE_MINUTES = 5
PRICE_COVERAGE_HOURS = PLANNING_HORIZON_HOURS + TERMINAL_VALUATION_HOURS


def planning_grid(now: datetime, *, align: bool = True) -> tuple[tuple[datetime, float], ...]:
    """Keep an exact elapsed horizon and switch to coarse steps on a UTC clock hour.

    Normal forecasts start on the current quarter. Keep fine steps for at least
    24 hours, through the next whole hour. Clip the last coarse step to the
    horizon rather than extending it. Explicit synthetic starts remain exact.

    Args:
        now: Timezone-aware planning instant.
        align: Align the start down to its current UTC quarter when true.

    Returns:
        Ordered pairs containing each UTC start and its duration in elapsed hours.

    Raises:
        ValueError: The planning instant has no timezone.
    """
    if now.utcoffset() is None:
        raise ValueError("Planning timestamps require a timezone")
    start = now.astimezone(UTC)
    if align:
        start = start.replace(
            minute=start.minute // FINE_INTERVAL_MINUTES * FINE_INTERVAL_MINUTES,
            second=0,
            microsecond=0,
        )
    end = start + timedelta(hours=PLANNING_HORIZON_HOURS)
    switch = start + timedelta(hours=FINE_RESOLUTION_HOURS)
    clock_hour = switch.replace(minute=0, second=0, microsecond=0)
    switch = min(end, clock_hour + timedelta(hours=1) if switch > clock_hour else clock_hour)
    intervals = []
    cursor = start
    while cursor < end:
        fine = cursor < switch
        duration = timedelta(minutes=FINE_INTERVAL_MINUTES if fine else COARSE_INTERVAL_MINUTES)
        until = min(cursor + duration, switch if fine else end)
        intervals.append((cursor, (until - cursor).total_seconds() / SECONDS_PER_HOUR))
        cursor = until
    return tuple(intervals)


def command_deadline(now):
    """Allow the next quarter transition plus one bounded replan grace period.

    Admission still checks measurement freshness separately. Freshness loss or
    another hard invalidation revokes commands before this fallback deadline.
    """
    start = now.astimezone(UTC)
    start = start.replace(
        minute=start.minute // FINE_INTERVAL_MINUTES * FINE_INTERVAL_MINUTES,
        second=0,
        microsecond=0,
    )
    return start + timedelta(minutes=FINE_INTERVAL_MINUTES + COMMAND_GRACE_MINUTES)
