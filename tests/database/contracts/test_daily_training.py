"""Daily model restoration and shared original forecast parameters on every supported database."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from open_energy_planner.demand.calculation.forecasting import RidgeDemandModel, predict
from open_energy_planner.demand.calculation.model import DemandConfiguration, DemandHour, EnergyTerm
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.demand.training import daily_models
from open_energy_planner.demand.wire import forecast_document
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from tests.database.support.backends import BACKENDS

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
