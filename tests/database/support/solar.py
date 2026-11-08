"""Independent forecast evidence for document persistence, not a forecast accuracy benchmark."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.solar.backtest import BacktestWindow, HistoricalForecastAssessment
from open_energy_planner.solar.calculation.model import SolarWeatherHour, version
from open_energy_planner.solar.forecast_weather import ForecastWeatherRun


def forecast_evidence():
    """Specify four weekly windows and finite provider values independently of native training."""
    site = SiteLocation(52, 6, "Europe/Amsterdam")
    now = datetime(2026, 8, 20, tzinfo=UTC)
    initializations = tuple(
        datetime(2026, month, day, 12, tzinfo=UTC)
        for month, day in ((7, 25), (8, 1), (8, 8), (8, 15))
    )
    windows = tuple(
        BacktestWindow(
            initialized,
            initialized + timedelta(hours=delay),
            initialized + timedelta(hours=delay + 48),
            delay,
            48,
            0.2,
            0.05,
            0.08,
            initialized - timedelta(days=7),
            now,
            "a" * 64,
            "b" * 64,
        )
        for initialized in initializations
        for delay in (0, 24)
    )
    report = HistoricalForecastAssessment("c" * 64, "d" * 64, initializations, windows, now)
    initialized = initializations[-1]
    run = ForecastWeatherRun(
        initialized_at=initialized,
        received_at=now,
        location_version=version(asdict(site)),
        hours=tuple(
            SolarWeatherHour(initialized + timedelta(hours=hour), 500, 400, 100, 20, 2, now)
            for hour in range(95)
        ),
    )
    return site, run, report
