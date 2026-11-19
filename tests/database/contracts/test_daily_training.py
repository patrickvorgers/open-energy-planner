"""Daily model restoration and shared original forecast parameters on every supported database."""

import json
import logging
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select

from open_energy_planner.appliances import weather_model
from open_energy_planner.appliances.weather_model import WeatherDemandModel
from open_energy_planner.demand.calculation.forecasting import RidgeDemandModel, predict
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    DemandHour,
    EnergyTerm,
    digest,
)
from open_energy_planner.demand.storage import DemandStore, resolve_native_weights
from open_energy_planner.demand.training import daily_models
from open_energy_planner.demand.wire import forecast_document
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.document_retention import maintain_documents
from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENT_METADATA, SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from tests.database.support.backends import BACKENDS
from tests.database.support.document_retention import finish_index

NOW = datetime(2026, 7, 1, 12, tzinfo=UTC)


@pytest.fixture(params=BACKENDS, ids=[name for name, _url in BACKENDS])
def archive(request, tmp_path):
    """Initialize only disposable backend databases and dispose the shared repository after each case."""
    backend, url = request.param
    engine = archive_engine(tmp_path) if backend == "sqlite" else create_engine(url)
    SCHEMA.drop_all(engine)
    repository = SqlPlanRepository(tmp_path, engine=engine)
    try:
        yield engine
    finally:
        repository.close()
        SCHEMA.drop_all(engine)
        engine.dispose()


def measured_case():
    """Provide forty completed days with an independently specified recurring measured profile."""
    configuration = DemandConfiguration("Europe/Amsterdam", 3200, (EnergyTerm("sensor.house", 1),))
    start = NOW - timedelta(days=40)
    samples = tuple(
        DemandHour(
            start + timedelta(hours=offset),
            0.2 + offset % 24 / 100,
            0.2 + offset % 24 / 100,
        )
        for offset in range(40 * 24)
    )
    return configuration, samples


def weather_case():
    """Expose completed synthetic weather-dependent measurements and independent future weather."""
    from tests.app.forecast.support.weather_learning import (
        CONFIGURATION,
        START,
        history,
        known_weather,
    )

    samples, temperatures = history()
    now = START + timedelta(days=42)
    return (
        CONFIGURATION,
        tuple(sample for sample in samples if sample.start < now),
        known_weather(temperatures, now),
        now,
    )


def test_reopened_daily_models_reproduce_the_original_predictions_without_fitting(archive):
    """Every backend restores checked coefficients and skips an unnecessary post-restart learner."""
    configuration, samples = measured_case()
    documents = SqlDocuments(archive, "demand")
    first = daily_models(documents, configuration, samples, NOW)
    original = predict(configuration, samples, NOW, first["ridge"])
    bundle = list(documents.iter_prefix("daily-models-"))
    with patch.object(RidgeDemandModel, "fit", side_effect=AssertionError("Unexpected refit")):
        reopened = SqlDocuments(archive, "demand")
        restored = daily_models(reopened, configuration, samples, NOW + timedelta(minutes=15))
    assert restored == first
    assert predict(configuration, samples, NOW, restored["ridge"]) == original
    assert list(reopened.iter_prefix("daily-models-")) == bundle
    assert len(bundle) == 1


def test_original_forecasts_share_one_parameter_document_and_keep_exact_replay(archive):
    """Repeated issues retain original horizons while one immutable coefficient document serves them all."""
    configuration, samples = measured_case()
    store = DemandStore(SqlDocuments(archive, "demand"))
    model = daily_models(store.documents, configuration, samples, NOW)["ridge"]
    forecasts = tuple(
        predict(configuration, samples, NOW + timedelta(minutes=15 * offset), model)
        for offset in range(8)
    )
    for forecast in forecasts:
        store.archive(forecast, model)
    assert len(list(store.documents.iter_prefix("model-parameters-"))) == 1
    assert len(list(store.documents.iter_prefix("forecast-"))) == 8
    reopened = DemandStore(SqlDocuments(archive, "demand"))
    for forecast in forecasts:
        parameters = reopened.model_parameters(forecast.version)
        assert parameters == json.loads(json.dumps(asdict(model), default=str))
        restored = RidgeDemandModel(
            **{
                **parameters,
                "coefficients": tuple(parameters["coefficients"]),
                "means": tuple(parameters["means"]),
                "scales": tuple(parameters["scales"]),
                "trained_until": datetime.fromisoformat(parameters["trained_until"]),
            }
        )
        assert predict(configuration, samples, forecast.issued_at, restored) == forecast


