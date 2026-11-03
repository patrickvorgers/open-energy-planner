"""Verify forecast comparison mathematics without starting a database backend."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.forecast_quality.bindings import bindings
from open_energy_planner.forecast_quality.calculation.metrics import integral, series
from tests.app.forecast.support.quality import START, prediction


@pytest.mark.parametrize("condition", ["late", "future", "controlled", "clipped"])
def test_ineligible_hours_never_enter_accuracy_metrics(condition):
    """Late forecasts, unfinished hours, controlled loads and clipped PV can show values but never prove accuracy."""
    forecast = prediction()
    now = START + timedelta(hours=4)
    arguments = {}
    if condition == "late":
        forecast["issued_at"] = (START + timedelta(minutes=1)).isoformat()
    elif condition == "future":
        now = START + timedelta(minutes=59)
    elif condition == "controlled":
        arguments["issue"] = "Controlled operation changes timing"
    else:
        arguments["maximum_comparable_kwh"] = 1
    view = series(
        "source",
        "Source",
        forecast,
        forecast["steps"],
        {START: 1},
        START,
        START + timedelta(hours=1),
        now,
        **arguments,
    )
    assert view["coverage"]["paired_hours"] == 0
    assert all(value is None for value in view["metrics"].values())
    assert view["rows"][0]["error_kwh"] is None


def test_original_projection_does_not_fill_forecast_gaps_or_overlaps():
    """Four .25-kWh quarters make 1 kWh. Removing or duplicating a quarter cannot yield a complete hourly forecast."""
    steps = prediction()["steps"][:4]
    assert integral(steps, START, START + timedelta(hours=1)) == 1
    assert integral(steps[:2] + steps[3:], START, START + timedelta(hours=1)) is None
    assert integral(steps[:2] + [steps[1]] + steps[2:], START, START + timedelta(hours=1)) is None


def test_empty_installation_requests_no_history_or_implicit_sources():
    """Missing configuration and catalogue provide no authorization to request arbitrary HA meters."""
    assert bindings(None, None, (), None) == []


def test_binding_version_follows_registry_device_and_skips_uncatalogued_entities():
    """A reassigned HA meter gets a new evidence identity. Uncatalogued meters cannot acquire a binding."""
    demand = SimpleNamespace(entities=("sensor.house",))

    def catalogue(device_identifier):
        """Describe one known kWh meter on the independently specified registry device."""
        return {
            "entities": [
                {"entity_id": "sensor.house", "device_id": device_identifier, "unit": "kWh"}
            ]
        }

    original = bindings(demand, None, (), catalogue("original-meter"))
    replacement = bindings(demand, None, (), catalogue("replacement-meter"))
    assert original[0]["entity_id"] == replacement[0]["entity_id"] == "sensor.house"
    assert original[0]["version"] != replacement[0]["version"]
    assert bindings(demand, None, (), {"entities": []}) == []
