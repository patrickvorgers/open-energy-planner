"""Independent heat arithmetic and joint electrical planning regressions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import exp

import pytest

from open_energy_planner.appliances.cycles.model import ApplianceWork, CycleProfile
from open_energy_planner.appliances.ev.model import EVWork, refine
from open_energy_planner.appliances.thermal.model import ThermalWork
from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import (
    Battery,
    Interval,
    SolveOptions,
    Status,
    Strategy,
)
from open_energy_planner.optimization.export_bonus import ExportBonus
from open_energy_planner.solar.calculation.model import SolarInverter

START = datetime(2026, 10, 26, 12, tzinfo=UTC)


def work(count=4, **changes):
    """Declare a 0.1-kWh/K store with a fixed 2-kW resistive heater."""
    return replace(
        ThermalWork("boiler", START, 40, 40, 80, 20, 0.1, 0, 2000, 1, (0,) * count, 40),
        **changes,
    )


def problem(current, *, prices=None, limit=6000):
    """Keep each test quarter independent of service input construction."""
    prices = prices or (0.3,) * len(current.demand_kwh)
    return InstallationProblem(
        tuple(
            Interval(START + timedelta(minutes=15 * index), 0.25, price, 0)
            for index, price in enumerate(prices)
        ),
        (),
        (),
        (),
        limit,
        6000,
        thermal_work=(current,),
    )


def solve(inputs, strategy=Strategy.COST):
    """Demand a proven native solution and independently check every balance."""
    result = HighsSolver().solve(inputs, SolveOptions(strategy=strategy))
    assert result.status == Status.OPTIMAL, result.stop_reason
    validate(inputs, result.plan)
    return result.plan


def test_exponential_cooling_and_constant_rate_draw_use_independent_heat_equation():
    """With no electrical headroom, exact cooling and 0.2-kWh draw act throughout each quarter."""
    current = work(initial_c=60, minimum_c=0, terminal_c=0, loss_w_per_c=8, demand_kwh=(0.2,) * 4)
    plan = solve(problem(current, limit=0))
    previous = 60
    decay = exp(-0.008 * 0.25 / 0.1)
    draw_factor = (1 - decay) / (0.008 * 0.25 / 0.1)
    for flow in plan.flows:
        previous = 20 + (previous - 20) * decay - 0.2 * draw_factor / 0.1
        assert flow.thermal[0].end_c == pytest.approx(previous)
        assert flow.thermal[0].ac_kwh == 0


def test_no_hot_water_draw_does_not_invent_thermal_consumption():
    """Without heat loss or draw, all four temperatures retain the measured 40 C."""
    plan = solve(problem(work()))
    assert [flow.thermal[0].end_c for flow in plan.flows] == pytest.approx([40] * 4)
    assert sum(flow.thermal[0].ac_kwh for flow in plan.flows) == 0


def test_missing_interval_draw_is_rejected_instead_of_filled_with_zero():
    """All four intervals require explicit thermal demand evidence, including real zero demand."""
    inputs = problem(work())
    with pytest.raises(ValueError):
        replace(inputs, thermal_work=(work(demand_kwh=(0, 0, 0)),))


def test_negative_prices_never_relax_hard_temperature_ceiling():
    """One complete quarter adds 5 C. A second would exceed 46 C despite earning money."""
    plan = solve(problem(work(maximum_c=46), prices=(-1,) * 4))
    assert sum(flow.thermal[0].ac_kwh for flow in plan.flows) == pytest.approx(0.5)
    assert all(flow.thermal[0].end_c <= 46 for flow in plan.flows)


def test_unreachable_comfort_is_explicit_and_maximum_useful_heat_is_planned():
    """A 2-kW quarter raises 30 to 35 C. The remaining 5-C deficit is 0.5 thermal kWh."""
    current = work(1, initial_c=30, minimum_c=40, terminal_c=40)
    plan = solve(problem(current, prices=(100,)))
    assert plan.flows[0].thermal[0].power_w == 2000
    assert plan.flows[0].thermal[0].end_c == pytest.approx(35)
    assert plan.flows[0].thermal[0].shortfall_kwh == pytest.approx(0.5)


@pytest.mark.parametrize("strategy", [Strategy.COST, Strategy.CONSUMPTION])
def test_terminal_stock_restoration_prevents_borrowing_tomorrows_heat(strategy):
    """A 0.5-kWh draw must be replenished even though comfort permits a 5-C temperature drop."""
    current = work(initial_c=50, minimum_c=40, terminal_c=50, demand_kwh=(0.5, 0, 0, 0))
    plan = solve(problem(current, prices=(0.4, 0.1, 0.3, 0.2)), strategy)
    assert sum(flow.thermal[0].ac_kwh for flow in plan.flows) == pytest.approx(0.5)
    assert plan.flows[-1].thermal[0].end_c >= 50 - 1e-6


def test_bonus_billing_split_keeps_one_physical_heater_command_per_quarter():
    """Opposite prices at minute 7 cannot create a seven-minute 2-kW pulse."""
    current = work(2, initial_c=40, minimum_c=40, maximum_c=43, terminal_c=40)
    inputs = replace(
        problem(current),
        intervals=(
            Interval(START, 7 / 60, -1, 0),
            Interval(START + timedelta(minutes=7), 8 / 60, 1, 0),
        ),
        export_bonuses=(ExportBonus("bonus", (0.1, 0.2), (2, 2), ("year", "year"), 0),),
    )
    plan = solve(inputs)
    assert [flow.thermal[0].power_w for flow in plan.flows] == [0, 0]
    assert [flow.thermal[0].end_c for flow in plan.flows] == pytest.approx([40, 40])


def test_before_observation_time_retains_state_without_backdated_heat_or_losses():
    """The first seven minutes predate the observation boundary. Only the final eight minutes cool."""
    current = work(
        2,
        ready_at=START + timedelta(minutes=7),
        state_at=START + timedelta(minutes=7),
        initial_c=60,
        minimum_c=0,
        terminal_c=0,
        loss_w_per_c=10,
    )
    inputs = replace(
        problem(current, limit=0),
        intervals=(
            Interval(START, 7 / 60, 0.2, 0),
            Interval(START + timedelta(minutes=7), 8 / 60, 0.2, 0),
        ),
    )
    plan = solve(inputs)
    assert plan.flows[0].thermal[0].end_c == 60
    assert plan.flows[0].thermal[0].power_w == 0
    assert plan.flows[1].thermal[0].end_c == pytest.approx(20 + 40 * exp(-0.01 * (8 / 60) / 0.1))


@pytest.mark.parametrize(
    "field,value",
    [
        ("device_id", "other"),
        ("ac_kwh", 0.1),
        ("end_c", 70),
        ("shortfall_kwh", 1),
        ("power_w", 500),
    ],
)
def test_independent_validator_rejects_forged_thermal_evidence(field, value):
    """Neither fabricated identities nor a fractional on/off command may become a published plan."""
    inputs = problem(work())
    plan = solve(inputs)
    first = replace(plan.flows[0], thermal=(replace(plan.flows[0].thermal[0], **{field: value}),))
    with pytest.raises(ValueError, match="[Tt]hermal"):
        validate(inputs, replace(plan, flows=(first, *plan.flows[1:])))


def test_boiler_ev_appliance_pv_and_battery_share_one_hard_grid_boundary():
    """A boiler, 0.1-kWh EV target and dishwasher share PV and battery without exceeding 2 kW import."""
    current = work(initial_c=35, minimum_c=40, terminal_c=40)
    inputs = problem(current, limit=2000)
    inputs = replace(
        inputs,
        intervals=tuple(
            replace(step, load_kwh=0.1, pv_sources=(("roof", 0.25),)) for step in inputs.intervals
        ),
        inverters=(SolarInverter("hybrid", 5000, 1, True),),
        batteries=(BatteryState("battery", "hybrid", Battery(2, 1, 0, 2000, 2000)),),
        pv_sources=(PVUnit("roof", "hybrid", False),),
        ev_work=(
            EVWork(
                "session",
                "point",
                "car",
                START,
                START + timedelta(hours=1),
                10,
                0,
                1,
                0,
                1000,
                "variable",
            ),
        ),
        appliances=(
            ApplianceWork(
                "task",
                "sensor.dishwasher",
                START,
                START + timedelta(hours=1),
                CycleProfile((1000,)),
            ),
        ),
    )
    inputs = refine(replace(inputs, thermal_work=()), inputs.ev_work)
    inputs = replace(
        inputs, thermal_work=(replace(current, demand_kwh=(0,) * len(inputs.intervals)),)
    )
    plan = solve(inputs)
    assert plan.flows[-1].thermal[0].end_c >= 40 - 1e-6
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(0.1, abs=1e-6)
    assert len(plan.appliance_starts) == 1
    assert sum(flow.solar[0].used_kwh for flow in plan.flows) == pytest.approx(1)
    for step, flow in zip(inputs.intervals, plan.flows, strict=True):
        assert flow.grid_import <= 2 * step.duration_h + 1e-6
        assert flow.thermal[0].power_w in (0, 2000)


@pytest.mark.parametrize("grid_limit, expected_heat", [(0, 0), (2000, 0.5)])
def test_impossible_heat_draw_remains_explicit_without_blocking_a_valid_candidate(
    grid_limit, expected_heat
):
    """A 25-kWh quarter can serve only 4-kWh initial heat plus 0 or 0.5 electrical kWh."""
    current = work(1, demand_kwh=(25,))
    inputs = problem(current, limit=grid_limit)
    plan = solve(inputs)
    heating = plan.flows[0].thermal[0]
    assert heating.ac_kwh == pytest.approx(expected_heat)
    assert heating.unmet_draw_kwh == pytest.approx(25 - 40 * 0.1 - expected_heat, abs=2e-7)
    assert heating.end_c == pytest.approx(0, abs=2e-6)
    assert heating.shortfall_kwh == pytest.approx(4, abs=2e-7)
    assert plan.flows[0].grid_import == pytest.approx(expected_heat)


@pytest.mark.parametrize("forged_unmet", [-0.1, 26, 1, float("nan"), True])
def test_independent_validator_rejects_forged_unmet_draw(forged_unmet):
    """Unmet demand is bounded physical evidence, not permission to invent served heat."""
    inputs = problem(work(1, demand_kwh=(25,)), limit=2000)
    plan = solve(inputs)
    forged = replace(plan.flows[0].thermal[0], unmet_draw_kwh=forged_unmet)
    with pytest.raises(ValueError, match="Thermal"):
        validate(inputs, replace(plan, flows=(replace(plan.flows[0], thermal=(forged,)),)))


def test_unreachable_thermal_draw_keeps_joint_pv_battery_ev_plan_feasible():
    """PV powers two 0.5-kWh heat quarters. Battery supplies the car despite 20.5-kWh unmet draw."""
    current = work(2, demand_kwh=(25, 0))
    inputs = replace(
        problem(current, limit=0),
        thermal_work=(),
        intervals=tuple(
            replace(step, pv_sources=(("roof", 0.5),)) for step in problem(current).intervals
        ),
        inverters=(SolarInverter("hybrid", 5000, 1, True),),
        batteries=(BatteryState("battery", "hybrid", Battery(2, 1, 0, 4000, 4000)),),
        pv_sources=(PVUnit("roof", "hybrid", False),),
    )
    vehicle = EVWork(
        "session",
        "point",
        "car",
        START,
        START + timedelta(minutes=30),
        10,
        0,
        1,
        0,
        1000,
        "variable",
    )
    inputs = refine(inputs, (vehicle,))
    inputs = replace(
        inputs,
        thermal_work=(
            replace(
                current,
                demand_kwh=tuple(
                    100 * step.duration_h if step.start < START + timedelta(minutes=15) else 0
                    for step in inputs.intervals
                ),
            ),
        ),
    )
    plan = solve(inputs)
    assert sum(flow.thermal[0].unmet_draw_kwh for flow in plan.flows) == pytest.approx(
        20.5, abs=2e-7
    )
    assert sum(flow.thermal[0].ac_kwh for flow in plan.flows) == pytest.approx(1)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(0.1, abs=2e-7)
    assert sum(flow.batteries[0].discharge_kwh for flow in plan.flows) >= 0.125 - 2e-7
    assert all(flow.grid_import == pytest.approx(0) for flow in plan.flows)


@pytest.mark.parametrize("initial_temperature", [0, 40])
def test_passive_heat_bounds_preserve_real_exhaustion_and_repeated_unmet_draw(initial_temperature):
    """An unheated store supplies its actual initial heat, then keeps every later 4-kWh draw explicit."""
    current = work(2, initial_c=initial_temperature, demand_kwh=(4, 4))
    plan = solve(problem(current, limit=0))
    assert [flow.thermal[0].end_c for flow in plan.flows] == pytest.approx([0, 0], abs=2e-6)
    assert [flow.thermal[0].unmet_draw_kwh for flow in plan.flows] == pytest.approx(
        [4 - initial_temperature * 0.1, 4],
        abs=2e-7,
    )


def test_zero_heat_lower_envelope_never_removes_a_feasible_heated_schedule():
    """Cooling plus draw stays positive without heating, while expensive later demand can still be served by earlier heating."""
    current = work(
        4,
        initial_c=55,
        minimum_c=45,
        terminal_c=55,
        capacity_kwh_per_c=0.2,
        loss_w_per_c=1,
        demand_kwh=(0.2,) * 4,
    )
    inputs = problem(current, prices=(0.01, 0.5, 0.5, 0.5))
    passive = 55
    decay = exp(-0.001 * 0.25 / 0.2)
    factor = (1 - decay) / (0.001 * 0.25 / 0.2)
    for _step in inputs.intervals:
        passive = 20 + (passive - 20) * decay - 0.2 * factor / 0.2
        assert passive > 0
    plan = solve(inputs)
    assert [flow.thermal[0].unmet_draw_kwh for flow in plan.flows] == [0] * 4
    assert plan.flows[-1].thermal[0].end_c >= 55 - 1e-6
    assert plan.flows[0].thermal[0].ac_kwh == pytest.approx(0.5)
