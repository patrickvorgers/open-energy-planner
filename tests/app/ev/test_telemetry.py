"""Independent energy, timestamp and session ownership examples for retained vehicle SoC."""

from datetime import timedelta

import pytest

from open_energy_planner.appliances.ev.telemetry import (
    empty_telemetry,
    update_telemetry,
    vehicle_estimates,
)
from tests.app.ev.support.devices import CAR, NOW, POINT, SESSION, observations, settings


def example():
    """Start at a known 50 percent and a 100-kWh charger counter with one assigned car."""
    state = {**settings(), "assignments": {POINT: {"vehicle_id": CAR, "session_id": SESSION}}}
    report = observations(type("Service", (), {"configuration_version": "fixture"})())
    report["points"][0]["energy_kwh"] = 100
    return state, report, update_telemetry(empty_telemetry(), state, report)


def test_republication_does_not_reset_charging_estimate():
    """One hour at 2.3 kW adds 1.84 battery kWh to 60 kWh, despite a repeated 50-percent report."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["vehicles"][0].update(observed_at=report["at"], reported_at=report["at"])
    report["points"][0]["energy_kwh"] = 102.3
    telemetry = update_telemetry(telemetry, state, report)
    estimate = vehicle_estimates(telemetry, state["vehicles"], NOW + timedelta(hours=1))[CAR]
    assert estimate["soc"] == pytest.approx(50 + 1.84 / 60 * 100)
    assert estimate["observed_at"] == NOW.isoformat()
    assert estimate["basis"] == "estimated_from_meter"
    assert update_telemetry(telemetry, state, report) == telemetry


@pytest.mark.parametrize("counter, issue", [(1, "meter_reset"), (150, "meter_discontinuity")])
def test_invalid_energy_delta_reanchors_without_credit(counter, issue):
    """A reset or impossible 50-kWh jump over one hour cannot create battery energy."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0]["energy_kwh"] = counter
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == 0
    assert telemetry["sessions"][POINT]["energy_issue"] == issue
    assert telemetry["sessions"][POINT]["energy_kwh"] == counter


def test_missing_energy_bridges_only_same_connection():
    """A missing capture preserves the baseline. A reconnect cannot inherit its unknown energy."""
    state, report, telemetry = example()
    report["at"] = (NOW + timedelta(minutes=30)).isoformat()
    report["points"][0]["energy_kwh"] = None
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["sessions"][POINT]["energy_kwh"] == 100
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["points"][0]["energy_kwh"] = 102.3
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(1.84)
    report["points"][0]["session_id"] = "new-connection"
    report["points"][0]["energy_kwh"] = 105
    telemetry = update_telemetry(telemetry, state, report)
    assert telemetry["vehicles"][CAR]["stored_kwh"] == pytest.approx(1.84)


def test_real_new_sample_corrects_estimate_and_old_anchor_never_expires():
    """A real device sample replaces accumulated energy. Years of age never erase known SoC."""
    state, report, telemetry = example()
    telemetry["vehicles"][CAR]["stored_kwh"] = 6
    report["at"] = (NOW + timedelta(hours=1)).isoformat()
    report["vehicles"][0].update(soc=52, observed_at=report["at"], timestamp_kind="device_sample")
    telemetry = update_telemetry(telemetry, state, report)
    estimate = vehicle_estimates(telemetry, state["vehicles"], NOW + timedelta(days=730))[CAR]
    assert estimate["soc"] == 52
    assert estimate["stored_kwh"] == 0
    assert estimate["basis"] == "last_known"
