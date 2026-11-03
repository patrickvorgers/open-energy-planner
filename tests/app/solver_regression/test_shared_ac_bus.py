"""Shared AC conservation, independent device limits and original multi-battery optima."""

from dataclasses import replace

import pytest

from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.domain import Status
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.app.solver_regression.bus_cases import BUS_CASES, M01, M03, M04, M07, horizon, storage
from tests.app.solver_regression.cases import CORE


def checked_case(case, result):
    """Compare actual current grid totals and per-device stocks with original exact expectations."""
    assert result.status == Status.OPTIMAL
    costs = validate(case.problem, result.plan)
    for actual, expected in (
        (costs.cash_cost, case.cash),
        (costs.terminal_value, case.terminal),
        (costs.objective, case.objective),
        (result.best_bound, case.objective),
        (sum(flow.grid_import for flow in result.plan.flows), case.imports),
        (sum(flow.grid_export for flow in result.plan.flows), case.exports),
    ):
        assert actual == pytest.approx(float(expected), abs=1e-6, rel=0), case.name
    assert {
        unit.device_id: unit.end_kwh for unit in result.plan.flows[-1].batteries
    } == pytest.approx({name: float(energy) for name, energy in case.final}, abs=1e-6)


@pytest.mark.planner_scenario
@pytest.mark.parametrize("case", BUS_CASES, ids=lambda case: case.name)
def test_two_batteries_attain_independently_derived_bound(case, solver):
    """Retain M01-M03/M05/M06/M08 physical/economic bounds without fictitious source-to-sink allocations."""
    checked_case(case, solver.solve(case.problem))


def test_shared_household_deficit_is_infeasible_without_phantom_circulation(solver):
    """Empty batteries cannot fill the original 1.1-kWh load through a 1-kWh connection."""
    result = solver.solve(M07)
    assert result.status == Status.INFEASIBLE
    assert result.plan is None


@pytest.mark.planner_scenario
@pytest.mark.parametrize("index", [0, 2])
def test_shared_grid_and_transfer_opportunity_survive_120_steps(index, solver):
    """A zero-opportunity tail preserves M01's -.275 and M03's -1 cash across 48 hours."""
    case = replace(BUS_CASES[index], problem=horizon(BUS_CASES[index].problem))
    assert len(case.problem.intervals) == 120
    assert sum(step.duration_h for step in case.problem.intervals) == 48
    checked_case(case, solver.solve(case.problem))


def test_import_limit_includes_load_and_both_local_charge_limits(solver):
    """One shared kWh includes .25 household demand, leaving .75 within both .5-kWh charger limits."""
    first = solver.solve(M01).plan.flows[0]
    assert first.grid_import == pytest.approx(1)
    assert sum(unit.charge_kwh for unit in first.batteries) == pytest.approx(0.75)
    assert all(0.25 - 1e-7 <= unit.charge_kwh <= 0.5 + 1e-7 for unit in first.batteries)
    assert sum(port.import_kwh for port in first.inverters) == pytest.approx(0.75)


def test_transfers_change_each_state_but_are_not_external_grid_consumption(solver):
    """Source exports .4 AC internally and destination stores .32 after .8 conversion, with no grid exchange."""
    plan = solver.solve(M04).plan
    validate(M04, plan)
    first = plan.flows[0]
    assert first.grid_import == pytest.approx(0)
    assert first.grid_export == pytest.approx(0)
    assert {unit.device_id: unit.end_kwh for unit in first.batteries} == pytest.approx(
        {"A": 0.5, "B": 0.32}
    )
    ports = {port.inverter_id: port for port in first.inverters}
    assert ports["A"].export_kwh == pytest.approx(0.4)
    assert ports["B"].import_kwh == pytest.approx(0.4)
    assert {
        unit.device_id: unit.discharge_kwh for unit in plan.flows[1].batteries
    } == pytest.approx({"A": 0.5, "B": 0.32})


def test_configuration_order_does_not_swap_battery_identity(solver):
    """Reversing the two units preserves named .5/.5 stock after the original internal transfer."""
    problem = replace(M03, batteries=tuple(reversed(M03.batteries)))
    plan = solver.solve(problem).plan
    validate(problem, plan)
    assert {unit.device_id: unit.end_kwh for unit in plan.flows[0].batteries} == pytest.approx(
        {"A": 0.5, "B": 0.5}
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_battery",
        "duplicate_battery",
        "state",
        "cost",
        "shared_import",
        "shared_export",
        "duplicate_inverter",
        "unknown_inverter",
    ],
)
def test_independent_checker_rejects_corrupted_shared_bus_results(mutation, solver):
    """Reject wrong identities, stocks, financial totals and shared limits on actual physical flow records."""
    problem = (
        M01
        if mutation == "shared_import"
        else BUS_CASES[1].problem
        if mutation == "shared_export"
        else M03
    )
    plan = solver.solve(problem).plan
    validate(problem, plan)
    flow = plan.flows[0]
    if mutation == "missing_battery":
        flow = replace(flow, batteries=flow.batteries[:1])
    elif mutation == "duplicate_battery":
        flow = replace(flow, batteries=flow.batteries + (flow.batteries[0],))
    elif mutation == "state":
        flow = replace(
            flow, batteries=(replace(flow.batteries[0], end_kwh=0.9), *flow.batteries[1:])
        )
    elif mutation == "cost":
        plan = replace(plan, costs=replace(plan.costs, import_cost=-100))
    elif mutation == "shared_import":
        flow = replace(flow, grid_import=1.25)
    elif mutation == "shared_export":
        flow = replace(flow, grid_export=1)
    elif mutation == "duplicate_inverter":
        flow = replace(flow, inverters=flow.inverters + (flow.inverters[0],))
    else:
        flow = replace(
            flow, inverters=(replace(flow.inverters[0], inverter_id="missing"), *flow.inverters[1:])
        )
    expected_issue = {
        "missing_battery": "Battery identities",
        "duplicate_battery": "Battery identities",
        "state": "Stored energy conservation",
        "cost": "Financial totals",
        "shared_import": "Grid bound",
        "shared_export": "Grid bound",
        "duplicate_inverter": "Inverter identities",
        "unknown_inverter": "Inverter identities",
    }[mutation]
    with pytest.raises(ValueError, match=f"^{expected_issue}$"):
        validate(problem, replace(plan, flows=(flow, *plan.flows[1:])))


