"""Independent reserve shortfall oracles, hard physical bounds and unchanged reference policy."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.installation.validation import validate
from open_energy_planner.installation.valuation import TerminalValuation
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Battery, Interval, Status
from open_energy_planner.savings.reference import dispatch
from open_energy_planner.solar.calculation.model import SolarInverter

START = datetime(2026, 10, 23, tzinfo=UTC)


def problem(*, mandatory=True, power=4000, count=2, grid=4000):
    """Use lossless quarter-hours so 4000 W restores exactly one internal kWh per interval."""
    battery = Battery(2, 0, minimum_kwh=1, charge_limit_w=power, recover_reserve=mandatory)
    return InstallationProblem(
        tuple(
            Interval(START + timedelta(minutes=15 * index), 0.25, 10 if index == 0 else 0, 0)
            for index in range(count)
        ),
        (SolarInverter("inv", 4000, 1),),
        (BatteryState("battery", "inv", battery),),
        (),
        grid,
        4000,
    )


def solve(value):
    """Require a proved optimum and recompute physical and financial acceptance independently."""
    result = HighsSolver().solve(value)
    assert result.status == Status.OPTIMAL, result
    validate(value, result.plan)
    return result.plan


@pytest.mark.planner_scenario
@pytest.mark.parametrize("mandatory", [False, True])
def test_reserve_recovery_precedes_prices_only_when_enabled(mandatory):
    """One missing kWh costs 10 now and zero later: mandatory restores now. Optional may stay below without discharging."""
    plan = solve(problem(mandatory=mandatory))
    assert plan.flows[0].batteries[0].charge_kwh == pytest.approx(1 if mandatory else 0)
    assert plan.costs.cash_cost == pytest.approx(10 if mandatory else 0)
    assert all(flow_item.batteries[0].discharge_kwh == 0 for flow_item in plan.flows)


@pytest.mark.planner_scenario
def test_reserve_restores_across_several_power_limited_steps():
    """1000 W for four quarters restores one kWh, giving independent end stocks 0.25/0.5/0.75/1."""
    plan = solve(problem(power=1000, count=4))
    assert [flow_item.batteries[0].end_kwh for flow_item in plan.flows] == pytest.approx(
        [0.25, 0.5, 0.75, 1]
    )


@pytest.mark.planner_scenario
def test_no_recovery_capacity_remains_usable_and_has_no_inventory_value():
    """A blocked grid and absent PV cannot restore reserve. Keep zero real energy and zero accessible terminal value."""
    value = replace(
        problem(grid=0),
        terminal_valuations=(
            TerminalValuation(
                device_id="battery",
                reserve_kwh=1,
                usable_kwh=1,
                rate=1,
                window_start=START,
                window_end=START + timedelta(hours=24),
                price_basis="trailing_window_proxy",
            ),
        ),
    )
    plan = solve(value)
    assert all(flow_item.batteries[0].end_kwh == 0 for flow_item in plan.flows)
    assert plan.costs.terminal_value == 0


@pytest.mark.planner_scenario
def test_two_batteries_share_recovery_capacity_before_money():
    """Two 0.5 kWh deficits share 1000 W: total deficits must be 0.75/0.5/0.25/0, irrespective of allocation ties."""
    base = problem(power=1000, count=4, grid=1000)
    first = replace(base.batteries[0], battery=replace(base.batteries[0].battery, minimum_kwh=0.5))
    second = replace(first, device_id="other")
    value = replace(base, batteries=(first, second))
    plan = solve(value)
    assert [
        sum(max(0, 0.5 - battery_item.end_kwh) for battery_item in flow_item.batteries)
        for flow_item in plan.flows
    ] == pytest.approx([0.75, 0.5, 0.25, 0])


@pytest.mark.planner_scenario
def test_hybrid_dc_recovery_respects_both_losses_without_grid_energy():
    """Each 1 kWh PV interval stores 0.9 DC-path times 0.8 cell efficiency, giving 0.72 then 1.44 kWh."""
    base = problem(grid=0)
    unit = replace(
        base.batteries[0],
        dc_efficiency=0.9,
        battery=replace(base.batteries[0].battery, charge_efficiency=0.8),
    )
    value = replace(
        base,
        batteries=(unit,),
        inverters=(SolarInverter("inv", 4000, 1, hybrid=True),),
        pv_sources=(PVUnit("roof", "inv", False),),
        grid_export_limit_w=0,
        intervals=tuple(
            replace(interval, pv_kwh=1, pv_sources=(("roof", 1),)) for interval in base.intervals
        ),
    )
    plan = solve(value)
    assert [flow_item.batteries[0].end_kwh for flow_item in plan.flows] == pytest.approx(
        [0.72, 1.44]
    )
    assert all(flow_item.grid_import == 0 for flow_item in plan.flows)


def test_reference_still_uses_self_consumption_without_mandatory_grid_charging():
    """EMS recovery permission does not rewrite the continuous non-EMS reference into an active grid charger."""
    value = problem()
    step = replace(value.intervals[0], load_kwh=0.1)
    flow = dispatch(value, step, {"battery": 0})
    assert flow["import_kwh"] == 0.1
    assert flow["stored"] == {"battery": 0}


def test_forged_discharge_below_reserve_is_rejected():
    """Reported stock below the measured starting energy fails the reserve check even when the solver reported optimal."""
    value = problem()
    unit = replace(value.batteries[0], battery=replace(value.batteries[0].battery, initial_kwh=0.5))
    value = replace(value, batteries=(unit,))
    plan = solve(value)
    flow = plan.flows[0]
    forged = replace(flow.batteries[0], end_kwh=0.4)
    with pytest.raises(ValueError, match="Battery reserve"):
        validate(value, replace(plan, flows=(replace(flow, batteries=(forged,)), *plan.flows[1:])))


@pytest.mark.planner_scenario
def test_household_load_leaves_only_actual_grid_headroom_for_recovery():
    """A 1 kW connection supplies 0.125 kWh household load per quarter, leaving only 0.125 kWh for storage."""
    base = problem(grid=1000, count=2)
    value = replace(
        base, intervals=tuple(replace(interval, load_kwh=0.125) for interval in base.intervals)
    )
    plan = solve(value)
    assert [flow_item.batteries[0].end_kwh for flow_item in plan.flows] == pytest.approx(
        [0.125, 0.25]
    )
    assert [flow_item.grid_import for flow_item in plan.flows] == pytest.approx([0.25, 0.25])


@pytest.mark.planner_scenario
def test_financial_dispatch_resumes_above_restored_reserve():
    """A -1 price fills 2 kWh. The next +10 export sells only the one kWh above the restored 1 kWh reserve."""
    base = problem(power=8000)
    value = replace(
        base,
        intervals=(
            replace(base.intervals[0], buy_price=-1),
            replace(base.intervals[1], buy_price=10, sell_price=10),
        ),
    )
    # Converter/grid allow one kWh per quarter, so use a longer first interval for the two-kWh fill.
    value = replace(
        value,
        intervals=(
            replace(value.intervals[0], duration_h=0.5),
            replace(value.intervals[1], start=START + timedelta(minutes=30)),
        ),
    )
    plan = solve(value)
    assert [flow_item.batteries[0].end_kwh for flow_item in plan.flows] == pytest.approx([2, 1])
    assert plan.flows[1].grid_export == pytest.approx(1)
    assert plan.costs.cash_cost == pytest.approx(-12)


def test_blocked_recovery_covers_the_full_mixed_resolution_horizon():
    """With zero connection headroom, all 120 deficit optima remain one kWh. Planning still completes within its solve budget."""
    base = problem(grid=0)
    intervals = tuple(
        Interval(START + timedelta(minutes=15 * index), 0.25, 1, 0) for index in range(96)
    )
    intervals += tuple(
        Interval(START + timedelta(hours=24 + index), 1, 1, 0) for index in range(24)
    )
    value = replace(base, intervals=intervals)
    result = HighsSolver().solve(value)
    assert result.status == Status.OPTIMAL, result
    validate(value, result.plan)
    assert len(result.plan.flows) == 120
    assert all(flow_item.batteries[0].end_kwh == 0 for flow_item in result.plan.flows)
    assert result.solve_seconds < 10


@pytest.mark.parametrize(
    "termination,reason", [("kTimeLimit", "time_limit"), ("kIterationLimit", "unproven")]
)
def test_unproven_priority_cannot_publish_a_financial_candidate(monkeypatch, termination, reason):
    """A time-limited reserve stage cannot claim its feasible but unproved profile has priority over money."""
    import highspy

    from open_energy_planner.optimization.adapters.highs import recovery

    monkeypatch.setattr(
        recovery.highspy.Highs,
        "getModelStatus",
        lambda self: getattr(highspy.HighsModelStatus, termination),
    )
    result = HighsSolver().solve(problem())
    assert result.status == Status.NO_CANDIDATE
    assert result.plan is None
    assert result.stop_reason == "reserve_recovery_" + reason


def test_impossible_household_balance_is_infeasible_not_merely_unrestored():
    """With no grid/PV and a below-reserve battery, positive load has no physical supply and must remain infeasible."""
    base = problem(grid=0)
    value = replace(
        base, intervals=tuple(replace(interval, load_kwh=0.1) for interval in base.intervals)
    )
    result = HighsSolver().solve(value)
    assert result.status == Status.INFEASIBLE
    assert result.plan is None


def test_expired_recovery_budget_restores_original_objective():
    """An exhausted shared deadline exits without native optimization and restores the original financial objective."""
    from time import perf_counter

    from open_energy_planner.optimization.adapters.highs.installation import (
        InstallationModelFactory,
    )
    from open_energy_planner.optimization.adapters.highs.recovery import prioritize_reserve

    value = problem()
    model = InstallationModelFactory().build(value)
    before, sense = model.context.native.getObjective()
    original_gap = model.context.native.getOptionValue("mip_abs_gap")
    assert prioritize_reserve(model, value, perf_counter() - 1) == "reserve_recovery_time_limit"
    after, after_sense = model.context.native.getObjective()
    assert str(after) == str(before)
    assert after_sense == sense
    assert model.context.native.getOptionValue("mip_abs_gap") == original_gap


def test_proved_recovery_seeds_a_feasible_financial_incumbent(monkeypatch):
    """The final priority row must retain the known 1 kWh recovered stock as a column-only seed for the money solve."""
    from time import perf_counter

    import highspy

    from open_energy_planner.optimization.adapters.highs.installation import (
        InstallationModelFactory,
    )
    from open_energy_planner.optimization.adapters.highs.recovery import prioritize_reserve

    model = InstallationModelFactory().build(problem())
    seeds = []
    original = highspy.Highs.setSolution

    def seed(native, solution):
        """Observe the real native seed without replacing its feasibility assessment."""
        seeds.append((list(solution.col_value), list(solution.row_value), solution.value_valid))
        return original(native, solution)

    monkeypatch.setattr(highspy.Highs, "setSolution", seed)
    assert prioritize_reserve(model, problem(), perf_counter() + 10) is None
    assert len(seeds) == 1 and seeds[0][1] == [] and seeds[0][2] is True
    first = model.context.binding("bat:battery").variables["energy"][0]
    assert seeds[0][0][int(first)] == pytest.approx(1)
    assert model.context.native.run() == highspy.HighsStatus.kOk
    assert model.context.native.getSolution().value_valid


def test_later_stage_budget_does_not_add_cumulative_native_runtime():
    """After two native seconds, five wall seconds remaining grants five to the new MIP run, never seven."""
    from unittest.mock import Mock

    import highspy

    from open_energy_planner.optimization.adapters.highs.recovery import set_time_budget

    native = Mock()
    native.getRunTime.return_value = 2
    native.setOptionValue.return_value = highspy.HighsStatus.kOk
    set_time_budget(native, 5)
    native.setOptionValue.assert_called_once_with("time_limit", 5)


def test_financial_gap_does_not_relax_reserve_priority(monkeypatch):
    """Keep zero relative gap through recovery. Apply 0.5 percent only to the final financial run."""
    import highspy

    observed_gaps = []
    original_run = highspy.Highs.run

    def observe_run(native):
        """Capture effective precision at each real native optimization stage."""
        observed_gaps.append(native.getOptionValue("mip_rel_gap")[1])
        return original_run(native)

    monkeypatch.setattr(highspy.Highs, "run", observe_run)
    result = HighsSolver().solve(problem())
    assert result.status == Status.OPTIMAL
    assert len(observed_gaps) >= 2
    assert observed_gaps[:-1] == [0] * (len(observed_gaps) - 1)
    assert observed_gaps[-1] == 0.005


def test_below_reserve_fixed_commands_cannot_be_replaced_by_grid_following():
    """The first solar-powered recovery interval must keep its explicit -4000 W charge even with zero grid exchange."""
    from open_energy_planner.installation.commands import plan_document
    from open_energy_planner.synthetic import capture

    base = problem(grid=0)
    value = replace(
        base,
        batteries=(replace(base.batteries[0], can_follow_grid=True),),
        inverters=(SolarInverter("inv", 4000, 1, True),),
        pv_sources=(PVUnit("roof", "inv", False),),
        intervals=tuple(
            replace(interval, pv_kwh=1, pv_sources=(("roof", 1),)) for interval in base.intervals
        ),
    )
    result = HighsSolver().solve(value)
    validate(value, result.plan)
    snapshot = replace(capture(START), problem=value)
    document = plan_document(snapshot, result, result.plan.costs, START)
    assert document["steps"][0]["devices"]["battery"] == {
        "kind": "battery",
        "mode": "fixed_power",
        "power_w": -4000,
    }


@pytest.mark.parametrize("mandatory", [False, True])
def test_measured_below_reserve_catalogue_and_default_hash_are_exact(mandatory):
    """Status follows real stored energy, optional/mandatory policy survives serialization, and false adds no old hash field."""
    import json
    from types import SimpleNamespace

    from open_energy_planner.installation.api import catalogue
    from open_energy_planner.installation.model import Installation, InstalledBattery
    from open_energy_planner.installation.storage import document, parse
    from open_energy_planner.synthetic import capture

    installed = InstalledBattery(
        "battery",
        "inv",
        "sensor.soc",
        "sensor.power",
        2,
        4000,
        4000,
        minimum_soc=50,
        recover_reserve=mandatory,
    )
    definition = Installation(
        "sensor.grid", 4000, 4000, (SolarInverter("inv", 4000, 1),), (installed,)
    )
    assert parse(json.loads(json.dumps(document(definition)))) == definition
    assert ("recover_reserve" in document(definition)["batteries"][0]) is mandatory
    assert installed.state(25).initial_kwh == 0.5
    store, solar = SimpleNamespace(current=definition), SimpleNamespace(configuration=None)
    assert catalogue(store, solar)[0]["reserve_status"] == "measurement_unavailable"
    below = replace(
        capture(datetime.now(UTC)),
        problem=replace(
            problem(mandatory=mandatory),
            batteries=(BatteryState("battery", "inv", installed.state(25)),),
        ),
    )
    assert catalogue(store, solar, below)[0]["reserve_status"] == (
        "recovery_required" if mandatory else "below_reserve"
    )
    full = replace(
        below,
        problem=replace(
            below.problem, batteries=(BatteryState("battery", "inv", installed.state(75)),)
        ),
    )
    assert catalogue(store, solar, full)[0]["reserve_status"] == "reserve_satisfied"
    for offset in (-5, 5):
        invalid = replace(below, measured_at=datetime.now(UTC) + timedelta(minutes=offset))
        assert catalogue(store, solar, invalid)[0]["reserve_status"] == "measurement_unavailable"