def test_composite_forecasts_share_parameters_without_losing_original_device_steps(archive):
    """Component horizons remain immutable even though repeated fitted parameters are stored separately."""
    configuration, samples = measured_case()
    store = DemandStore(SqlDocuments(archive, "demand"))
    model = daily_models(store.documents, configuration, samples, NOW)["ridge"]
    for offset in range(6):
        forecast = predict(configuration, samples, NOW + timedelta(minutes=15 * offset), model)
        composite = replace(forecast, model="consumer-composite-v1")
        components = {
            "status": "active",
            "model_parameters": [["sensor.synthetic_device", asdict(model)]],
            "devices": [
                {"entity_id": "sensor.synthetic_device", "forecast": forecast_document(forecast)}
            ],
        }
        store.archive_composite(composite, components)
        retained = store.documents.read("consumer-forecast-" + composite.version + ".json")
        assert retained["components"]["devices"] == json.loads(
            json.dumps(components["devices"], default=str)
        )
        assert set(retained["components"]["model_parameters"]) == {"reference"}
    assert len(list(store.documents.iter_prefix("model-parameters-"))) == 1
    assert len(list(store.documents.iter_prefix("consumer-forecast-"))) == 6


@pytest.mark.parametrize("failure", [ImportError, OSError])
def test_missing_optional_native_loader_keeps_scalar_models_and_retries_restore(
    archive, failure, caplog
):
    """A failed native reload never discards baseline/Ridge or announces recovery before a usable reload."""
    configuration, samples, temperatures, now = weather_case()
    documents = SqlDocuments(archive, "demand")
    original = daily_models(
        documents, configuration, samples, now, temperatures=temperatures, weather_sensitive=True
    )
    assert set(original) == {"baseline", "ridge", "weather"}
    reopened = SqlDocuments(archive, "demand")
    pointer = list(reopened.iter_prefix("daily-training-"))
    caplog.clear()
    with (
        caplog.at_level(logging.DEBUG, logger="open_energy_planner.demand.training"),
        patch.object(weather_model, "_booster", side_effect=failure("private native failure")),
        patch.object(WeatherDemandModel, "fit", side_effect=AssertionError("Unexpected refit")),
        patch.object(RidgeDemandModel, "fit", side_effect=AssertionError("Unexpected refit")),
    ):
        for minutes in (15, 30):
            restored = daily_models(
                reopened,
                configuration,
                samples,
                now + timedelta(minutes=minutes),
                temperatures=temperatures,
                weather_sensitive=True,
            )
            assert restored == {kind: original[kind] for kind in ("baseline", "ridge")}
    messages = [
        record for record in caplog.records if record.name == "open_energy_planner.demand.training"
    ]
    assert [record.levelno for record in messages] == [logging.WARNING, logging.DEBUG]
    assert "recovered" not in caplog.text
    assert "private native failure" not in caplog.text
    assert list(reopened.iter_prefix("daily-training-")) == pointer
    caplog.clear()
    with (
        caplog.at_level(logging.INFO, logger="open_energy_planner.demand.training"),
        patch.object(WeatherDemandModel, "fit", side_effect=AssertionError("Unexpected refit")),
        patch.object(RidgeDemandModel, "fit", side_effect=AssertionError("Unexpected refit")),
    ):
        recovered = daily_models(
            reopened,
            configuration,
            samples,
            now + timedelta(minutes=45),
            temperatures=temperatures,
            weather_sensitive=True,
        )
    assert recovered == original
    assert "weather restoration training recovered" in caplog.text


