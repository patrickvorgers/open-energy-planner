"""Resolve local bonus payment strategies independently of rates and counters."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import daylight

from ..planning_policy import PLANNING_HORIZON_HOURS
from ..prices.model import aware

SOLAR_EVENT_CACHE_ENTRIES = 256
BONUS_WINDOW_RESOLUTION = timedelta(minutes=1)
MONTHS_PER_YEAR = 12
OFFSET_SCAN_STEP = timedelta(hours=1)
TRANSITION_PRECISION = timedelta(microseconds=1)


@lru_cache(maxsize=SOLAR_EVENT_CACHE_ENTRIES)
def solar_window(latitude, longitude, day, timezone):
    """Cache full eligible daylight minutes. Round sunrise up and sunset down.

    Calculation and display share whole-minute boundaries. Do not pay a bonus
    before sunrise or after sunset. Missing polar events remain unsupported.
    """
    sunrise, sunset = daylight(Observer(latitude, longitude), day, ZoneInfo(timezone))
    start = sunrise.replace(second=0, microsecond=0)
    if start < sunrise:
        start += BONUS_WINDOW_RESOLUTION
    return start, sunset.replace(second=0, microsecond=0)


@dataclass(frozen=True)
class ClockWindow:
    """One half-open local clock range, anchored to its opening date."""

    start_time: str
    end_time: str

    def __post_init__(self):
        """Reject incomplete, equal or non-HH:MM clock endpoints."""
        for value in (self.start_time, self.end_time):
            if (
                not isinstance(value, str)
                or len(value) != 5
                or value[2] != ":"
                or not value[:2].isascii()
                or not value[:2].isdigit()
                or not value[3:].isascii()
                or not value[3:].isdigit()
            ):
                raise ValueError("Clock windows require HH:MM")
            time.fromisoformat(value)
        if self.start_time == self.end_time:
            raise ValueError("Invalid clock window")


@dataclass(frozen=True)
class BonusWindow:
    """Own eligibility and boundary discovery for every payment-window strategy.

    Monthly schedules contain exactly twelve January-to-December ranges. None
    disables a month. Overnight ranges continue from their opening date even
    across month/year boundaries. Price gates and counters remain separate.
    """

    kind: str = "always"
    start_time: str | None = None
    end_time: str | None = None
    monthly_schedule: tuple[ClockWindow | None, ...] = ()

    def __post_init__(self):
        """Require only fields belonging to the selected strategy."""
        if self.kind not in ("always", "clock", "daylight", "monthly_schedule"):
            raise ValueError("Unsupported bonus window")
        if self.kind == "clock":
            ClockWindow(self.start_time, self.end_time)
        elif self.start_time is not None or self.end_time is not None:
            raise ValueError("Only clock windows accept top-level clock fields")
        if (
            not isinstance(self.monthly_schedule, tuple)
            or (
                self.kind == "monthly_schedule"
                and (
                    len(self.monthly_schedule) != MONTHS_PER_YEAR
                    or any(
                        entry is not None and not isinstance(entry, ClockWindow)
                        for entry in self.monthly_schedule
                    )
                )
            )
            or (self.kind != "monthly_schedule" and self.monthly_schedule)
        ):
            raise ValueError("Monthly schedule requires twelve clock ranges or nulls")

    def clock_on(self, day):
        """Select the range by its opening date, without optimizer-specific logic."""
        if self.kind == "clock":
            return ClockWindow(self.start_time, self.end_time)
        if self.kind == "monthly_schedule":
            return self.monthly_schedule[day.month - 1]
        return None

    def zone(self, timezone, location):
        """Use HA's timezone for daylight and the contract's zone for clock ranges."""
        if self.kind == "daylight":
            if location is None:
                raise ValueError("Home Assistant location is required for daylight")
            return ZoneInfo(location.timezone)
        return ZoneInfo(timezone)

    def contains(self, instant, *, timezone, location=None):
        """Check a real instant against the selected half-open local payment range."""
        instant = aware(instant)
        local = instant.astimezone(self.zone(timezone, location))
        if self.kind == "always":
            return True
        if self.kind == "daylight":
            start, end = solar_window(
                location.latitude, location.longitude, local.date(), location.timezone
            )
            return start.astimezone(UTC) <= instant < end.astimezone(UTC)
        wall = local.time().replace(tzinfo=None)
        for opening_day in (local.date(), local.date() - timedelta(days=1)):
            clock = self.clock_on(opening_day)
            if clock is None:
                continue
            start, end = time.fromisoformat(clock.start_time), time.fromisoformat(clock.end_time)
            if opening_day == local.date():
                inside = start <= wall < end if start < end else wall >= start
                if inside:
                    return True
            elif start > end and wall < end:
                return True
        return False

    def boundaries(self, start, end, *, timezone, location=None):
        """Return exact UTC clock/solar edges, including real offset transitions.

        Both autumn folds are included. Nonexistent clock edges are omitted.
        An offset jump itself is an edge because eligibility can change there.
        Overnight endpoints preserve their opening-date ownership at month boundaries.
        """
        start, end = aware(start), aware(end)
        if end <= start or end - start > timedelta(hours=PLANNING_HORIZON_HOURS):
            raise ValueError(
                f"Bonus windows require a positive horizon of at most {PLANNING_HORIZON_HOURS} hours"
            )
        zone = self.zone(timezone, location)
        edges = set()
        day = start.astimezone(zone).date() - timedelta(days=1)
        last = end.astimezone(zone).date() + timedelta(days=1)
        while day <= last:
            if self.kind == "daylight":
                edges.update(
                    instant.astimezone(UTC)
                    for instant in solar_window(
                        location.latitude, location.longitude, day, location.timezone
                    )
                )
            elif self.kind in ("clock", "monthly_schedule"):
                clock = self.clock_on(day)
                if clock is not None:
                    closing_day = day + timedelta(days=clock.start_time > clock.end_time)
                    for edge_day, value in ((day, clock.start_time), (closing_day, clock.end_time)):
                        for fold in (0, 1):
                            local = datetime.combine(
                                edge_day, time.fromisoformat(value), zone
                            ).replace(fold=fold)
                            instant = local.astimezone(UTC)
                            if instant.astimezone(zone).replace(tzinfo=None) == local.replace(
                                tzinfo=None
                            ):
                                edges.add(instant)
            day += timedelta(days=1)
        if self.kind in ("clock", "monthly_schedule"):
            cursor = start
            while cursor < end:
                following = min(cursor + OFFSET_SCAN_STEP, end)
                offset = cursor.astimezone(zone).utcoffset()
                if following.astimezone(zone).utcoffset() != offset:
                    lower, upper = cursor, following
                    while upper - lower > TRANSITION_PRECISION:
                        middle = lower + (upper - lower) // 2
                        if middle.astimezone(zone).utcoffset() == offset:
                            lower = middle
                        else:
                            upper = middle
                    if self.contains(
                        upper - TRANSITION_PRECISION, timezone=timezone, location=location
                    ) != self.contains(upper, timezone=timezone, location=location):
                        edges.add(upper)
                cursor = following
        return tuple(sorted(edge for edge in edges if start < edge < end))
