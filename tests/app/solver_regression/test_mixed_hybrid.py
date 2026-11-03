"""Compatible mixed-hybrid invariants on current per-inverter AC and battery DC contracts."""

from dataclasses import replace

import pytest

from open_energy_planner.installation.model import InstallationProblem, PVUnit
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.domain import Costs, Status
from open_energy_planner.optimization.export_bonus import ExportBonus
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.app.solver_regression.bus_cases import M01, horizon, slots, storage
from tests.app.solver_regression.cases import valuation


def mixed():
    """Declare the historical 95% DC and 80% AC devices with current symmetric converter semantics."""
    steps = tuple(
        replace(step, pv_sources=(("pv", step.pv_kwh),))
        for step in slots((1, 1), (0, 1), pv=(1, 0))
    )
    units = (replace(storage("H"), dc_efficiency=0.95), storage("A"))
    return InstallationProblem(
        steps,
        (SolarInverter("H", 4000, 0.8, True), SolarInverter("A", 4000, 0.8)),
        units,
        (PVUnit("pv", "H", False),),
        4000,
        4000,
    )


def checked(problem, solver, objective):
    """Require actual optimality and independent current financial validation against a hand-derived constant."""
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL, result
    assert validate(problem, result.plan).objective == pytest.approx(objective, abs=1e-7)
    assert result.best_bound == pytest.approx(objective, abs=1e-7)
    return result.plan


def test_h03_shared_grid_export_and_local_hybrid_rating(solver):
    """Two full ideal batteries respect .75 shared AC export and at most .5 from the 2-kW hybrid."""
    problem = mixed()
    problem = replace(
        problem,
        intervals=(replace(problem.intervals[0], pv_kwh=0, pv_sources=(("pv", 0),), sell_price=1),),
        inverters=(SolarInverter("H", 2000, 1, True), SolarInverter("A", 4000, 1)),
        batteries=(storage("H", 1), storage("A", 1)),
        grid_export_limit_w=3000,
    )
    plan = checked(problem, solver, -0.75)
    assert plan.flows[0].grid_export == pytest.approx(0.75)
    assert (
        next(port for port in plan.flows[0].inverters if port.inverter_id == "H").export_kwh
        <= 0.5 + 1e-7
    )


def test_h04_pv_and_battery_share_hybrid_output(solver):
    """Original .75-kWh demand cannot pass a .5-kWh shared hybrid AC limit without another energy source."""
    problem = mixed()
    problem = replace(
        problem,
        intervals=(
            replace(problem.intervals[0], load_kwh=0.75, pv_kwh=0.5, pv_sources=(("pv", 0.5),)),
        ),
        inverters=(SolarInverter("H", 2000, 1, True), SolarInverter("A", 4000, 1)),
        batteries=(storage("H", 0.5), storage("A")),
        grid_import_limit_w=0,
        grid_export_limit_w=0,
    )
    result = solver.solve(problem)
    assert result.status == Status.INFEASIBLE
    assert result.plan is None


def h05():
    """Preserve .5*.95 DC storage plus .5 ideal AC charge, earning .1 import and .0975 terminal value."""
    steps = tuple(
        replace(step, pv_sources=(("pv", step.pv_kwh),)) for step in slots((-0.2,), (0,), pv=(0.5,))
    )
    units = (replace(storage("H"), dc_efficiency=0.95), storage("A"))
    return InstallationProblem(
        steps,
        (SolarInverter("H", 2000, 0.8, True), SolarInverter("A", 4000, 1)),
        units,
        (PVUnit("pv", "H", True),),
        2000,
        4000,
        terminal_valuations=tuple(valuation(unit, 0.1, steps) for unit in units),
    )


def test_h05_direct_dc_can_coexist_with_negative_price_import(solver):
    """The original .5 paid AC import and .475 direct DC charge give -.1975 economic cost."""
    plan = checked(h05(), solver, -0.1975)
    assert plan.flows[0].grid_import == pytest.approx(0.5)
    assert {unit.device_id: unit.end_kwh for unit in plan.flows[0].batteries} == pytest.approx(
        {"H": 0.475, "A": 0.5}
    )


def test_separate_ac_opportunity_survives_120_steps(solver):
    """The original AC branch still exports .8 cubed=.512 after an empty 48-hour tail."""
    problem = mixed()
    steps = tuple(replace(step, pv_sources=(("pv", step.pv_kwh),)) for step in problem.intervals)
    units = tuple(replace(unit, dc_efficiency=1) for unit in problem.batteries)
    problem = replace(
        problem,
        intervals=steps,
        batteries=units,
        inverters=(
            SolarInverter("H", 4000, 0.8),
            SolarInverter("A", 4000, 0.8),
            SolarInverter("pv", 4000, 0.8),
        ),
        pv_sources=(PVUnit("pv", "pv", False),),
    )
    checked(horizon(problem), solver, -0.512)


