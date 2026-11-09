"""Define Home Assistant local calendar boundaries independently of browser and model clocks."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def calendar_day(day):
    """Reject non-canonical ISO date forms before using dates as archive or pagination keys."""
    local_day = date.fromisoformat(day)
    if local_day.isoformat() != day:
        raise ValueError("Calendar day must use YYYY-MM-DD")
    return local_day


def day_window(day, timezone):
    """Return actual UTC bounds of a local calendar day, including 23/25-hour DST days."""
    local_day = calendar_day(day)
    zone = ZoneInfo(timezone)
    start = datetime.combine(local_day, time(), zone).astimezone(UTC)
    end = datetime.combine(local_day + timedelta(days=1), time(), zone).astimezone(UTC)
    if end <= start or start.astimezone(zone).date() != local_day:
        raise ValueError("Calendar day does not exist in the installation timezone")
    return start, end
