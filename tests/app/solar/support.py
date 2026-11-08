"""Synthetic original-run declarations shared by solar learning and storage scenarios."""

from dataclasses import asdict
from datetime import timedelta

from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.forecast_weather import ForecastWeatherRun


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
