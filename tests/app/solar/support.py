"""Shared synthetic solar inputs and offline weather adapters without app-runtime ownership."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.solar.calculation.model import (
    SolarConfiguration,
    SolarInverter,
    SolarSource,
    SolarWeatherHour,
    version,
)
from open_energy_planner.solar.forecast_weather import ForecastWeatherRun

START = datetime(2026, 6, 20, 0, tzinfo=UTC)


SITE = SiteLocation(52, 6, "Europe/Amsterdam")


def plant(*, count=1, shared=False, limit=5000, tilt=0, orientation="south"):
    """Declare disjoint 1 kWp sources with explicit standalone or shared inverter ratings."""
    return SolarConfiguration(
        tuple(
            SolarSource(
                f"pv{index_item}",
                f"sensor.pv{index_item}",
                "shared" if shared else f"i{index_item}",
                1000,
                orientation,
                tilt=tilt,
            )
            for index_item in range(count)
        ),
        (SolarInverter("shared", limit),)
        if shared
        else tuple(SolarInverter(f"i{index_item}", limit) for index_item in range(count)),
    )


def weather(now=START, *, days=3, ghi=600):
    """Provide complete independently fixed hourly radiation and temperature, never external weather."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return tuple(
        SolarWeatherHour(start + timedelta(hours=index_item), ghi, 500, 100, 20, 1, now)
        for index_item in range(days * 24)
    )


@asynccontextmanager
async def fixture_weather():
    """Supply complete normalized solar weather locally, without provider URLs or production HA access."""

    class Source:
        """Implement the solar-weather port with deterministic physical inputs."""

        async def read(self, location, now):
            """Return weather at the actual requested site/time, keeping knowledge timestamps consistent."""
            assert location == SITE
            return weather(now)

    yield Source()


def frozen_runs(conditions, location, received_at):
    """Declare complete synthetic daily forecast snapshots without changing their known numeric inputs."""
    by_hour = {row.start: row for row in conditions}
    initialized = min(by_hour).replace(hour=12, minute=0, second=0, microsecond=0)
    if initialized < min(by_hour):
        initialized += timedelta(days=1)
    runs = []
    while initialized <= received_at and initialized + timedelta(hours=94) <= max(by_hour):
        hours = tuple(by_hour.get(initialized + timedelta(hours=index)) for index in range(95))
        if all(hour is not None for hour in hours):
            runs.append(
                ForecastWeatherRun(
                    initialized_at=initialized,
                    received_at=received_at,
                    location_version=version(asdict(location)),
                    hours=hours,
                )
            )
        initialized += timedelta(days=1)
    return tuple(runs)