def test_expired_daily_bundle_keeps_original_native_weights_and_shared_parameter_replay(archive):
    """Superseded reports can expire while real original native weights remain independently readable."""
    configuration, samples, temperatures, now = weather_case()
    documents = SqlDocuments(archive, "demand")
    first = daily_models(
        documents, configuration, samples, now, temperatures=temperatures, weather_sensitive=True
    )
    pointer = next(documents.iter_prefix("daily-training-"))
    first_name = "daily-models-" + pointer["reference"] + ".json"
    body = documents.read(first_name)
    original_parameters = body["models"]["weather"]
    assert "model_json" not in original_parameters
    weights_name = "ml-weights-" + original_parameters["weights_reference"] + ".json"
    assert documents.read(weights_name) == {"model_json": first["weather"].model_json}
    store = DemandStore(documents)
    shared = store.share_parameters([["sensor.heat", asdict(first["weather"])]])
    retained = documents.read("model-parameters-" + shared["reference"] + ".json")
    assert "model_json" not in retained[0][1]
    assert retained[0][1]["weights_reference"] == original_parameters["weights_reference"]
    later = now + timedelta(days=1)
    from tests.app.forecast.support.weather_learning import history, known_weather

    all_samples, all_temperatures = history()
    daily_models(
        documents,
        configuration,
        tuple(sample for sample in all_samples if sample.start < later),
        later,
        temperatures=known_weather(all_temperatures, later),
        weather_sensitive=True,
    )
    assert next(documents.iter_prefix("daily-training-"))["reference"] != pointer["reference"]
    with archive.begin() as connection:
        connection.execute(
            DOCUMENT_METADATA.update()
            .where(
                DOCUMENT_METADATA.c.namespace == "demand", DOCUMENT_METADATA.c.name == first_name
            )
            .values(created_at=(later - timedelta(days=31)).isoformat(timespec="microseconds"))
        )
    maintenance = SqlDocuments(archive, "savings")
    finish_index(maintenance, later)
    for _attempt in range(4):
        maintain_documents(maintenance, later)
    assert not documents.exists(first_name)
    assert documents.read(weights_name) == {"model_json": first["weather"].model_json}
    restored = WeatherDemandModel.restore(
        resolve_native_weights(SqlDocuments(archive, "demand"), original_parameters),
        configuration.version,
    )
    assert restored.predict(now, temperature=2) == first["weather"].predict(now, temperature=2)
    assert store._resolve_parameters(shared)[0][1]["model_json"] == first["weather"].model_json


@pytest.mark.parametrize("kind", ["forecast", "composite", "partial"])
def test_identical_legacy_inline_forecasts_rearchive_without_rewriting_the_original(archive, kind):
    """Inline and shared parameter representations compare by value while all other fields stay immutable."""
    configuration, samples, temperatures, now = weather_case()
    model = WeatherDemandModel.fit(configuration, samples, temperatures=temperatures, known_at=now)
    forecast = predict(configuration, samples, now, model, temperatures=temperatures)
    composite = replace(forecast, model="consumer-composite-v1")
    parameters = json.loads(json.dumps(asdict(model), default=str))
    components = {
        "status": "active",
        "model_parameters": [["sensor.heat", parameters]],
        "devices": [{"entity_id": "sensor.heat", "forecast": forecast_document(forecast)}],
    }
    store = DemandStore(SqlDocuments(archive, "demand"))
    if kind == "forecast":
        name = "forecast-" + forecast.version + ".json"
        original = {"forecast": forecast_document(forecast), "model_parameters": parameters}

        def rearchive():
            """Exercise the existing ordinary-forecast write entry point."""
            store.archive(forecast, model)

    elif kind == "composite":
        name = "consumer-forecast-" + composite.version + ".json"
        original = {"forecast": forecast_document(composite), "components": components}

        def rearchive():
            """Exercise the composite write entry point against its original inline parameters."""
            store.archive_composite(composite, components)

    else:
        name = "quality-components-" + forecast.version + ".json"
        original = {"selected": False, "components": components}

        def rearchive():
            """Exercise the incomplete-challenger binding without a composite archive."""
            store.archive_components(forecast, None, components)

    store.documents.write(name, original)
    before = store.documents.read(name)
    rearchive()
    assert store.documents.read(name) == before
    changed = deepcopy(before)
    if kind == "forecast":
        changed["model_parameters"]["recent_kwh"] += 1
    else:
        changed["components"]["model_parameters"][0][1]["recent_kwh"] += 1
    store.documents.write(name, changed)
    with pytest.raises(ValueError):
        rearchive()
    assert store.documents.read(name) == changed
    changed = deepcopy(before)
    if kind == "partial":
        changed["selected"] = True
    else:
        changed["forecast"]["quality"] = "changed"
    store.documents.write(name, changed)
    with pytest.raises(ValueError):
        rearchive()
    assert store.documents.read(name) == changed


