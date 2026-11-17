"""Provide varied complete synthetic price days through production daily-price processing."""

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from open_energy_planner.prices.daily import DailyPrices
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.storage.adapters.documents import SqlDocuments


def seed(service, engine):
    """Retain today's published prices and two forecast days with visible morning/evening peaks."""
    now = datetime.now(UTC)
    start = (
        now.astimezone(ZoneInfo("Europe/Amsterdam"))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    sources = []
    for day_index, kind in (
        (0, PriceKind.PUBLISHED),
        (1, PriceKind.FORECAST),
        (2, PriceKind.FORECAST),
    ):
        points = []
        for index in range(96):
            hour = index / 4
            value = (
                0.08
                + 0.17 * math.exp(-(((hour - 8) / 2) ** 2))
                + 0.23 * math.exp(-(((hour - 18) / 2) ** 2))
                - 0.10 * math.exp(-(((hour - 13) / 2) ** 2))
            )
            at = start + timedelta(days=day_index, minutes=index * 15)
            points.append(PricePoint(at, at + timedelta(minutes=15), value, kind, 15))
        sources.append(
            PriceSeries(
                f"synthetic-{day_index}",
                "NL",
                "EUR",
                now,
                now,
                now + timedelta(hours=6) if kind == PriceKind.FORECAST else None,
                tuple(points),
            )
        )
    service.series = tuple(sources)
    DailyPrices(SqlDocuments(engine, "prices")).update(
        service.daily_timeline(now), "NL", "Europe/Amsterdam", now
    )
