"""Exercise nonlinear appliance learning against genuinely later synthetic measurements."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from math import cos, pi, sin
from statistics import mean

import pytest

from open_energy_planner.appliances.weather_learning import select_weather_model
from open_energy_planner.appliances.weather_model import WeatherDemandModel
from open_energy_planner.demand.calculation.forecasting import (
    RidgeDemandModel,
    WeekdayProfile,
    predict,
)
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    DemandHour,
    EnergyTerm,
    TemperatureHour,
)
from open_energy_planner.demand.storage import DemandStore
from tests.database.support.archive import make_store

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


def test_nonlinear_model_generalizes_and_replays_portable_json():
    """A temperature threshold and clock interaction generalize to a later unseen week."""
    samples, weather = history()
    cutoff = START + timedelta(days=42)
    train = tuple(item for item in samples if item.start < cutoff)
    training_weather = tuple(item for item in weather if item.known_at <= cutoff)
    learned = WeatherDemandModel.fit(CONFIGURATION, train, temperatures=training_weather)
    ridge = RidgeDemandModel.fit(CONFIGURATION, train, temperatures=training_weather)
    errors = {"nonlinear": [], "ridge": []}
    for sample, temperature in zip(
        samples[42 * 24 : 49 * 24], weather[42 * 24 : 49 * 24], strict=True
    ):
        errors["nonlinear"].append(
            abs(
                learned.predict(sample.start, temperature=temperature.celsius) - sample.residual_kwh
            )
        )
        errors["ridge"].append(
            abs(ridge.predict(sample.start, temperature=temperature.celsius) - sample.residual_kwh)
        )
    assert mean(errors["nonlinear"]) < mean(errors["ridge"]) * 0.65
    stored = json.loads(json.dumps(asdict(learned), default=str, allow_nan=False))
    replay = WeatherDemandModel.restore(stored, CONFIGURATION.version)
    assert replay.predict(cutoff, temperature=2) == pytest.approx(
        learned.predict(cutoff, temperature=2)
    )
    assert replay.training_version == learned.training_version
    assert learned.training_hours == 41 * 24
    assert learned.source_hours == 42 * 24
    assert learned.excluded_periods == (
        (START.isoformat(), (START + timedelta(days=1)).isoformat(), "previous_day_incomplete"),
    )
    with pytest.raises(ValueError):
        WeatherDemandModel.restore({**stored, "feature_names": ["unknown"]}, CONFIGURATION.version)
    with pytest.raises(ValueError):
        WeatherDemandModel.restore({**stored, "model_json": "{}"}, CONFIGURATION.version)
    with pytest.raises(ValueError):
        WeatherDemandModel.restore(stored, "another-device")
    with pytest.raises(ValueError):
        WeatherDemandModel.restore({**stored, "training_hours": 42 * 24}, CONFIGURATION.version)


def test_training_rejects_future_or_missing_weather_and_prediction_extrapolation():
    """Neither unknown weather nor unobserved temperatures become fabricated model inputs."""
    samples, weather = history(35 * 24)
    cutoff = START + timedelta(days=35)
    with pytest.raises(ValueError):
        WeatherDemandModel.fit(CONFIGURATION, samples, temperatures=())
    with pytest.raises(ValueError):
        WeatherDemandModel.fit(
            CONFIGURATION,
            samples,
            temperatures=tuple(
                replace(item, known_at=cutoff + timedelta(hours=1)) for item in weather
            ),
        )
    model = WeatherDemandModel.fit(CONFIGURATION, samples, temperatures=weather)
    with pytest.raises(ValueError):
        model.predict(cutoff - timedelta(hours=1), temperature=2)
    with pytest.raises(ValueError):
        model.predict(cutoff, temperature=-40)
    with pytest.raises(ValueError):
        model.predict(cutoff)


def test_training_explains_missing_temperature_without_trimming_measured_peaks():
    """Only the absent feature hour and initial preceding-day warmup are omitted from 35 measured days."""
    samples, weather = history(35 * 24)
    missing = START + timedelta(days=30, hours=12)
    model = WeatherDemandModel.fit(
        CONFIGURATION,
        samples,
        temperatures=tuple(item for item in weather if item.start != missing),
    )
    assert model.source_hours == 840
    assert model.training_hours == 815
    assert model.excluded_periods == (
        (START.isoformat(), (START + timedelta(days=1)).isoformat(), "previous_day_incomplete"),
        (
            missing.isoformat(),
            (missing + timedelta(hours=1)).isoformat(),
            "temperature_unavailable",
        ),
    )


@pytest.mark.parametrize("constant", [False, True])
def test_only_saved_forward_predictions_can_promote_candidate(tmp_path, constant):
    """Three unseen disjoint days promote nonlinear improvement, while equal constant forecasts retain Ridge."""
    documents = make_store(tmp_path, DemandStore).documents
    samples, weather = history(constant=constant)
    evidence = None
    for day in range(42, 46):
        now = START + timedelta(days=day)
        train = tuple(item for item in samples if item.start < now)
        available = known_weather(weather[: (day + 3) * 24], now)
        ridge = RidgeDemandModel.fit(CONFIGURATION, train, temperatures=available)
        baseline = predict(CONFIGURATION, train, now, ridge, temperatures=available)
        model, forecast, evidence = select_weather_model(
            CONFIGURATION, train, available, now, ridge, baseline, documents
        )
        if day < 45 or constant:
            assert model.model_id == "ridge-v2"
        else:
            assert model.model_id == "xgboost-weather-v1"
        assert forecast.model == model.model_id
        assert evidence["completed_windows"] == min(day - 42, 3)
    assert evidence["status"] == (
        "no_consistent_accuracy_benefit" if constant else "accuracy_validated"
    )
    assert len({window["start"] for window in evidence["windows"]}) == 3
    assert all(window["issued_at"] <= window["start"] for window in evidence["windows"])


def test_missing_realization_excludes_window_without_zero_or_promotion(tmp_path):
    """A gap in the later measured day withholds comparison and retains the simpler forecast."""
    documents = make_store(tmp_path, DemandStore).documents
    samples, weather = history()
    for day in (42, 44):
        now = START + timedelta(days=day)
        train = tuple(
            item
            for item in samples
            if item.start < now and item.start != START + timedelta(days=42, hours=12)
        )
        available = known_weather(weather[: (day + 3) * 24], now)
        profile = WeekdayProfile.fit(CONFIGURATION, train)
        fallback = predict(CONFIGURATION, train, now, profile, temperatures=available)
        model, _, evidence = select_weather_model(
            CONFIGURATION, train, available, now, profile, fallback, documents
        )
    assert model.model_id == "weekday-hour-v2"
    assert evidence["completed_windows"] == 0
    assert evidence["excluded_window"]["reason"] == "incomplete_measurements"


def test_corrupt_forward_evidence_cannot_promote_or_be_silently_replaced(tmp_path):
    """A saved forward prediction with a changed quantity loses admission without rewriting the original."""
    documents = make_store(tmp_path, DemandStore).documents
    forecast_day(documents, 42)
    window = next(documents.iter_prefix("consumer-weather-window-"))
    window["predictions"]["candidate"][0] = 999
    from open_energy_planner.demand.calculation.model import digest

    # Keep the original pointer and deliberately corrupt only its referenced row.
    pointer = next(documents.iter_prefix("consumer-weather-selection-"))
    path = "consumer-weather-window-" + pointer["pending"] + ".json"
    documents.write(path, window)
    model, _, evidence = forecast_day(documents, 43)
    assert model.model_id == "ridge-v2"
    assert evidence["status"] == "comparison_evidence_unreadable"
    assert documents.read(path) == window
    assert digest(window) != pointer["pending"]


def test_failed_forward_pointer_write_rolls_back_model_and_window(tmp_path, monkeypatch):
    """An archive write failure leaves no unreferenced model or falsely saved comparison."""
    documents = make_store(tmp_path, DemandStore).documents
    original = documents.write

    def fail_pointer(name, value):
        """Fail at the final pointer after the immutable evidence writes."""
        if name.startswith("consumer-weather-selection-"):
            raise OSError("Synthetic pointer failure")
        original(name, value)

    monkeypatch.setattr(documents, "write", fail_pointer)
    with pytest.raises(OSError):
        forecast_day(documents, 42)
    assert list(documents.iter_prefix("consumer-weather-")) == []


def test_later_bad_day_revokes_admission_and_keeps_original_evidence(tmp_path):
    """A later changed usage pattern demotes the candidate without replacing earlier predictions."""
    documents = make_store(tmp_path, DemandStore).documents
    for day in range(42, 46):
        model, _, _ = forecast_day(documents, day)
    assert model.model_id == "xgboost-weather-v1"
    original_windows = list(documents.iter_prefix("consumer-weather-window-"))
    samples, weather = history()
    now = START + timedelta(days=46)
    train = tuple(
        replace(item, total_kwh=4, residual_kwh=4)
        if START + timedelta(days=45) <= item.start < now
        else item
        for item in samples
        if item.start < now
    )
    available = known_weather(weather[: 49 * 24], now)
    ridge = RidgeDemandModel.fit(CONFIGURATION, train, temperatures=available)
    fallback = predict(CONFIGURATION, train, now, ridge, temperatures=available)
    model, _, evidence = select_weather_model(
        CONFIGURATION, train, available, now, ridge, fallback, documents
    )
    assert model.model_id == "ridge-v2"
    assert evidence["status"] == "no_consistent_accuracy_benefit"
    assert any(not window["improved"] for window in evidence["windows"])
    assert all(
        window in list(documents.iter_prefix("consumer-weather-window-"))
        for window in original_windows
    )


def test_nonlinear_composition_cannot_borrow_ridge_financial_preference(tmp_path):
    """A validated weather component still needs financial evidence for its changed composition family."""
    from open_energy_planner.appliances.forecasting import parse_selection
    from open_energy_planner.demand.runtime import DemandRuntime
    from tests.app.demand.test_consumers import catalogue
    from tests.app.demand.test_demand import channel, config

    store = make_store(tmp_path, DemandStore)
    store.save(replace(config(), timezone="UTC"))
    runtime = DemandRuntime(store)
    runtime.consumers.sync(catalogue())
    runtime.consumers.save(
        parse_selection([{"entity_id": "sensor.heat", "weather_sensitive": True}]),
        runtime.consumers.version,
    )
    for day in range(42, 46):
        forecast_day(store.documents, day)
    samples, weather = history()
    now = START + timedelta(days=45)
    measured = [item.residual_kwh for item in samples[: 45 * 24]]
    runtime.histories["sensor.house"] = channel("sensor.house", [value + 0.3 for value in measured])
    runtime.consumers.histories["sensor.heat"] = channel("sensor.heat", measured)
    runtime.weather(known_weather(weather[: 48 * 24], now))
    runtime.candidate_features = "consumers:" + runtime.consumers.version
    runtime.select_learned(True)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.forecast.model == "weekday-hour-v2"
    assert runtime.candidate.model == "consumer-composite-v1"
    assert runtime.model_status == "shadow_waiting_for_decision_evidence"
    assert ":weather:" in runtime.candidate_features
    assert runtime.learned_preferred is False
    assert runtime.install(runtime.build(runtime.job(now)), now)
    assert runtime.forecast.model == "weekday-hour-v2"
