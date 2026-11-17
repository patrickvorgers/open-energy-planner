"""Retain whole-calendar-day spot-price rankings independently of rolling plans."""

import hashlib
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES, PLANNING_HORIZON_HOURS

from .model import PriceKind

EXTREME_QUARTERS = 12
QUARTER = timedelta(minutes=FINE_INTERVAL_MINUTES)
MAX_QUERY_DAYS = 4


class DailyPriceStatus(StrEnum):
    """Distinguish missing day coverage, forecast rankings and immutable published rankings."""

    INCOMPLETE = "incomplete"
    PROVISIONAL = "provisional"
    FINAL = "final"


class DailyPrices:
    """Archive daily quarter prices and freeze their first completely published ranking.

    The price refresh owns writes. HTTP only reads these documents. Elapsed
    forecast quarters survive rolling source replacement. Future forecasts must
    still be usable. Published quarters supersede forecasts. Complete final days
    remain unchanged through corrections, restarts and source-cache eviction.
    """

    def __init__(self, documents):
        """Borrow the app-owned price document namespace and its transaction guard."""
        self.documents = documents

    def _name(self, area, timezone, day):
        """Scope daily evidence by bidding area, currency and installation calendar."""
        identity = hashlib.sha256(f"{area}:EUR:{timezone}:{day}".encode()).hexdigest()
        return f"daily-{identity}.json"

    def read(self, area, timezone, start, end):
        """Read at most four touched local dates without fetching or changing prices."""
        zone = ZoneInfo(timezone)
        day = start.astimezone(zone).date()
        last = (end - timedelta(microseconds=1)).astimezone(zone).date()
        if end <= start or (last - day).days >= MAX_QUERY_DAYS:
            raise ValueError("Choose a price period of at most four calendar days")
        days = []
        while day <= last:
            name = self._name(area, timezone, day)
            if self.documents.exists(name):
                days.append(self.documents.read(name))
            else:
                days.append(
                    {"day": day.isoformat(), "status": DailyPriceStatus.INCOMPLETE, "quarters": []}
                )
            day += timedelta(days=1)
        return days

    def update(self, timeline, area, timezone, now):
        """Refresh derived evidence from a captured source timeline on the storage worker.

        Rank all quarters of each local day touched by the planning horizon,
        including 23/25-hour DST days. Ties use chronological order. Extremes
        need complete coverage. Highest and lowest sets never overlap.
        """
        zone = ZoneInfo(timezone)
        day = now.astimezone(zone).date()
        last = (now + timedelta(hours=PLANNING_HORIZON_HOURS)).astimezone(zone).date()
        while day <= last:
            start = datetime.combine(day, time(), zone).astimezone(UTC)
            end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
            name = self._name(area, timezone, day)
            with self.documents.transaction():
                previous = self.documents.read(name) if self.documents.exists(name) else None
                if previous and previous["status"] == DailyPriceStatus.FINAL:
                    day += timedelta(days=1)
                    continue
                retained = {row["start"]: row for row in previous["quarters"]} if previous else {}
                quarters = []
                cursor = start
                while cursor < end:
                    following = cursor + QUARTER
                    old = retained.get(cursor.isoformat())
                    selected = timeline(cursor, hours=QUARTER.total_seconds() / SECONDS_PER_HOUR)
                    entries = selected.entries
                    if selected.covered_until == following and entries:
                        rate = (
                            sum(
                                entry.value * (entry.end - entry.start).total_seconds()
                                for entry in entries
                            )
                            / QUARTER.total_seconds()
                        )
                        kind = (
                            PriceKind.PUBLISHED
                            if all(entry.kind == PriceKind.PUBLISHED for entry in entries)
                            else PriceKind.FORECAST
                        )
                        row = {
                            "start": cursor.isoformat(),
                            "end": following.isoformat(),
                            "market_price": rate,
                            "price_kind": kind,
                        }
                        quarters.append(
                            old
                            if old
                            and old["price_kind"] == PriceKind.PUBLISHED
                            and kind != PriceKind.PUBLISHED
                            else row
                        )
                    elif old and (old["price_kind"] == PriceKind.PUBLISHED or following <= now):
                        quarters.append(old)
                    cursor = following
                complete = len(quarters) * QUARTER == end - start
                document = {
                    "day": day.isoformat(),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "timezone": timezone,
                    "market_area": area,
                    "currency": "EUR",
                    "status": DailyPriceStatus.FINAL
                    if complete
                    and all(row["price_kind"] == PriceKind.PUBLISHED for row in quarters)
                    else DailyPriceStatus.PROVISIONAL
                    if complete
                    else DailyPriceStatus.INCOMPLETE,
                    "quarters": quarters,
                    "cheapest": [],
                    "most_expensive": [],
                }
                if complete:
                    ranked = sorted(quarters, key=lambda row: (row["market_price"], row["start"]))
                    document["cheapest"] = [row["start"] for row in ranked[:EXTREME_QUARTERS]]
                    document["most_expensive"] = [
                        row["start"]
                        for row in sorted(
                            ranked[EXTREME_QUARTERS:],
                            key=lambda row: (-row["market_price"], row["start"]),
                        )[:EXTREME_QUARTERS]
                    ]
                if document != previous:
                    self.documents.write(name, document)
            day += timedelta(days=1)
