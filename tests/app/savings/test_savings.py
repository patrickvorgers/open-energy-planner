"""Independent self-consumption, billing and continuous benefit scenarios with explicit arithmetic."""

from dataclasses import asdict, replace

import pytest

from open_energy_planner.installation.model import PVUnit
from open_energy_planner.savings.context import physical_context
from open_energy_planner.savings.reference import dispatch
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.app.savings.support.scenarios import (
    problem,
)


def test_reference_discharge_covers_load_without_price_decision():
    """One kWh load consumes one stored kWh even at negative export price. No grid trade occurs."""
    planning_problem = problem()
    flow = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 2})
    assert flow["stored"] == {"battery": 1}
    assert flow["import_kwh"] == flow["export_kwh"] == 0


def test_reference_keeps_pv_enabled_and_exports_even_when_it_costs_money():
    """A full battery and 1-kWh production with no load exports the full kWh, costing 0.10."""
    planning_problem = problem(pv_kwh=1, load_kwh=0, hybrid=True)
    flow = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 4})
    assert flow["export_kwh"] == 1
    assert flow["stored"] == {"battery": 4}
    assert flow["export_kwh"] * planning_problem.intervals[0].sell_price == -0.1


def test_reference_dc_clipping_charge_obeys_shared_converter_limit():
    """1.5 DC kWh at 80% produces 1 AC kWh and stores the otherwise clipped 0.25 DC kWh."""
    planning_problem = problem(pv_kwh=1.5, load_kwh=1, hybrid=True, efficiency=0.8)
    flow = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 2})
    assert flow["stored"]["battery"] == pytest.approx(2.25)
    assert flow["import_kwh"] == flow["export_kwh"] == 0


def test_original_plan_physics_reconstruct_without_solver():
    """JSON serialization retains multiple-device identifiers and terminal curves exactly for reference calculation."""
    import json

    planning_problem = problem(hybrid=True)
    value = json.loads(
        json.dumps(asdict(planning_problem), default=lambda value_item: value_item.isoformat())
    )
    rebuilt = physical_context({"input_snapshot": {"problem": value}})
    assert rebuilt == planning_problem


def test_hybrid_charges_local_and_other_inverter_surplus_without_creating_energy():
    """0.1 local plus 0.5 remote kWh stores exactly 0.6, leaving no grid trade or unexplained energy."""
    planning_problem = problem(pv_kwh=0.1, load_kwh=0, hybrid=True)
    planning_problem = replace(
        planning_problem,
        inverters=(*planning_problem.inverters, SolarInverter("other", 4000, 1, False)),
        pv_sources=(*planning_problem.pv_sources, PVUnit("garage", "other", False)),
        intervals=(
            replace(
                planning_problem.intervals[0],
                pv_kwh=0.6,
                pv_sources=(("roof", 0.1), ("garage", 0.5)),
            ),
        ),
    )
    result = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 2})
    assert result["stored"]["battery"] == pytest.approx(2.6)
    assert result["import_kwh"] == 0
    assert result["export_kwh"] == pytest.approx(0)


def test_shared_converter_cannot_create_dc_clipping_while_charging_from_ac():
    """Two batteries behind one converter can absorb one remote kWh once. Negative AC flow is not clipped PV."""
    planning_problem = problem(pv_kwh=0, load_kwh=0, hybrid=True)
    second = replace(planning_problem.batteries[0], device_id="second")
    planning_problem = replace(
        planning_problem,
        inverters=(*planning_problem.inverters, SolarInverter("other", 4000, 1, False)),
        batteries=(*planning_problem.batteries, second),
        pv_sources=(*planning_problem.pv_sources, PVUnit("garage", "other", False)),
        intervals=(
            replace(
                planning_problem.intervals[0], pv_kwh=1, pv_sources=(("roof", 0), ("garage", 1))
            ),
        ),
        terminal_valuations=(),
    )
    result = dispatch(planning_problem, planning_problem.intervals[0], {"battery": 2, "second": 2})
    assert sum(result["stored"].values()) == pytest.approx(5)
    assert result["export_kwh"] == 0


def test_reference_uses_real_dc_efficiency_advantage():
    """One DC kWh stores 1 kWh locally, versus .64 through two 80% AC converters."""
    hybrid = problem(pv_kwh=1, load_kwh=0, hybrid=True, efficiency=0.8)
    ac = replace(
        hybrid,
        inverters=(
            SolarInverter("inv", 4000, 0.8, False),
            SolarInverter("battery-inv", 4000, 0.8, False),
        ),
        batteries=(replace(hybrid.batteries[0], inverter_id="battery-inv"),),
    )
    direct = dispatch(hybrid, hybrid.intervals[0], {"battery": 2})
    converted = dispatch(ac, ac.intervals[0], {"battery": 2})
    assert direct["stored"]["battery"] == pytest.approx(3)
    assert converted["stored"]["battery"] == pytest.approx(2.64)
