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
from open_energy_planner.demand.storage import DemandStore
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
    bundle = list(documents.iter_prefix("active-training-"))
    with patch.object(RidgeDemandModel, "fit", side_effect=AssertionError("Unexpected refit")):
        reopened = SqlDocuments(archive, "demand")
        restored = daily_models(reopened, configuration, samples, NOW + timedelta(minutes=15))
    assert restored == first
    assert predict(configuration, samples, NOW, restored["ridge"]) == original
    assert list(reopened.iter_prefix("active-training-")) == bundle
    assert len(bundle) == 1


def test_original_forecasts_keep_exact_predictions_without_parameter_documents(archive):
    """Repeated issues retain their own horizons and never refer to executable coefficients."""
    configuration, samples = measured_case()
    store = DemandStore(SqlDocuments(archive, "demand"))
    model = daily_models(store.documents, configuration, samples, NOW)["ridge"]
    forecasts = tuple(
        predict(configuration, samples, NOW + timedelta(minutes=15 * offset), model)
        for offset in range(8)
    )
    for forecast in forecasts:
        store.archive(forecast)
    assert not list(store.documents.iter_prefix("model-parameters-"))
    assert len(list(store.documents.iter_prefix("forecast-"))) == 8
    reopened = SqlDocuments(archive, "demand")
    for forecast in forecasts:
        original = reopened.read("forecast-" + forecast.version + ".json")
        assert original == {
            "forecast": json.loads(json.dumps(forecast_document(forecast), default=str))
        }


def test_composite_forecasts_keep_original_device_steps_without_parameters(archive):
    """Component horizons stay independently readable without fitted parameters."""
    configuration, samples = measured_case()
    store = DemandStore(SqlDocuments(archive, "demand"))
    model = daily_models(store.documents, configuration, samples, NOW)["ridge"]
    for offset in range(6):
        forecast = predict(configuration, samples, NOW + timedelta(minutes=15 * offset), model)
        composite = replace(forecast, model="consumer-composite-v1")
        components = {
            "status": "active",
            "devices": [
                {"entity_id": "sensor.synthetic_device", "forecast": forecast_document(forecast)}
            ],
        }
        store.archive_composite(composite, components)
        retained = store.documents.read("consumer-forecast-" + composite.version + ".json")
        assert retained["components"]["devices"] == json.loads(
            json.dumps(components["devices"], default=str)
        )
        assert "model_parameters" not in retained["components"]
    assert not list(store.documents.iter_prefix("model-parameters-"))
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
    pointer = list(reopened.iter_prefix("active-training-"))
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
    assert list(reopened.iter_prefix("active-training-")) == pointer
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


def test_replacing_daily_fit_keeps_original_native_forecast_but_discards_its_weights(archive):
    """A later validated fit overwrites the device row while the original issue remains exact."""
    from tests.app.forecast.support.weather_learning import history, known_weather

    configuration, samples, temperatures, now = weather_case()
    documents = SqlDocuments(archive, "demand")
    store = DemandStore(documents)
    fitted = daily_models(
        documents, configuration, samples, now, temperatures=temperatures, weather_sensitive=True
    )
    forecast = predict(configuration, samples, now, fitted["weather"], temperatures=temperatures)
    store.archive(forecast)
    path = "forecast-" + forecast.version + ".json"
    original = documents.read(path)
    later = now + timedelta(days=1)
    all_samples, all_temperatures = history()
    active = daily_models(
        documents,
        configuration,
        tuple(sample for sample in all_samples if sample.start < later),
        later,
        temperatures=known_weather(all_temperatures, later),
        weather_sensitive=True,
    )
    assert active["weather"].model_digest != fitted["weather"].model_digest
    reopened = SqlDocuments(archive, "demand")
    retained = list(reopened.iter_prefix("active-training-"))
    assert len(retained) == 1
    assert retained[0]["models"]["weather"]["model_digest"] == active["weather"].model_digest
    assert not list(reopened.iter_prefix("ml-weights-"))
    assert reopened.read(path) == original
    assert set(original) == {"forecast"}
    assert original["forecast"]["weather_version"] == forecast.weather_version


@pytest.mark.parametrize("kind", ["forecast", "composite", "partial"])
def test_identical_legacy_inline_forecasts_rearchive_without_rewriting_the_original(archive, kind):
    """Older forecast attachments are untouched and changed original predictions are refused."""
    configuration, samples, temperatures, now = weather_case()
    model = WeatherDemandModel.fit(configuration, samples, temperatures=temperatures, known_at=now)
    forecast = predict(configuration, samples, now, model, temperatures=temperatures)
    composite = replace(forecast, model="consumer-composite-v1")
    parameters = {"reference": "a" * 64}
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
            store.archive(forecast)

    elif kind == "composite":
        name = "consumer-forecast-" + composite.version + ".json"
        original = {"forecast": forecast_document(composite), "components": components}

        def rearchive():
            """Exercise the composite write entry point against its original inline parameters."""
            store.archive_composite(
                composite,
                {key: value for key, value in components.items() if key != "model_parameters"},
            )

    else:
        name = "quality-components-" + forecast.version + ".json"
        original = {"selected": False, "components": components}

        def rearchive():
            """Exercise the incomplete-challenger binding without a composite archive."""
            store.archive_components(
                forecast,
                None,
                {key: value for key, value in components.items() if key != "model_parameters"},
            )

    store.documents.write(name, original)
    before = store.documents.read(name)
    rearchive()
    assert store.documents.read(name) == before
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
    name = "active-training-" + digest("household") + ".json"
    body = documents.read(name)
    body["models"]["weather"]["model_json"] = "{}"
    documents.write(name, body)
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
    retained = documents.read(name)["model_parameters"]
    assert documents.read(name) == original
    assert retained["model_json"] == model.model_json
    restored = WeatherDemandModel.restore(retained, configuration.version)
    assert restored.predict(now, temperature=2) == model.predict(now, temperature=2)
