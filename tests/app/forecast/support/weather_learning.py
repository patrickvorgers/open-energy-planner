"""Share synthetic weather-dependent consumption and forward forecast setup across suites."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import cos, pi, sin

from open_energy_planner.appliances.weather_learning import select_weather_model
from open_energy_planner.demand.calculation.forecasting import RidgeDemandModel, predict
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    DemandHour,
    EnergyTerm,
    TemperatureHour,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
CONFIGURATION = DemandConfiguration("UTC", 0, (EnergyTerm("sensor.heat", 1),))


def history(hours=60 * 24, *, constant=False):
    """Keep cold spells and hourly operating transitions in an independently specified process."""
    samples, weather = [], []
    for index in range(hours):
        instant = START + timedelta(hours=index)
        temperature = 7 + 10 * sin(2 * pi * index / (24 * 5.3)) + 2 * cos(2 * pi * index / 24)
        energy = (
            0.4
            if constant
            else 0.12
            + (1.8 if temperature < 3 else 0.25) * (1.4 if 6 <= instant.hour < 10 else 0.6)
        )
        samples.append(DemandHour(instant, energy, energy))
        weather.append(TemperatureHour(instant, temperature, instant + timedelta(hours=1)))
    return tuple(samples), tuple(weather)


def known_weather(weather, now):
    """Supply historical observations and a newly issued future forecast without backdating it."""
    return tuple(replace(item, known_at=now) if item.start >= now else item for item in weather)


def forecast_day(documents, day, *, constant=False):
    """Issue one real forecast before the next synthetic day and assess only earlier saved days."""
    samples, weather = history(constant=constant)
    now = START + timedelta(days=day)
    train = tuple(item for item in samples if item.start < now)
    available = known_weather(weather[: (day + 3) * 24], now)
    ridge = RidgeDemandModel.fit(CONFIGURATION, train, temperatures=available)
    fallback = predict(CONFIGURATION, train, now, ridge, temperatures=available)
    return select_weather_model(CONFIGURATION, train, available, now, ridge, fallback, documents)
