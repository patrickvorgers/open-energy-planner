"""Keep simulated weather inside disposable acceptance while retaining real model and bootstrap behavior."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.forecasting import component
from open_energy_planner.demand import weather
from open_energy_planner.demand.calculation.model import DemandConfiguration, DemandHour, EnergyTerm
from script.release import package_ha_app
from script.release.package_ha_app import package
from script.supervisor import acceptance_app, dev
from tests.test_environment.support import sources


def test_acceptance_bootstrap_requires_marker_and_calls_original_entrypoint(monkeypatch):
    """Only the explicit test image selects fixture weather. The original HA bootstrap still owns startup."""
    monkeypatch.delenv("OEP_SUPERVISOR_SMOKE", raising=False)
    with pytest.raises(RuntimeError, match="disposable Supervisor acceptance"):
        acceptance_app.main()
    original_factory = weather.weather_factory
    monkeypatch.setattr(weather, "weather_factory", original_factory)
    calls = []

    def original_main():
        """Observe the injected port without starting or modifying any Home Assistant instance."""
        calls.append(weather.weather_factory)

    monkeypatch.setitem(sys.modules, "production_ha_app", SimpleNamespace(main=original_main))
    monkeypatch.setenv("OEP_SUPERVISOR_SMOKE", "1")
    acceptance_app.main()
    assert calls == [acceptance_app.temperature_factory]


@pytest.mark.parametrize("missing_weather_period", [None, "past", "future"])
def test_acceptance_weather_feeds_real_training_and_horizon_prediction(
    monkeypatch, missing_weather_period
):
    """Fixture weather remains available, but no archived daily comparison permits learned-model admission."""
    monkeypatch.setitem(sys.modules, "acceptance_sources", sources)
    now = datetime(2026, 10, 28, 15, 45, tzinfo=UTC)

    async def read():
        """Acquire the staged fixture through the same async weather port used by the app lifecycle."""
        async with acceptance_app.temperature_factory() as source:
            return await source.read(None, now)

    temperatures = asyncio.run(read())
    assert len(temperatures) == 840
    assert all(temperature_sample.known_at == now for temperature_sample in temperatures)
    start = now.replace(hour=0, minute=0) - timedelta(days=31)
    samples = tuple(
        DemandHour(start + timedelta(hours=hour_offset), 0.4, 0.4) for hour_offset in range(31 * 24)
    )
    if missing_weather_period:
        omitted = (
            start + timedelta(days=7)
            if missing_weather_period == "past"
            else now.replace(minute=0) + timedelta(hours=40)
        )
        temperatures = tuple(
            temperature_sample
            for temperature_sample in temperatures
            if temperature_sample.start != omitted
        )
    configuration = DemandConfiguration("Europe/Amsterdam", 0, (EnergyTerm("sensor.fixture", 1),))
    model, forecast, selection = component(configuration, samples, temperatures, now, True)
    assert forecast.model == "weekday-hour-v2"
    assert model.model_id == "weekday-hour-v2"
    assert selection["selected"] == "baseline"
    assert selection["status"] == "forward_archive_required"
    assert forecast.training_version
    assert sum(step.duration_h for step in forecast.steps) == 48


def test_only_disposable_context_contains_weather_bootstrap(tmp_path, monkeypatch):
    """Normal release packaging stays fixture-free while the test context retains the original startup script."""
    monkeypatch.setattr(package_ha_app.subprocess, "check_output", lambda *args, **kwargs: "")
    normal = package(tmp_path / "release")
    original_bootstrap = (normal / "script/ha_app.py").read_bytes()
    original_dockerfile = (normal / "Dockerfile").read_bytes()
    stage = tmp_path / "acceptance"
    stage.mkdir()
    monkeypatch.setattr(dev.dev_bundle, "safe_directory", lambda _root: stage)
    dev.prepare_acceptance_sources()
    disposable = stage / "test-app"
    assert (disposable / "script/production_ha_app.py").read_bytes() == original_bootstrap
    assert (disposable / "script/acceptance_sources.py").read_bytes() == (
        dev.ROOT / "tests/test_environment/support/sources.py"
    ).read_bytes()
    assert (disposable / "script/ha_app.py").read_bytes() == (
        dev.ROOT / "script/supervisor/acceptance_app.py"
    ).read_bytes()
    assert {path.name for path in (normal / "script").iterdir()} == {"ha_app.py"}
    assert (normal / "script/ha_app.py").read_bytes() == original_bootstrap
    assert (normal / "Dockerfile").read_bytes() == original_dockerfile
    assert b"OEP_SUPERVISOR_SMOKE" not in original_dockerfile
    assert b"ENV OEP_SUPERVISOR_SMOKE=1" in (disposable / "Dockerfile").read_bytes()


@pytest.mark.parametrize("additional_ready", [False, True])
def test_supervisor_capture_accepts_real_fitted_archives_without_forward_promotion(
    tmp_path, monkeypatch, additional_ready
):
    """Actual 31-day training archives Ridge/weather while short laundry stays in the household remainder."""
    from dataclasses import replace

    from open_energy_planner.appliances.forecasting import parse_selection
    from open_energy_planner.demand.calculation.model import TemperatureHour
    from open_energy_planner.demand.runtime import DemandRuntime
    from open_energy_planner.demand.storage import DemandStore
    from script.supervisor import supervisor_smoke
    from tests.app.demand.test_demand import START, channel, config
    from tests.database.support.archive import make_store

    directory = tmp_path / "apps/data" / supervisor_smoke.APP_SLUG
    store = make_store(directory, DemandStore)
    store.save(config())
    runtime = DemandRuntime(store)
    entities = [
        f"sensor.oep_test_{name}_energy"
        for name in ("heat_pump", "dishwasher", "washing_machine", "dryer")
    ]
    extra_entities = (
        [f"sensor.oep_test_{name}_energy" for name in ("wallbox_a", "wallbox_b", "boiler")]
        if additional_ready
        else []
    )
    entities += extra_entities
    runtime.consumers.sync(
        {
            "entities": [{"entity_id": entity} for entity in entities],
            "suggestions": {
                "consumers": [{"entity_id": entity, "upstream": None} for entity in entities]
            },
        }
    )
    runtime.consumers.save(
        parse_selection(
            [
                {"entity_id": entity, "weather_sensitive": entity == entities[0]}
                for entity in entities
            ]
        ),
        runtime.consumers.version,
    )
    hours = 31 * 24
    now = START + timedelta(hours=hours)
    temperatures = [5 + (hour // 24) % 12 for hour in range(hours)]
    heating = [(20 - value) * 0.05 for value in temperatures]
    runtime.histories["sensor.house"] = channel(
        "sensor.house", [value + 0.4 + 0.05 * len(extra_entities) for value in heating]
    )
    runtime.data_versions["sensor.house"] = "actual-fixture-history"
    runtime.consumers.histories[entities[0]] = channel(entities[0], heating)
    runtime.consumers.histories[entities[1]] = channel(entities[1], [0.1] * hours)
    for entity in extra_entities:
        runtime.consumers.histories[entity] = channel(entity, [0.05] * hours)
    for entity in entities[2:4]:
        history = channel(entity, [0.1] * hours)
        runtime.consumers.histories[entity] = replace(history, hours=history.hours[-49:])
    runtime.temperatures = tuple(
        TemperatureHour(START + timedelta(hours=hour), value, START)
        for hour, value in enumerate(temperatures)
    ) + tuple(TemperatureHour(now + timedelta(hours=hour), 10, now) for hour in range(49))
    assert runtime.install(runtime.build(runtime.job(now)), now)
    result = runtime.consumers.document()["result"]
    assert result["status"] == "partial_history"
    assert set(result["separated_entities"]) == set(entities[:2] + extra_entities)
    assert all(record["forecast"]["model"] == "weekday-hour-v2" for record in result["devices"])
    # Measured heat + .1-kWh/h dishwasher + .3-kWh/h remainder must remain one household boundary.
    assert sum(step.kwh for step in runtime.forecast.steps) == pytest.approx(
        sum(step["kwh"] for step in result["residual"]["steps"])
        + sum(step["kwh"] for record in result["devices"] for step in record["forecast"]["steps"])
    )
    assert sum(step["kwh"] for step in result["residual"]["steps"]) == pytest.approx(14.4)
    monkeypatch.setattr(supervisor_smoke, "SUPERVISOR_ROOT", tmp_path)
    actual = {
        "setup": {"annual_estimate": {"days": 31, "start": 0, "end": 31 * 86400}},
        "consumers": runtime.consumers.document(),
        "demand": runtime.document(now),
    }
    monkeypatch.setattr(supervisor_smoke, "app_request", lambda route: actual[route])

    def require_ready(operation, _description):
        """Require immediate acceptance of the actual fitted and durably archived model results."""
        observed = operation()
        assert observed is not None
        return observed

    monkeypatch.setattr(supervisor_smoke, "wait_for", require_ready)
    archives = supervisor_smoke.pending_model_forecasts()
    assert sum("ridge" in archive for archive in archives) >= 3
    assert any(
        archive.get("weather", {}).get("model") == "xgboost-weather-v1" for archive in archives
    )
    supervisor_smoke.model_history()
