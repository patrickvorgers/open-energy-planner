"""Daily models and reference-safe retention remain usable after every archive migration route."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from open_energy_planner.appliances.weather_model import WeatherDemandModel
from open_energy_planner.demand.calculation.forecasting import (
    RidgeDemandModel,
    WeekdayProfile,
    predict,
)
from open_energy_planner.demand.calculation.model import DemandConfiguration, EnergyTerm
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.demand.training import daily_models
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.document_retention import maintain_documents
from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive
from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENT_LINKS, DOCUMENT_METADATA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.demand.daily_training_support import NOW, samples_before, temperatures_for
from tests.database.migrations.test_archive_migration import route
from tests.database.support.document_retention import archived, finish_index

__all__ = ["route"]


def disconnected(*args, **kwargs):
    """Fail if reconstruction accesses the disposed source or invokes a statistical learner."""
    pytest.fail("Migrated daily models must restore without source access or fitting")


def test_solar_daily_model_restores_without_source_or_training(route, monkeypatch):
    """Every backend route preserves solar daily pointers, original reports and checked native weights."""
    from open_energy_planner.solar.storage import SolarStore
    from open_energy_planner.solar.training import DailySolarTraining
    from tests.app.solar.boosting_support import runtime_case
    from tests.app.solar.support import SITE

    (source, target), root = route
    repository = SqlPlanRepository(root / "source", engine=source)
    store = SolarStore(SqlDocuments(source, "solar"))
    runtime, now = runtime_case(root, store=store)
    assert runtime.install(runtime.build(runtime.job(now)), now)
    original = runtime.learning["pv0"]
    assert original.model is not None
    definition = store.current
    migrate_archive(source, target, overwrite_target=True)
    repository.close()
    source.dispose()
    monkeypatch.setattr(source, "connect", disconnected)
    monkeypatch.setattr(source, "begin", disconnected)
    monkeypatch.setattr("open_energy_planner.solar.runtime.fit_boosted_model", disconnected)
    restored = DailySolarTraining(SolarStore(SqlDocuments(target, "solar")))
    cached, _token = restored.prepare(
        definition.sources[0],
        definition.inverters[0],
        SITE,
        runtime.histories["sensor.pv0"],
        runtime.weather,
        runtime.forecast_runs,
        None,
        now,
    )
    assert cached == original


def index_rows(engine):
    """Snapshot portable metadata and incoming-reference rows in deterministic primary-key order."""
    with engine.connect() as connection:
        return [
            [
                dict(row)
                for row in connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table in (DOCUMENT_METADATA, DOCUMENT_LINKS)
        ]


def test_daily_models_and_shared_parameters_restore_without_source_or_fitting(route, monkeypatch):
    """Every backend route restores actual baseline, Ridge and weather fits and shared forecast parameters."""
    (source, target), root = route
    repository = SqlPlanRepository(root / "source", engine=source)
    store = DemandStore(SqlDocuments(source, "demand"))
    configuration = DemandConfiguration("Europe/Amsterdam", 3000, (EnergyTerm("sensor.house", 1),))
    store.save(configuration)
    samples = samples_before(NOW)
    weather = temperatures_for(samples, NOW)
    fitted = daily_models(
        store.documents, configuration, samples, NOW, temperatures=weather, weather_sensitive=True
    )
    assert set(fitted) == {"baseline", "ridge", "weather"}
    forecasts = {
        kind: predict(configuration, samples, NOW, fitted[kind], temperatures=weather)
        for kind in ("baseline", "ridge")
    }
    for kind, forecast in forecasts.items():
        store.archive(forecast, fitted[kind])
    parameters = {
        kind: store.model_parameters(forecast.version) for kind, forecast in forecasts.items()
    }
    migrate_archive(source, target, overwrite_target=True)
    repository.close()
    source.dispose()
    with monkeypatch.context() as patch:
        patch.setattr(source, "connect", disconnected)
        patch.setattr(source, "begin", disconnected)
        for learner in (WeekdayProfile, RidgeDemandModel, WeatherDemandModel):
            patch.setattr(learner, "fit", disconnected)
        restored_store = DemandStore(SqlDocuments(target, "demand"))
        assert restored_store.current == configuration
        restored = daily_models(
            restored_store.documents,
            configuration,
            samples,
            NOW + timedelta(minutes=15),
            temperatures=weather,
            weather_sensitive=True,
        )
        assert restored == fitted
        for kind, forecast in forecasts.items():
            assert restored_store.model_parameters(forecast.version) == parameters[kind]
            assert (
                predict(configuration, samples, NOW, restored[kind], temperatures=weather)
                == forecast
            )
        assert restored["weather"].predict(NOW, temperature=14) == pytest.approx(
            fitted["weather"].predict(NOW, temperature=14)
        )


def test_migrated_retention_index_preserves_live_roots_and_expires_only_orphans(route, monkeypatch):
    """Transferred metadata and cursor safely resume expiry while a frozen quality root protects its forecast."""
    (source, target), root = route
    repository = SqlPlanRepository(root / "source", engine=source)
    demand = SqlDocuments(source, "demand")
    maintenance = SqlDocuments(source, "savings")
    protected, orphan = "a" * 64, "b" * 64
    protected_value = {"version": protected, "original_steps": [0.1, 0.2]}
    protected_name = archived(demand, "forecast", protected, protected_value)
    orphan_name = archived(demand, "forecast", orphan, {"version": orphan})
    demand.write("quality-day-2026-04-01.json", {"forecast_version": protected})
    finish_index(maintenance)
    cursor = maintenance.read("archive-retention.json")
    expected_index = index_rows(source)
    migrate_archive(source, target, overwrite_target=True)
    repository.close()
    source.dispose()
    with monkeypatch.context() as patch:
        patch.setattr(source, "connect", disconnected)
        patch.setattr(source, "begin", disconnected)
        migrated_maintenance = SqlDocuments(target, "savings")
        restored = SqlDocuments(target, "demand")
        assert migrated_maintenance.read("archive-retention.json") == cursor
        assert index_rows(target) == expected_index
        assert maintain_documents(migrated_maintenance, NOW) == 1
        assert restored.read(protected_name) == protected_value
        assert not restored.exists(orphan_name)