@pytest.mark.parametrize(
    "mutation", ["wrong_battery", "extra_dc", "wrong_state", "wrong_cost", "wrong_loss"]
)
def test_independent_validator_rejects_mixed_mutations(solver, mutation):
    """Reject identity, terminal energy, state, loss or financial corruption of the original compatible H05 candidate."""
    problem = h05()
    plan = checked(problem, solver, -0.1975)
    first = plan.flows[0]
    batteries = list(first.batteries)
    if mutation == "wrong_battery":
        batteries[1] = replace(batteries[1], device_id="missing")
    elif mutation == "extra_dc":
        batteries[0] = replace(batteries[0], charge_kwh=batteries[0].charge_kwh + 0.01)
    elif mutation == "wrong_state":
        batteries[0] = replace(batteries[0], end_kwh=batteries[0].end_kwh + 0.01)
    elif mutation == "wrong_loss":
        problem = replace(
            problem,
            batteries=(replace(problem.batteries[0], dc_efficiency=0.9), problem.batteries[1]),
        )
    else:
        plan = replace(plan, costs=replace(plan.costs, objective=0))
    expected_issue = {
        "wrong_battery": "Battery identities",
        "extra_dc": "Stored energy conservation",
        "wrong_state": "Stored energy conservation",
        "wrong_cost": "Financial totals",
        "wrong_loss": "DC conservation",
    }[mutation]
    with pytest.raises(ValueError, match=f"^{expected_issue}$"):
        validate(problem, replace(plan, flows=(replace(first, batteries=tuple(batteries)),)))


@pytest.mark.parametrize(
    "invalid", ["missing", "zero_efficiency", "excess_efficiency", "rating", "hybrid_permission"]
)
def test_invalid_hybrid_configuration(invalid):
    """Reject current missing identities, invalid DC loss/rating or PV/storage sharing without hybrid permission."""
    problem = mixed()
    expected_issue = {
        "missing": "Invalid physical inverter links",
        "zero_efficiency": "Invalid solar quantity",
        "excess_efficiency": "Invalid solar quantity",
        "rating": "Invalid solar quantity",
        "hybrid_permission": "PV and battery sharing requires a hybrid inverter",
    }[invalid]
    with pytest.raises(ValueError, match=f"^{expected_issue}$"):
        if invalid == "missing":
            replace(
                problem,
                batteries=(
                    replace(problem.batteries[0], inverter_id="missing"),
                    problem.batteries[1],
                ),
            )
        elif invalid == "zero_efficiency":
            replace(problem.batteries[0], dc_efficiency=0)
        elif invalid == "excess_efficiency":
            replace(problem.batteries[0], dc_efficiency=1.01)
        elif invalid == "rating":
            replace(problem.inverters[0], ac_limit_w=-1)
        else:
            replace(
                problem,
                inverters=(replace(problem.inverters[0], hybrid=False), problem.inverters[1]),
            )


def test_mixed_and_ac_rounds_remain_isolated(solver):
    """The compatible original -.1975 hybrid and -.275 AC bounds survive interleaved native solves."""
    for problem, value in ((h05(), -0.1975), (M01, -0.275), (h05(), -0.1975)):
        checked(problem, solver, value)


def test_simultaneous_hybrid_ac_input_output_is_rejected(solver):
    """A forged .2-kWh input/output pair balances AC/DC and storage but violates one converter direction."""
    problem = mixed()
    problem = replace(
        problem,
        intervals=(
            replace(problem.intervals[0], pv_kwh=0.2, load_kwh=0.2, pv_sources=(("pv", 0.2),)),
        ),
        inverters=(SolarInverter("H", 4000, 1, True), SolarInverter("A", 4000, 1)),
        batteries=(storage("H"), storage("A")),
    )
    plan = solver.solve(problem).plan
    validate(problem, plan)
    first = plan.flows[0]
    first = replace(
        first,
        grid_import=0.2,
        inverters=tuple(
            replace(port, import_kwh=0.2, export_kwh=0.2) if port.inverter_id == "H" else port
            for port in first.inverters
        ),
        batteries=tuple(
            replace(unit, charge_kwh=0.2, end_kwh=0.2) if unit.device_id == "H" else unit
            for unit in first.batteries
        ),
    )
    with pytest.raises(ValueError, match="Converter bounds"):
        validate(problem, replace(plan, flows=(first,), costs=Costs(0.2, 0, 0, 0, 0.2, 0, 0.2)))


def test_pv_to_standalone_crosses_both_converters_and_cell_efficiency(solver):
    """A full hybrid retains 1 while .5*.8*.8*.9=.288 is stored separately, worth the original 1.288."""
    problem = mixed()
    steps = (replace(problem.intervals[0], pv_kwh=0.5, pv_sources=(("pv", 0.5),), sell_price=0),)
    units = (storage("H", 1), storage("A", charge_efficiency=0.9))
    problem = replace(
        problem,
        intervals=steps,
        batteries=units,
        grid_import_limit_w=0,
        grid_export_limit_w=0,
        terminal_valuations=tuple(valuation(unit, 1, steps) for unit in units),
    )
    plan = checked(problem, solver, -1.288)
    assert next(
        unit for unit in plan.flows[0].batteries if unit.device_id == "A"
    ).end_kwh == pytest.approx(0.288)


def test_stored_pv_without_grid_export_cannot_earn_bonus(solver):
    """With no grid export, current aggregate-export rules also pay zero and preserve H05's -.1975 cost."""
    problem = replace(
        h05(),
        grid_export_limit_w=0,
        export_bonuses=(ExportBonus("bonus", (1,), (1,), ("period",), 0),),
    )
    plan = checked(problem, solver, -0.1975)
    assert plan.costs.bonus_income == pytest.approx(0)
    assert plan.export_bonus_results[0].quota_kwh == pytest.approx((0,))


def test_reordered_h05_units_preserve_named_dc_and_ac_states(solver):
    """Reordering original H05 preserves distinct .475 DC and .5 AC-derived stocks by stable battery ID."""
    problem = h05()
    problem = replace(problem, batteries=tuple(reversed(problem.batteries)))
    plan = checked(problem, solver, -0.1975)
    assert {unit.device_id: unit.end_kwh for unit in plan.flows[0].batteries} == pytest.approx(
        {"H": 0.475, "A": 0.5}
    )
