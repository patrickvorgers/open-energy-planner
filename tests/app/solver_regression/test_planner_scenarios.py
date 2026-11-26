"""Original compatible economics checked through the current production installation path."""

from dataclasses import replace
from fractions import Fraction

import pytest

from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs.replay import dispatch
from open_energy_planner.optimization.domain import Battery, Status
from open_energy_planner.optimization.export_bonus import ExportBonus
from tests.app.solver_regression.cases import CORE, installation, intervals


@pytest.mark.planner_scenario
@pytest.mark.parametrize("case", CORE, ids=lambda case: case.name)
def test_core_known_optimum(case, solver):
    """Keep S01/S02/S04's exact hand-derived costs, grid exchanges and terminal commands."""
    result = solver.solve(case.problem)
    assert result.status == Status.OPTIMAL
    assert result.stop_reason == "optimal"
    costs = validate(case.problem, result.plan)
    for actual, expected in (
        (costs.cash_cost, case.cash),
        (costs.terminal_value, case.terminal),
        (costs.objective, case.objective),
        (result.best_bound, case.objective),
    ):
        assert actual == pytest.approx(float(expected), abs=1e-6, rel=0)
    for index, (step, flow) in enumerate(
        zip(case.problem.intervals, result.plan.flows, strict=True)
    ):
        assert flow.grid_import == pytest.approx(case.imports[index], abs=1e-6)
        assert flow.grid_export == pytest.approx(case.exports[index], abs=1e-6)
        battery = flow.batteries[0]
        assert battery.end_kwh == pytest.approx(case.energy[index], abs=1e-6)
        power = (battery.discharge_kwh - battery.charge_kwh) * 1000 / step.duration_h
        assert power == pytest.approx(case.battery_power_w[index], abs=1e-6)


@pytest.mark.planner_scenario
@pytest.mark.parametrize(
    "charge_efficiency,discharge_efficiency,wear_cost_per_kwh,cash,energy,exported",
    [(0.8, 0.9, 0, "-0.168", 0.8, 0.72), (1, 1, 0.35, "0", 0, 0)],
)
def test_loss_and_wear_companions(
    solver, charge_efficiency, discharge_efficiency, wear_cost_per_kwh, cash, energy, exported
):
    """Keep 0.72*0.40-0.12=0.168 profit with losses, and idle when 0.35 wear exceeds 0.28 spread."""
    problem = installation(
        CORE[0].problem.intervals,
        battery=Battery(
            1,
            0,
            charge_efficiency=charge_efficiency,
            discharge_efficiency=discharge_efficiency,
            wear_cost_per_kwh=wear_cost_per_kwh,
        ),
    )
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    assert validate(problem, result.plan).cash_cost == pytest.approx(float(Fraction(cash)))
    assert result.plan.flows[0].batteries[0].end_kwh == pytest.approx(energy)
    assert result.plan.flows[1].grid_export == pytest.approx(exported)


@pytest.mark.planner_scenario
def test_equal_optima_do_not_require_a_particular_trace(solver):
    """Wear equal to the 0.28 spread makes zero objective optimal without choosing a tied flow trace."""
    problem = installation(CORE[0].problem.intervals, battery=Battery(1, 0, wear_cost_per_kwh=0.28))
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    assert validate(problem, result.plan).objective == pytest.approx(0)


@pytest.mark.planner_scenario
@pytest.mark.parametrize(
    "buy,can_curtail,cash,exports",
    [(0.20, True, "0.048", 0.60), (0.10, True, "0.040", 0), (0.10, False, "0.048", 0.60)],
)
def test_negative_export_price_respects_pv_capability(solver, buy, can_curtail, cash, exports):
    """Retain full PV costing 0.60*0.08=0.048 unless permitted shutdown replaces it for 0.40*0.10=0.040."""
    problem = installation(intervals((buy,), (-0.08,), pv=1, load=0.40), can_curtail=can_curtail)
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    assert validate(problem, result.plan).cash_cost == pytest.approx(float(Fraction(cash)))
    assert result.plan.flows[0].grid_export == pytest.approx(exports)


