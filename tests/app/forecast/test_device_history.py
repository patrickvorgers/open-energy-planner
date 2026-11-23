"""Use only earlier training and original temperature forecasts in device replays."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.demand.calculation.model import DemandConfiguration, DemandHour, EnergyTerm
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.demand.training import daily_models
from open_energy_planner.forecast_selection.device_history import replay_days, replay_scores
from open_energy_planner.weather.model import TemperatureHour, WeatherHour
from open_energy_planner.weather.run import ForecastWeatherRun
from tests.database.support.archive import make_store


def test_replay_preserves_original_weather_and_cannot_fit_held_out_energy(tmp_path):
    """Temperature models win independent replay. Corrected held-out energy changes scores only."""
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    initialized = now - timedelta(days=4)
    origin = initialized + timedelta(days=1)
    run = ForecastWeatherRun(
        initialized_at=initialized,
        received_at=now,
        location_version="a" * 64,
        hours=tuple(
            WeatherHour(initialized + timedelta(hours=index), 0, 0, 0, 6, 0, now)
            for index in range(95)
        ),
    )
    ((day, captured_origin, evidence),) = replay_days(now, "UTC", (run,))
    assert captured_origin == origin
    assert evidence.version == run.version
    target = datetime.combine(day, datetime.min.time(), UTC)
    samples, historical_weather = [], []
    for index in range(90 * 24):
        hour = now - timedelta(hours=90 * 24 - index)
        temperature = 6 + 12 * ((index // 24) % 4) / 3
        energy = 1.4 if temperature == 6 else 0.1
        if target <= hour < target + timedelta(days=1):
            # Realized weather in this synthetic test would lead to the wrong forecast.
            temperature, energy = 18, 1.4
        samples.append(DemandHour(hour, energy, energy))
        historical_weather.append(TemperatureHour(hour, temperature, now))
    configuration = DemandConfiguration("UTC", 0, (EnergyTerm("sensor.device", 1),))
    models = daily_models(
        None, configuration, samples, now, temperatures=historical_weather, weather_sensitive=True
    )
    archive = make_store(tmp_path, DemandStore).documents
    (window,) = replay_scores(
        archive,
        configuration,
        tuple(samples),
        historical_weather,
        ((day, origin, run),),
        models,
        (),
    )
    assert window["source"] == "historical_replay"
    assert window["hours"] == 24
    assert set(window["mae_kwh"]) == {"baseline", "ridge", "weather"}
    assert window["mae_kwh"]["weather"] < window["mae_kwh"]["baseline"]
    corrected = tuple(
        DemandHour(sample.start, 2.8, 2.8)
        if target <= sample.start < target + timedelta(days=1)
        else sample
        for sample in samples
    )
    (updated,) = replay_scores(
        archive, configuration, corrected, historical_weather, ((day, origin, run),), models, ()
    )
    assert updated["forecast_version"] == window["forecast_version"]
    assert updated["actual_version"] != window["actual_version"]
    assert not replay_scores(
        archive,
        configuration,
        corrected,
        historical_weather,
        ((day, origin, run),),
        models,
        (day.isoformat(),),
    )
