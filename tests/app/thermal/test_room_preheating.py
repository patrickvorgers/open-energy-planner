"""Independent calibration, extrapolation and joint dispatch regressions for rooms."""

from datetime import timedelta
from math import exp
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.thermal.room import fit_room, room_work
from open_energy_planner.history.importer import CumulativeHistory, EnergyEndpoint
from open_energy_planner.optimization.domain import Interval
from tests.app.thermal.test_thermal_solver import START, problem, solve

METER = "sensor.room_airco"
ROOM = "00000000-0000-4000-8000-000000000500"


def evidence(corrupt_holdout=False, *, energy_scale=1):
    """Generate complete hourly measurements from independently chosen heat-loss and gain."""
    hour = timedelta(hours=1)
    first = START - 96 * hour
    indoor, weather = {first: 20.0}, []
    endpoints = [EnergyEndpoint(first - hour, 10.0)]
    for index in range(96):
        stamp = first + index * hour
        outside = 6 + index % 4
        energy = (0, 0.8, 0, 1.0)[index % 4] * energy_scale
        next_temperature = outside + (indoor[stamp] - outside) * exp(-0.04)
        next_temperature += 1.5 * energy * (1 - exp(-0.04)) / 0.04
        if corrupt_holdout and index >= 72:
            next_temperature += 0.8
        indoor[stamp + hour] = next_temperature
        weather.append(SimpleNamespace(start=stamp, celsius=outside))
        endpoints.append(EnergyEndpoint(stamp, endpoints[-1].total_kwh + energy))
    return indoor, tuple(weather), CumulativeHistory(METER, tuple(endpoints))


def application():
    """Model one scheduled independent air conditioner with a 0.6-C preheat margin."""
    return {
        "id": ROOM,
        "kind": "space_heating",
        "name": "Room",
        "area_id": "room",
        "climate_entity": "climate.room",
        "maximum_preheat_c": 0.6,
        "sources": [{"energy_entity": METER, "allow_scheduling": True, "full_load_power_w": 1000}],
    }


def test_independent_week_can_identify_effective_heating_without_claiming_cop():
    """The retained exact response is checked against separate hours with heating and idle use."""
    model = fit_room(*evidence(), START, full_load_power_w=1000)
    assert model.loss_per_hour == 0.04
    assert model.gain_c_per_kwh == pytest.approx(1.5)
    assert model.heater_w == 1000
    assert model.holdout_error_c < 1e-8
    assert fit_room(*evidence(corrupt_holdout=True), START, full_load_power_w=1000) is None
    assert fit_room(*evidence(), START, full_load_power_w=800) is None
    assert fit_room(*evidence(), START, full_load_power_w=None) is None


def test_calibrated_room_uses_cheaper_heat_under_the_live_temperature_limit():
    """Dispatch charges full quarter-hour input even when every training hour cycled."""
    model = fit_room(*evidence(energy_scale=0.7), START, full_load_power_w=1000)
    intervals = tuple(
        Interval(START + timedelta(minutes=15 * index), 0.25, price, 0)
        for index, price in enumerate((0.05, 0.7, 0.8, 1.1))
    )
    readings = {"temperature_c": 20.28, "target_c": 20}
    work = room_work(application(), readings, model, {START: 8}, intervals, START, START)
    assert work is not None
    result = solve(problem(work, prices=(0.05, 0.7, 0.8, 1.1)))
    assert [flow.thermal[0].power_w for flow in result.flows] == [1000, 0, 0, 0]
    assert sum(flow.thermal[0].ac_kwh for flow in result.flows) == pytest.approx(0.25)
    assert all(20 <= flow.thermal[0].end_c <= 20.6 for flow in result.flows)
    assert room_work(application(), readings, model, {START: -4}, intervals, START, START) is None
    assert room_work(application(), readings, model, {}, intervals, START, START) is None
