"""Shared planning horizon, forecast freshness and resolution. Providers retain their access limits."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.core.units import MINUTES_PER_HOUR

PLANNING_HORIZON_HOURS = 48
FINE_INTERVAL_MINUTES = 15
TERMINAL_VALUATION_HOURS = 24
COMMAND_GRACE_MINUTES = 5
PRICE_COVERAGE_HOURS = PLANNING_HORIZON_HOURS + TERMINAL_VALUATION_HOURS


def planning_grid(now: datetime, *, align: bool = True) -> tuple[tuple[datetime, float], ...]:
    """Cover the elapsed planning horizon in UTC quarters.

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
    return tuple(
        (start + timedelta(minutes=offset), FINE_INTERVAL_MINUTES / MINUTES_PER_HOUR)
        for offset in range(0, PLANNING_HORIZON_HOURS * MINUTES_PER_HOUR, FINE_INTERVAL_MINUTES)
    )


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
