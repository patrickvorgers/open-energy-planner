"""Verify measured battery evidence and calibration deadlines in the Planning basis."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.home_assistant.observations import InputsUnavailable
from open_energy_planner.installation.model import Installation, InstalledBattery
from open_energy_planner.solar.calculation.model import SolarConfiguration, SolarInverter
from tests.app.forecast.support.basis import forecast_basis


def test_measured_state_and_current_calibration_both_bind_the_basis():
    """Current HA power and SoC gate a hold whose deadline contributes to the basis version."""
    now = datetime(2026, 11, 24, 10, 7, tzinfo=UTC)
    battery = InstalledBattery(
        "battery",
        "inverter",
        "sensor.soc",
        "sensor.battery",
        5,
        3500,
        3500,
        dc_efficiency=0.8,
        calibration_enabled=True,
    )
    settings = Installation(
        "sensor.grid", 3000, 4000, (SolarInverter("inverter", 4000, 0.95),), (battery,)
    )
    solar = SimpleNamespace(
        store=SimpleNamespace(issue=None), configuration=SolarConfiguration((), ()), forecast=None
    )
    source, configuration, report = forecast_basis(now, solar, settings=settings)
    hold = now + timedelta(hours=1)

    def directive(device, at, charge_w):
        """Return a hold after checking the independent shared grid cap of 3000 W × 0.95 × 0.8."""
        assert device == battery
        assert at == now
        assert charge_w == pytest.approx(2280)
        return False, hold

    source.store.calibration = SimpleNamespace(issue=None, directive=directive)
    basis = source.current(configuration, now, report=report)
    (planned,) = basis.problem.batteries
    assert planned.battery.initial_kwh == 2.5
    assert planned.calibration_hold_until == hold
    assert basis.problem.intervals[0].start == now
    hold += timedelta(minutes=15)
    updated = source.current(configuration, now, report=report)
    assert updated.problem.batteries[0].calibration_hold_until == hold
    assert updated.forecast_version != basis.forecast_version

    source.store.calibration.issue = "unreadable"
    with pytest.raises(InputsUnavailable, match="battery_calibration_unreadable"):
        source.current(configuration, now, report=report)
    missing_power = replace(
        report,
        readings=tuple(row for row in report.readings if row.key != "battery.battery.power"),
    )
    with pytest.raises(InputsUnavailable, match="battery_sources_required"):
        source.current(configuration, now, report=missing_power)
