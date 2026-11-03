"""Offline price and weather fixtures for independent fast regression tests, never a runtime entry point."""

import asyncio
from datetime import timedelta
from math import pi, sin
from zoneinfo import ZoneInfo

from open_energy_planner.demand.calculation.model import TemperatureHour
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.service import PriceService
from open_energy_planner.solar.calculation.model import SolarWeatherHour


def price_rows(now):
    """Make published quarters plus two overlapping offline forecasts, including a 72h valuation tail."""
    start = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    series = []
    for source_name, point_count, resolution_minutes, start_offset_hours, price_kind in (
        ("published", 96, 15, 0, PriceKind.PUBLISHED),
        ("forecast", 24, 60, 24, PriceKind.FORECAST),
        ("extended-forecast", 48, 60, 24, PriceKind.FORECAST),
    ):
        rows = []
        for point_index in range(point_count):
            left = start + timedelta(
                hours=start_offset_hours, minutes=resolution_minutes * point_index
            )
            hour = left.astimezone(ZoneInfo("Europe/Amsterdam")).hour
            price_eur_per_kwh = -0.20 if hour < 5 else 0.35 if 17 <= hour < 22 else 0.10
            if source_name == "extended-forecast":
                price_eur_per_kwh += 0.01
            rows.append(
                PricePoint(
                    left,
                    left + timedelta(minutes=resolution_minutes),
                    price_eur_per_kwh,
                    price_kind,
                    resolution_minutes,
                )
            )
        series.append(
            PriceSeries(
                "interactive-test-" + source_name,
                "NL",
                "EUR",
                now,
                now,
                now + timedelta(hours=6) if price_kind == PriceKind.FORECAST else None,
                tuple(rows),
            )
        )
    return tuple(series)


class TestPrices(PriceService):
    """Refresh only offline fixtures. The production adapters never acquire a network client here."""

    async def refresh(self, now):
        """Persist one new fixture cohort per quarter, keeping normal price composition and provenance."""
        rows = price_rows(now)
        if tuple(
            (series_item.source_id, series_item.points[0].start, series_item.points[-1].end)
            for series_item in self.series
        ) == tuple(
            (row_item.source_id, row_item.points[0].start, row_item.points[-1].end)
            for row_item in rows
        ):
            return False
        async with self.persistence_lock:
            writer = asyncio.create_task(asyncio.to_thread(self.store.save, rows))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await asyncio.gather(writer, return_exceptions=True)
                raise
            self.series = rows
        await self.assess(
            now,
            tuple(row_item for row_item in rows if row_item.points[0].kind == PriceKind.FORECAST),
        )
        return True


class TestWeather:
    """Supply repeatable sample weather around the test HA location without cloud requests."""

    async def read(self, _location, now):
        """Return 32 historic days and three future days of explicitly simulated irradiance/weather."""
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=32)
        result = []
        for hour_index in range(35 * 24):
            instant = start + timedelta(hours=hour_index)
            hour = instant.astimezone(ZoneInfo("Europe/Amsterdam")).hour + 0.5
            daylight_factor = max(0, sin((hour - 7) / 12 * pi)) if 7 <= hour <= 19 else 0
            result.append(
                SolarWeatherHour(
                    instant,
                    650 * daylight_factor,
                    500 * daylight_factor,
                    150 * daylight_factor,
                    15,
                    1,
                    now,
                )
            )
        return tuple(result)


class TestTemperatures:
    """Translate the same simulated weather into the separate demand-weather port."""

    async def read(self, location, now):
        """Keep the ports independent, retaining an explicit knowledge time for each sample."""
        return tuple(
            TemperatureHour(row.start, row.celsius, now)
            for row in await TestWeather().read(location, now)
        )