def test_native_weight_corruption_fails_closed_before_optional_native_loading(archive):
    """A damaged shared native body is an archive fault even when the native loader is unavailable."""
    configuration, samples, temperatures, now = weather_case()
    documents = SqlDocuments(archive, "demand")
    daily_models(
        documents, configuration, samples, now, temperatures=temperatures, weather_sensitive=True
    )
    weights = next(documents.iter_prefix("ml-weights-"))
    documents.write("ml-weights-" + digest(weights) + ".json", {"model_json": "{}"})
    with (
        patch.object(
            weather_model, "_booster", side_effect=ImportError("Native loader unavailable")
        ),
        pytest.raises(ValueError, match="checksum"),
    ):
        daily_models(
            SqlDocuments(archive, "demand"),
            configuration,
            samples,
            now + timedelta(minutes=15),
            temperatures=temperatures,
            weather_sensitive=True,
        )


def test_existing_inline_daily_native_models_restore_without_rewriting_the_bundle(archive):
    """The original inline native JSON remains readable without fitting or rewriting its saved body."""
    configuration, samples, temperatures, now = weather_case()
    documents = SqlDocuments(archive, "demand")
    original = daily_models(
        documents, configuration, samples, now, temperatures=temperatures, weather_sensitive=True
    )
    pointer = next(documents.iter_prefix("daily-training-"))
    body = resolve_native_weights(
        documents, documents.read("daily-models-" + pointer["reference"] + ".json")
    )
    reference = digest(body)
    name = "daily-models-" + reference + ".json"
    documents.write(name, body)
    documents.write(
        "daily-training-" + pointer["binding"] + ".json", {**pointer, "reference": reference}
    )
    with patch.object(WeatherDemandModel, "fit", side_effect=AssertionError("Unexpected refit")):
        restored = daily_models(
            SqlDocuments(archive, "demand"),
            configuration,
            samples,
            now + timedelta(minutes=15),
            temperatures=temperatures,
            weather_sensitive=True,
        )
    assert restored == original
    assert documents.read(name) == body


def test_legacy_inline_native_forecast_stays_exact_and_nonexpiring(archive):
    """A real original native model stays recoverable when its legacy forecast has no other owner."""
    configuration, samples, temperatures, now = weather_case()
    model = WeatherDemandModel.fit(configuration, samples, temperatures=temperatures, known_at=now)
    forecast = predict(configuration, samples, now, model, temperatures=temperatures)
    documents = SqlDocuments(archive, "demand")
    name = "forecast-" + forecast.version + ".json"
    original = {
        "forecast": forecast_document(forecast),
        "model_parameters": json.loads(json.dumps(asdict(model), default=str)),
    }
    original = json.loads(json.dumps(original))
    documents.write(name, original)
    with archive.begin() as connection:
        key = (DOCUMENT_METADATA.c.namespace == "demand") & (DOCUMENT_METADATA.c.name == name)
        assert connection.scalar(select(DOCUMENT_METADATA.c.kind).where(key)) is None
        connection.execute(
            DOCUMENT_METADATA.update()
            .where(key)
            .values(created_at=(now - timedelta(days=31)).isoformat(timespec="microseconds"))
        )
    maintenance = SqlDocuments(archive, "savings")
    finish_index(maintenance, now)
    for _attempt in range(4):
        maintain_documents(maintenance, now)
    retained = DemandStore(SqlDocuments(archive, "demand")).model_parameters(forecast.version)
    assert documents.read(name) == original
    assert retained["model_json"] == model.model_json
    restored = WeatherDemandModel.restore(retained, configuration.version)
    assert restored.predict(now, temperature=2) == model.predict(now, temperature=2)
