"""Independent per-kWh wear arithmetic across capacity, losses and planning decisions."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.installation.model import Installation, InstalledBattery
from open_energy_planner.installation.storage import document, parse
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Battery, Interval, Status
from open_energy_planner.savings.accounting import actual_flows
from open_energy_planner.savings.reference import dispatch
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.app.installation.support.problems import installation_problem

START = datetime(2026, 11, 11, tzinfo=UTC)


@pytest.mark.parametrize("capacity", [2, 8])
@pytest.mark.parametrize("rate,exported,cost", [(0.2, 1, -0.2), (0.45, 0, 0)])
def test_wear_rate_controls_export_without_capacity_scaling(capacity, rate, exported, cost):
    """Selling one stored kWh at .40 earns .20 after .20 wear, but loses money at .45 wear."""
    inputs = installation_problem(
        (Interval(START, 1, 0.5, 0.4, 0, 0),),
        Battery(capacity, 1, discharge_limit_w=1000, wear_cost_per_kwh=rate),
    )
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL
    costs = validate(inputs, result.plan)
    assert result.plan.flows[0].grid_export == pytest.approx(exported)
    assert costs.cash_cost == pytest.approx(cost)
    assert costs.wear_cost == pytest.approx(0.2 if exported else 0)


@pytest.mark.parametrize("capacity", [2, 8])
def test_charging_is_free_of_wear_and_discharge_counts_internal_losses(capacity):
    """1.25 kWh bought at .10 delivers 1 kWh at .8 efficiency and costs .10 wear at .08/kWh."""
    steps = (
        Interval(START, 1, 0.1, 0, 0, 0),
        Interval(START + timedelta(hours=1), 1, 0.5, 0, 1, 0),
    )
    inputs = installation_problem(
        steps,
        Battery(capacity, 0, discharge_efficiency=0.8, wear_cost_per_kwh=0.08),
    )
    result = HighsSolver().solve(inputs)
    assert result.status == Status.OPTIMAL
    costs = validate(inputs, result.plan)
    assert result.plan.flows[0].batteries[0].charge_kwh == pytest.approx(1.25)
    assert result.plan.flows[1].batteries[0].discharge_kwh == pytest.approx(1)
    assert costs.import_cost == pytest.approx(0.125)
    assert costs.wear_cost == pytest.approx(0.1)
    assert costs.cash_cost == pytest.approx(0.225)


@pytest.mark.parametrize("capacity", [2, 8])
def test_self_consumption_reference_uses_the_same_internal_discharge_rate(capacity):
    """Storing 1.25 solar kWh incurs no wear, then supplying 1 kWh costs .08*1.25=.10."""
    inputs = installation_problem(
        (
            Interval(START, 1, 0.3, 0, 0, 1.25),
            Interval(START + timedelta(hours=1), 1, 0.3, 0, 1, 0),
        ),
        Battery(capacity, 0, discharge_efficiency=0.8, wear_cost_per_kwh=0.08),
    )
    charged = dispatch(inputs, inputs.intervals[0], {"battery": 0})
    discharged = dispatch(inputs, inputs.intervals[1], charged["stored"])
    assert charged["wear"] == 0
    assert charged["stored"]["battery"] == pytest.approx(1.25)
    assert discharged["wear"] == pytest.approx(0.1)
    assert discharged["stored"]["battery"] == pytest.approx(0)
    assert discharged["import_kwh"] == pytest.approx(0)


@pytest.mark.parametrize("capacity", [2, 8])
def test_measured_ac_discharge_recovers_internal_energy_before_applying_wear(capacity):
    """.36 measured AC kWh uses one internal kWh through .5 cell, .9 DC and .8 inverter efficiency."""
    inputs = installation_problem(
        (Interval(START, 1, 0.3, 0.1, 0, 0),),
        Battery(capacity, 1, discharge_efficiency=0.5, wear_cost_per_kwh=0.025),
    )
    inputs = replace(
        inputs,
        batteries=(replace(inputs.batteries[0], dc_efficiency=0.9),),
        inverters=(replace(inputs.inverters[0], efficiency=0.8),),
    )
    frame = SimpleNamespace(
        channels={"sensor.import": 0, "sensor.export": 0.36, "sensor.discharge": 0.36},
        battery_start={"battery": 1},
        battery_end={"battery": 0},
    )
    bindings = {
        "imports": ["sensor.import"],
        "exports": ["sensor.export"],
        "batteries": [{"device_id": "battery", "energy_entity": "sensor.discharge"}],
    }
    assert actual_flows(inputs, frame, bindings) == pytest.approx((0, 0.36, 0.025))


def test_installation_roundtrip_preserves_the_explicit_rate_and_configuration_identity():
    """A precise .025-per-kWh setting survives JSON projection and reaches the solver unchanged."""
    battery = InstalledBattery(
        "battery",
        "inverter",
        "sensor.soc",
        "sensor.power",
        8,
        2000,
        2000,
        wear_cost_per_kwh=0.025,
    )
    settings = Installation(
        "sensor.grid", 4000, 4000, (SolarInverter("inverter", 4000, 1),), (battery,)
    )
    projected = document(settings)
    assert projected["batteries"][0]["wear_cost_per_kwh"] == 0.025
    assert "cycle_fee" not in projected["batteries"][0]
    restored = parse(json.loads(json.dumps(projected)))
    assert restored == settings
    assert restored.version == projected["version"]
    assert restored.batteries[0].state(50).wear_cost_per_kwh == 0.025


@pytest.mark.parametrize("rate", [-0.01, float("inf"), float("nan")])
def test_invalid_wear_rates_cannot_enter_installation_or_solver_contracts(rate):
    """Negative and nonfinite rates are rejected instead of crediting battery discharge."""
    with pytest.raises(ValueError):
        Battery(4, 2, wear_cost_per_kwh=rate)
    with pytest.raises(ValueError):
        InstalledBattery(
            "battery",
            "inverter",
            "sensor.soc",
            "sensor.power",
            4,
            2000,
            2000,
            wear_cost_per_kwh=rate,
        )