@pytest.mark.planner_scenario
def test_curtailment_requires_forecast_production_in_the_control_interval(solver):
    """Keep dark intervals enabled while allowing profitable daytime curtailment."""
    steps = intervals((0.2,) * 4, (-0.08, -0.08, -0.08, 0.1), pv=1)
    steps = (
        replace(steps[0], pv_kwh=0),
        replace(steps[1], pv_kwh=0, control_group="split"),
        replace(steps[2], control_group="split"),
        steps[3],
    )
    problem = installation(steps, battery=Battery(1, 1, minimum_kwh=1), can_curtail=True)
    result = solver.solve(problem)

    assert result.status == Status.OPTIMAL
    validate(problem, result.plan)
    assert [flow.solar[0].enabled for flow in result.plan.flows] == [True, False, False, True]
    assert [flow.grid_export for flow in result.plan.flows] == pytest.approx([0, 0, 0, 1])


@pytest.mark.planner_scenario
def test_negative_export_price_keeps_pv_available_for_useful_storage(solver):
    """Use available storage to avoid paying for export and later import."""
    steps = intervals((0.2, 0.3), (-0.08, -0.08), pv=1)
    steps = (steps[0], replace(steps[1], load_kwh=1, pv_kwh=0))
    problem = installation(steps, battery=Battery(1, 0), can_curtail=True)
    result = solver.solve(problem)

    assert result.status == Status.OPTIMAL
    assert validate(problem, result.plan).cash_cost == pytest.approx(0)
    first, second = result.plan.flows
    assert first.solar[0].enabled
    assert first.batteries[0].charge_kwh == pytest.approx(1)
    assert second.grid_import == pytest.approx(0)


def test_replay_keeps_a_frozen_off_command_when_realized_production_is_zero():
    """A published shutdown remains replayable even if no sun was realized."""
    problem = installation(intervals((0.2,), (-0.08,)), can_curtail=True)
    flow, _wear = dispatch(problem, problem.intervals[0], {}, {"pv": {"enabled": False}})

    assert flow.solar[0].enabled is False
    assert flow.solar[0].used_kwh == 0


@pytest.mark.planner_scenario
def test_terminal_value_prevents_the_horizon_end_dump(solver):
    """Removing S04's continuation value sells 1 kWh for 0.14 and leaves no stored energy."""
    problem = replace(CORE[-1].problem, terminal_valuations=())
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    assert validate(problem, result.plan).objective == pytest.approx(-0.14)
    assert result.plan.flows[1].grid_export == pytest.approx(1)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(0)


def test_infeasible_problem_has_no_publishable_candidate(solver):
    """The original 2-kWh demand cannot pass a 1-kWh grid quarter without storage or PV."""
    problem = installation(intervals((0.20,), (0.10,), load=2))
    result = solver.solve(problem)
    assert result.status == Status.INFEASIBLE
    assert result.plan is None


@pytest.mark.planner_scenario
@pytest.mark.parametrize("initial,cash,exports", [(2, "0", (0, 0)), (0, "-0.16", (1, 1))])
def test_remaining_bonus_budget(solver, initial, cash, exports):
    """The original spent/full extremes require only on/off PV: no exports or two profitable whole kWh."""
    problem = installation(intervals((0.20, 0.20), (-0.03, -0.07), pv=1), can_curtail=True)
    problem = replace(
        problem,
        export_bonuses=(ExportBonus("bonus", (0.13, 0.13), (2, 2), ("period", "period"), initial),),
    )
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    assert validate(problem, result.plan).cash_cost == pytest.approx(
        float(Fraction(cash)), abs=1e-6
    )
    assert tuple(flow.grid_export for flow in result.plan.flows) == pytest.approx(exports, abs=1e-6)


@pytest.mark.planner_scenario
def test_bonus_credit_is_chronological_even_with_forced_export(solver):
    """The original two forced PV exports consume .4 remaining quota first and produce -.072 cash."""
    problem = installation(intervals((0.20, 0.20), (-0.03, 0.05), pv=1))
    problem = replace(
        problem,
        export_bonuses=(ExportBonus("bonus", (0.13, 0.13), (2, 2), ("period", "period"), 1.6),),
    )
    result = solver.solve(problem)
    assert result.status == Status.OPTIMAL
    assert validate(problem, result.plan).cash_cost == pytest.approx(-0.072, abs=1e-6)
    assert result.plan.export_bonus_results[0].quota_kwh == pytest.approx((0.4, 0), abs=1e-6)
