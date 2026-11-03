"""Independent primary-objective scenarios and publication checks for consumption planning."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.installation.validation import validate
from open_energy_planner.installation.valuation import TerminalValuation
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.adapters.process.worker import ProcessSolver
from open_energy_planner.optimization.calculation.consumption import evaluate_consumption
from open_energy_planner.optimization.domain import (
    Battery,
    Interval,
    SolveOptions,
    Status,
    Strategy,
)
from open_energy_planner.optimization.export_bonus import ExportBonus
from open_energy_planner.planning import PlanningService
from open_energy_planner.solar.calculation.model import SolarInverter
from open_energy_planner.synthetic import capture

START = datetime(2026, 10, 23, tzinfo=UTC)


def installation_problem(
    intervals, battery=None, *, curtail=False, terminal_rate=0, export_bonuses=()
):
    """Declare ideal separate converters, fixed PV by default and explicit accessible-stock valuation."""
    end = intervals[-1].start + timedelta(hours=intervals[-1].duration_h)
    return InstallationProblem(
        tuple(replace(step, pv_sources=(("pv", step.pv_kwh),)) for step in intervals),
        (SolarInverter("pv_inv", 4000, 1),)
        + ((SolarInverter("battery_inv", 4000, 1),) if battery else ()),
        (BatteryState("battery", "battery_inv", battery),) if battery else (),
        (PVUnit("pv", "pv_inv", curtail),),
        4000,
        4000,
        export_bonuses=export_bonuses,
        terminal_valuations=(
            TerminalValuation(
                "battery",
                battery.minimum_kwh,
                battery.capacity_kwh - battery.minimum_kwh,
                terminal_rate,
                end,
                end + timedelta(hours=24),
                "trailing_window_proxy",
            ),
        )
        if battery and terminal_rate
        else (),
    )


def solve(problem, strategy=Strategy.CONSUMPTION):
    """Solve a small exact scenario and independently validate physics, money and primary kWh."""
    result = HighsSolver().solve(problem, SolveOptions(strategy=strategy))
    assert result.status == Status.OPTIMAL, result
    validate(problem, result.plan)
    if strategy == Strategy.CONSUMPTION:
        assert evaluate_consumption(problem, result.plan).corrected_import_kwh == pytest.approx(
            result.consumption_optimum_kwh, abs=2e-7
        )
    return result


@pytest.mark.planner_scenario
def test_negative_price_profit_cannot_buy_extra_import():
    """PV can supply 1 kWh free. Cost mode earns 1 EUR importing instead, while consumption uses PV."""
    problem = installation_problem((Interval(START, 1, -1, 0, 1, 1),), curtail=True)
    cost = solve(problem, Strategy.COST)
    consumption = solve(problem)
    assert cost.plan.flows[0].grid_import == pytest.approx(1)
    assert consumption.plan.flows[0].grid_import == pytest.approx(0, abs=2e-7)
    assert consumption.plan.costs.cash_cost == pytest.approx(0, abs=2e-7)


@pytest.mark.planner_scenario
def test_financial_tie_breaker_uses_cheapest_equal_import_plan():
    """Lossless empty storage shifts one required import from EUR 1 to EUR .1 without increasing kWh."""
    problem = installation_problem(
        (Interval(START, 1, 0.1, 0), Interval(START + timedelta(hours=1), 1, 1, 0, 1)),
        Battery(1, 0),
    )
    result = solve(problem)
    assert result.consumption_optimum_kwh == pytest.approx(1)
    assert result.plan.flows[0].grid_import == pytest.approx(1)
    assert result.plan.costs.cash_cost == pytest.approx(0.1)


@pytest.mark.planner_scenario
def test_existing_stock_is_not_free_avoided_import():
    """Using .8 internal kWh saves .8 import but adds 1 replacement kWh at 80% charge efficiency. Retain stock."""
    problem = installation_problem(
        (Interval(START, 1, 1, 0, 0.8),), Battery(1, 0.8, charge_efficiency=0.8)
    )
    result = solve(problem)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(0.8)
    assert result.plan.flows[0].grid_import == pytest.approx(0.8)
    assert result.consumption_optimum_kwh == pytest.approx(0.8)


@pytest.mark.planner_scenario
def test_solar_can_replace_used_starting_stock_within_horizon():
    """One stored kWh supplies load, then one free PV kWh refills it: import and replacement are both zero."""
    problem = installation_problem(
        (Interval(START, 1, 1, 0, 1), Interval(START + timedelta(hours=1), 1, 1, 0, 0, 1)),
        Battery(1, 1),
    )
    result = solve(problem)
    assert result.plan.flows[0].batteries[0].discharge_kwh == pytest.approx(1)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(1)
    assert result.consumption_optimum_kwh == pytest.approx(0, abs=2e-7)


@pytest.mark.planner_scenario
def test_no_surplus_credit_for_profitable_grid_charging():
    """An empty battery at a negative price must not earn negative-import credit by ending full."""
    result = solve(installation_problem((Interval(START, 1, -10, 0),), Battery(1, 0)))
    assert result.plan.flows[0].grid_import == pytest.approx(0, abs=2e-7)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(0, abs=2e-7)


@pytest.mark.planner_scenario
def test_terminal_money_cannot_buy_extra_primary_import():
    """Even EUR 100 per final stored kWh cannot justify importing into an initially empty battery."""
    result = solve(
        installation_problem((Interval(START, 1, 0.1, 0),), Battery(1, 0), terminal_rate=100)
    )
    assert result.plan.flows[0].grid_import == pytest.approx(0, abs=2e-7)


@pytest.mark.planner_scenario
def test_on_off_bonus_cap_breaks_ties_without_changing_import_priority():
    """At zero import, enabling 1 PV kWh costs 1 EUR but earns 2.5 EUR capped bonus, beating shutdown."""
    bonus = ExportBonus("cap", (5,), (0.5,), ("period",), 0)
    result = solve(
        installation_problem(
            (Interval(START, 1, 0.1, -1, 0, 1),),
            curtail=True,
            export_bonuses=(bonus,),
        )
    )
    assert result.plan.flows[0].grid_import == pytest.approx(0, abs=2e-7)
    assert result.plan.flows[0].grid_export == pytest.approx(1)
    assert result.plan.costs.objective == pytest.approx(-1.5)


@pytest.mark.parametrize("issue", ["consumption_time_limit", "consumption_unproven"])
def test_unproved_primary_never_returns_a_publishable_candidate(monkeypatch, issue):
    """A stopped primary solve cannot be mislabeled as minimum import even if a feasible financial seed exists."""
    from open_energy_planner.optimization.adapters.highs import solver

    monkeypatch.setattr(solver, "prioritize_consumption", lambda *args: (None, issue))
    result = HighsSolver().solve(
        installation_problem((Interval(START, 1, 0.1, 0, 1),)),
        SolveOptions(strategy=Strategy.CONSUMPTION),
    )
    assert result.status == Status.NO_CANDIDATE
    assert result.plan is None
    assert result.stop_reason == issue


@pytest.mark.planner_scenario
def test_mandatory_reserve_precedes_minimum_import():
    """Restoring one internal kWh requires one grid kWh even though the unconstrained import optimum is zero."""
    problem = installation_problem(
        (Interval(START, 1, 10, 0),), Battery(2, 0, minimum_kwh=1, recover_reserve=True)
    )
    result = solve(problem)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(1)
    assert result.consumption_optimum_kwh == pytest.approx(1)


@pytest.mark.planner_scenario
def test_converter_losses_are_counted_once_at_grid_boundary():
    """With .8 cell and .5 converter charging efficiency, .4 lost internal kWh requires exactly 1 grid kWh."""
    problem = InstallationProblem(
        (Interval(START, 1, 1, 0, 0.4),),
        (SolarInverter("inv", 4000, 0.5),),
        (BatteryState("one", "inv", Battery(1, 1, charge_efficiency=0.8)),),
        (),
        4000,
        4000,
    )
    result = solve(problem)
    assert result.consumption_optimum_kwh == pytest.approx(0.4)
    assert evaluate_consumption(problem, result.plan).terminal_replacement_kwh == pytest.approx(
        0, abs=2e-7
    )
    # Accounting alone: an independently specified final internal stock of .6
    # is a .4 deficit, divided by .8 * .5 = .4, hence one replacement grid kWh.
    flow = result.plan.flows[-1]
    depleted = replace(flow, batteries=(replace(flow.batteries[0], end_kwh=0.6),))
    metrics = evaluate_consumption(problem, replace(result.plan, flows=(depleted,)))
    assert metrics.terminal_replacement_kwh == pytest.approx(1)


@pytest.mark.planner_scenario
def test_multiple_batteries_do_not_net_out_stock_deficits():
    """Moving one initial kWh to another battery leaves a one-kWh deficit on the first. Surplus cannot cancel it."""
    problem = InstallationProblem(
        (Interval(START, 1, 0, 0),),
        (SolarInverter("inv_a", 4000, 1), SolarInverter("inv_b", 4000, 1)),
        (BatteryState("a", "inv_a", Battery(1, 1)), BatteryState("b", "inv_b", Battery(1, 0))),
        (),
        4000,
        4000,
    )
    result = solve(problem)
    assert result.plan.flows[-1].batteries[0].end_kwh == pytest.approx(1)
    assert result.consumption_optimum_kwh == pytest.approx(0, abs=2e-7)


def test_real_worker_publishes_frozen_strategy_and_separate_kwh_evidence():
    """The real 48-hour worker rejects profitable extra import and archives a proved zero primary objective."""

    async def scenario():
        """Run the bounded native worker through independent validation and wire projection."""
        snapshot = replace(capture(START), strategy=Strategy.CONSUMPTION)
        outcome = await PlanningService(ProcessSolver(), clock=lambda: START).calculate(snapshot)
        assert outcome.reason == "validated", outcome
        document = outcome.document
        assert document["strategy"] == "minimize consumption"
        assert document["input_snapshot"]["strategy"] == "minimize consumption"
        assert document["consumption_objective"]["corrected_import_kwh"] == pytest.approx(
            0, abs=2e-7
        )
        assert document["independent_costs"]["objective"] == pytest.approx(0, abs=2e-7)
        assert len(document["steps"]) == 120

    asyncio.run(scenario())


@pytest.mark.parametrize("reported", [None, float("nan"), -1, 12])
def test_primary_objective_mismatch_blocks_publication(reported):
    """Missing, nonfinite, negative or false primary claims cannot escape independent validation."""

    async def scenario():
        """Feed otherwise feasible cost-optimal flows with untrusted primary evidence."""
        snapshot = replace(capture(START), strategy=Strategy.CONSUMPTION)
        result = HighsSolver().solve(snapshot.problem)

        class Stub:
            """Return a physically valid candidate with deliberately invalid priority evidence."""

            async def solve(self, problem, *, strategy):
                """Preserve detached flows while replacing the claimed primary bound."""
                return replace(result, consumption_optimum_kwh=reported)

        outcome = await PlanningService(Stub(), clock=lambda: START).calculate(snapshot)
        assert outcome.document is None
        assert outcome.reason == "consumption_objective_rejected"

    asyncio.run(scenario())
