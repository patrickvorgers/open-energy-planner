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
    """Complete fixture weather trains Ridge. Removing past or future coverage retains the production fallback."""
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
    model, forecast = component(configuration, samples, temperatures, now, True)
    assert forecast.model == ("ridge-v2" if missing_weather_period is None else "weekday-hour-v2")
    assert forecast.training_version
    assert sum(step.duration_h for step in forecast.steps) == 48
    if missing_weather_period is None:
        assert model.temperature_enabled


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