def test_internal_transfer_cannot_become_fictitious_grid_income(solver):
    """Only 1 kWh of physical grid export earns 1. A claimed 1.5 sale must fail independent accounting."""
    plan = solver.solve(M03).plan
    assert validate(M03, plan).cash_cost == pytest.approx(-1)
    forged = replace(
        plan, costs=replace(plan.costs, export_income=1.5, cash_cost=-1.5, objective=-1.5)
    )
    with pytest.raises(ValueError, match="Financial totals"):
        validate(M03, forged)


@pytest.mark.parametrize(
    "invalid", ["duplicate", "charge_efficiency", "discharge_efficiency", "rating"]
)
def test_invalid_multi_battery_inputs_are_rejected(invalid):
    """Reject duplicate battery identities, invalid physical efficiency and negative inverter ratings."""
    expected_issue = {
        "duplicate": "Duplicate battery state",
        "charge_efficiency": "Efficiency must lie in",
        "discharge_efficiency": "Efficiency must lie in",
        "rating": "Invalid solar quantity",
    }[invalid]
    with pytest.raises(ValueError, match=expected_issue):
        if invalid == "duplicate":
            replace(M03, batteries=(M03.batteries[0], M03.batteries[0]))
        elif invalid == "charge_efficiency":
            replace(M03.batteries[0].battery, charge_efficiency=0)
        elif invalid == "discharge_efficiency":
            replace(M03.batteries[0].battery, discharge_efficiency=1.01)
        else:
            replace(M03.inverters[0], ac_limit_w=-1)


def test_individual_inverter_limit_is_checked_even_when_shared_grid_is_valid(solver):
    """A shared .75-kWh export cannot conceal exceeding A's tightened .25-kWh converter limit."""
    problem = BUS_CASES[1].problem
    plan = solver.solve(problem).plan
    validate(problem, plan)
    narrowed = replace(
        problem, inverters=(replace(problem.inverters[0], ac_limit_w=1000), *problem.inverters[1:])
    )
    with pytest.raises(ValueError, match="Converter bounds"):
        validate(narrowed, plan)


def test_individual_charge_limit_is_checked_under_valid_shared_import(solver):
    """Reject a candidate whose terminal charging exceeds a battery's reduced zero charge rating."""
    plan = solver.solve(M01).plan
    validate(M01, plan)
    index = next(index for index, unit in enumerate(plan.flows[0].batteries) if unit.charge_kwh > 0)
    units = list(M01.batteries)
    units[index] = replace(units[index], battery=replace(units[index].battery, charge_limit_w=0))
    with pytest.raises(ValueError, match="Battery terminal bounds"):
        validate(replace(M01, batteries=tuple(units)), plan)


def test_destination_charge_loss_cannot_be_removed_from_transfer_state(solver):
    """Destination .4 AC import stores .32. Forging .4 stored energy must fail conservation."""
    plan = solver.solve(M04).plan
    validate(M04, plan)
    first = plan.flows[0]
    forged = replace(
        first,
        batteries=tuple(
            replace(unit, end_kwh=0.4) if unit.device_id == "B" else unit
            for unit in first.batteries
        ),
    )
    with pytest.raises(ValueError, match="^Stored energy conservation$"):
        validate(M04, replace(plan, flows=(forged, plan.flows[1])))


def test_adding_a_third_battery_does_not_duplicate_the_initial_energy(solver):
    """Adding empty C cannot create energy. The original 1 kWh remains the total later export."""
    problem = replace(
        M03,
        batteries=(*M03.batteries, storage("C")),
        inverters=(*M03.inverters, SolarInverter("C", 4000, 1)),
    )
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    assert validate(problem, result.plan).objective == pytest.approx(-1)
    assert sum(flow.grid_export for flow in result.plan.flows) == pytest.approx(1)


def test_one_solver_can_interleave_independent_installations_without_state_leak(solver):
    """Interleave two-battery transfer, single-battery arbitrage and wear without carrying state across rounds."""
    for case in (BUS_CASES[2], CORE[0], BUS_CASES[3], BUS_CASES[2]):
        result = solver.solve(case.problem)
        assert result.status == Status.OPTIMAL
        assert validate(case.problem, result.plan).objective == pytest.approx(float(case.objective))


def test_symmetric_converter_transfer_has_independent_current_economics(solver):
    """Current bidirectional .8 conversion gives .5*.8+.5*.8*.8*.8=.656 exported kWh."""
    plan = solver.solve(M04).plan
    assert validate(M04, plan).cash_cost == pytest.approx(-0.656)
    assert plan.flows[1].grid_export == pytest.approx(0.656)
