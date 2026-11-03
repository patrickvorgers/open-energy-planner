"""Independent EV service, exact final stops, shared limits and financial checks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.appliances.cycles.model import ApplianceWork, CycleProfile
from open_energy_planner.appliances.ev.model import EVWork, refine
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
from open_energy_planner.solar.calculation.model import SolarInverter

START = datetime(2026, 10, 26, 12, tzinfo=UTC)


def work(**changes):
    """Request 0.6 usable kWh in a 60-kWh car, at an explicitly fixed 11-kW wallbox."""
    return replace(
        EVWork(
            "session-a",
            "point-a",
            "car-a",
            START,
            START + timedelta(hours=2),
            60,
            50,
            51,
            1400,
            11000,
            "on_off",
        ),
        **changes,
    )


def problem(works, *, prices=(0.5, 0.1), hours=1, limit=11000):
    """Build an empty AC bus so every imported kWh and charging loss is independently attributable."""
    base = InstallationProblem(
        tuple(
            Interval(START + timedelta(hours=hours * index), hours, price, 0)
            for index, price in enumerate(prices)
        ),
        (),
        (),
        (),
        limit,
        11000,
    )
    return refine(base, works)


def solve(inputs, strategy=Strategy.COST):
    """Require a proved solver result and independently validate all physical/financial evidence."""
    result = HighsSolver().solve(inputs, SolveOptions(strategy=strategy))
    assert result.status == Status.OPTIMAL, result.stop_reason
    validate(inputs, result.plan)
    return result.plan


@pytest.mark.parametrize("selected, expected_power", [(1e-9, 0), (1, 0.000014)])
def test_extract_rounds_only_binary_off_controls(selected, expected_power):
    """Discard a 14-microwatt off-control residue. Preserve the same invalid power on a selected control for validation."""
    from types import SimpleNamespace

    from open_energy_planner.optimization.adapters.highs.ev import extract

    ports = {"on": (selected,), "ac": (1e-9,), "stored": (8e-10,), "power": (0.000014,)}
    context = SimpleNamespace(binding=lambda identity: SimpleNamespace(ports=ports))
    (flow,) = extract(context, SimpleNamespace(ev_work=(work(),)), 0, [])
    assert flow.power_w == expected_power
    assert (flow.ac_kwh, flow.stored_kwh) == ((0, 0) if not expected_power else (1e-9, 8e-10))


@pytest.mark.parametrize("strategy", [Strategy.COST, Strategy.CONSUMPTION])
def test_on_off_exact_target_uses_real_11kw_stop_not_an_impossible_low_setpoint(strategy):
    """0.6 useful kWh needs 2/3 AC kWh at 90%. Charge in the cheaper hour and stop after 218.18 s."""
    inputs = problem((work(),))
    plan = solve(inputs, strategy)
    assert sum(flow.grid_import for flow in plan.flows) == pytest.approx(2 / 3)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(0.6)
    assert plan.costs.cash_cost == pytest.approx(1 / 15)
    active = [
        (step, flow.ev[0])
        for step, flow in zip(inputs.intervals, plan.flows, strict=True)
        if flow.ev[0].power_w > 0
    ]
    assert len(active) == 1
    assert START + timedelta(hours=1) <= active[0][0].start < START + timedelta(hours=2)
    assert active[0][1].power_w == 11000
    assert active[0][0].duration_h * 3600 == pytest.approx(218.181818, abs=1e-6)


def test_impossible_target_maximizes_useful_charge_before_negative_price_preferences():
    """A 4-kW connection limits AC intake to 1 kWh. Interpolated useful energy is 0.84840278 kWh."""
    current = work(
        capacity_kwh=10,
        initial_soc=0,
        target_soc=100,
        control="variable",
        deadline=START + timedelta(minutes=15),
    )
    inputs = problem((current,), prices=(-0.2,), hours=0.25, limit=4000)
    plan = solve(inputs)
    expected = (1840 + (4000 - 2300) * (3485 - 1840) / (4100 - 2300)) / 4000
    assert sum(flow.grid_import for flow in plan.flows) == pytest.approx(1)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(expected)
    assert plan.costs.cash_cost == pytest.approx(-0.2)


def test_continuous_variable_power_has_no_unconfigured_one_watt_floor():
    """A 0.5-W quarter supplies 0.0001 useful kWh at 80%, within a 0.75-W grid limit."""
    current = work(
        capacity_kwh=10,
        initial_soc=0,
        target_soc=0.001,
        minimum_w=0,
        maximum_w=1000,
        control="variable",
        deadline=START + timedelta(minutes=15),
    )
    inputs = problem((current,), prices=(0.2,), hours=0.25, limit=0.75)
    plan = solve(inputs)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(0.0001, abs=1.1e-7)
    assert sum(flow.grid_import for flow in plan.flows) == pytest.approx(0.000125, abs=1.4e-7)


def test_two_charge_points_share_hard_headroom_without_mixing_vehicle_soc():
    """Two cars each need 0.46 useful kWh. Two separate 2.3-kW quarters fit a 3-kW shared grid."""
    first = work(
        capacity_kwh=10,
        initial_soc=0,
        target_soc=4.6,
        maximum_w=2300,
        deadline=START + timedelta(minutes=30),
    )
    second = replace(first, session_id="session-b", point_id="point-b", vehicle_id="car-b")
    inputs = problem((first, second), prices=(0.2, 0.4), hours=0.25, limit=3000)
    plan = solve(inputs)
    for identity in ("session-a", "session-b"):
        assert sum(
            row.stored_kwh for flow in plan.flows for row in flow.ev if row.session_id == identity
        ) == pytest.approx(0.46)
    assert all(sum(row.power_w for row in flow.ev) <= 3000 for flow in plan.flows)
    assert plan.costs.cash_cost == pytest.approx(0.345)


def test_negative_prices_do_not_authorize_charging_beyond_requested_soc():
    """A one-percent target still permits only 0.6 useful kWh, even when all charging earns money."""
    inputs = problem((work(),), prices=(-1, -0.5))
    plan = solve(inputs)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(0.6)
    assert sum(flow.grid_import for flow in plan.flows) == pytest.approx(2 / 3)


def test_hourly_on_off_window_can_finish_after_half_an_hour():
    """Half an hour at 4 kW imports 2 kWh and stores 1.69680556 kWh. A quarter modulo must not erase it."""
    useful = 1.6968055555555556
    current = work(
        capacity_kwh=10,
        initial_soc=0,
        target_soc=useful * 10,
        maximum_w=4000,
        deadline=START + timedelta(hours=1),
    )
    inputs = problem((current,), prices=(0.2,), limit=4000)
    plan = solve(inputs)
    assert sum(flow.grid_import for flow in plan.flows) == pytest.approx(2)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(useful)
    assert sum(
        step.duration_h
        for step, flow in zip(inputs.intervals, plan.flows, strict=True)
        if flow.ev[0].power_w > 0
    ) == pytest.approx(0.5)


def test_short_variable_charge_uses_minimum_power_when_maximum_exceeds_grid_limit():
    """A 1-kW 450-second pulse stores 0.1 kWh at 80%. A forbidden 4-kW pulse must not fabricate shortfall."""
    current = work(
        capacity_kwh=10,
        initial_soc=0,
        target_soc=1,
        minimum_w=1000,
        maximum_w=4000,
        control="variable",
        deadline=START + timedelta(minutes=15),
    )
    inputs = problem((current,), prices=(0.2,), hours=0.25, limit=2000)
    plan = solve(inputs)
    assert sum(flow.grid_import for flow in plan.flows) == pytest.approx(0.125)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(0.1)
    assert {round(flow.ev[0].power_w) for flow in plan.flows} == {0, 1000}


def test_variable_power_does_not_invent_a_whole_watt_energy_lattice():
    """27 kWh fits three hours below 10.8 kW. A continuous setpoint can meet it without a fractional-energy knapsack."""
    current = work(
        initial_soc=35, target_soc=80, control="variable", deadline=START + timedelta(hours=3)
    )
    inputs = problem((current,), prices=(0.2, 0.2, 0.2), limit=10800)
    plan = solve(inputs)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(27)
    assert all(flow.ev[0].power_w <= 10800 + 1e-5 for flow in plan.flows)


@pytest.mark.parametrize("field,value", [("stored_kwh", 1), ("power_w", 100), ("ac_kwh", -1)])
def test_independent_validator_rejects_forged_energy_or_impossible_commands(field, value):
    """Reported energy, minimum power and no-V2G evidence must agree with physical 11-kW charging."""
    inputs = problem((work(),))
    plan = solve(inputs)
    index = next(index for index, flow in enumerate(plan.flows) if flow.ev[0].power_w > 0)
    flows = list(plan.flows)
    flows[index] = replace(flows[index], ev=(replace(flows[index].ev[0], **{field: value}),))
    with pytest.raises(ValueError, match="EV"):
        validate(inputs, replace(plan, flows=tuple(flows)))


def test_earlier_departure_service_precedes_cheaper_later_energy():
    """A 2.3-kW grid supplies 0.46 useful kWh per quarter. Give the earlier car the expensive quarter and serve the later one next."""
    first = work(
        capacity_kwh=10,
        initial_soc=0,
        target_soc=9.2,
        maximum_w=2300,
        deadline=START + timedelta(minutes=15),
    )
    second = replace(
        first,
        session_id="session-b",
        point_id="point-b",
        vehicle_id="car-b",
        target_soc=4.6,
        deadline=START + timedelta(minutes=30),
    )
    inputs = problem((first, second), prices=(0.5, 0.1), hours=0.25, limit=2300)
    plan = solve(inputs)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(0.46)
    assert sum(flow.ev[1].stored_kwh for flow in plan.flows) == pytest.approx(0.46)
    assert all(
        flow.ev[1].ac_kwh == pytest.approx(0)
        for step, flow in zip(inputs.intervals, plan.flows, strict=True)
        if step.start < START + timedelta(minutes=15)
    )
    assert plan.costs.cash_cost == pytest.approx(0.345)


def test_model_construction_consumes_the_same_bounded_time_budget(monkeypatch):
    """A two-second model build exceeds a one-second total allowance. No financial solve may begin afterward."""
    from open_energy_planner.optimization.adapters.highs import solver as solver_module
    from open_energy_planner.optimization.adapters.highs.installation import (
        InstallationModelFactory,
    )

    clock = [0.0]

    class SlowFactory:
        """Advance a deterministic clock after building an otherwise trivial solvable installation."""

        def build(self, inputs):
            """Spend two synthetic seconds in construction, not in the native solve."""
            built = InstallationModelFactory().build(inputs)
            clock[0] = 2
            return built

    monkeypatch.setattr(solver_module, "perf_counter", lambda: clock[0])
    result = HighsSolver(factory=SlowFactory()).solve(problem(()), SolveOptions(time_limit_s=1))
    assert result.status == Status.NO_CANDIDATE
    assert result.stop_reason == "time_limit"
    assert result.plan is None


def test_ev_pv_stationary_battery_and_running_appliance_share_one_bus():
    """1 PV + .25 battery kWh supplies .5 household + .25 appliance + .5 EV AC, storing .4 EV kWh at 80%."""
    car = work(
        target_soc=50 + 0.4 / 60 * 100,
        minimum_w=0,
        maximum_w=2300,
        control="variable",
        deadline=START + timedelta(hours=1),
    )
    cycle = ApplianceWork(
        "cycle",
        "sensor.dishwasher",
        START,
        START + timedelta(hours=2),
        CycleProfile((250, 250, 250, 250)),
        fixed_start=START,
    )
    base = InstallationProblem(
        (Interval(START, 1, 0.2, 0, load_kwh=0.5, pv_kwh=1, pv_sources=(("pv", 1),)),),
        (SolarInverter("hybrid", 4000, 1, True),),
        (
            BatteryState(
                "stationary", "hybrid", Battery(1, 0.5, charge_limit_w=1000, discharge_limit_w=1000)
            ),
        ),
        (PVUnit("pv", "hybrid", False),),
        0,
        0,
        appliances=(cycle,),
    )
    plan = solve(refine(base, (car,)))
    assert sum(flow.grid_import + flow.grid_export for flow in plan.flows) == pytest.approx(
        0, abs=1e-7
    )
    assert sum(flow.ev[0].ac_kwh for flow in plan.flows) == pytest.approx(0.5, abs=1e-6)
    assert sum(flow.ev[0].stored_kwh for flow in plan.flows) == pytest.approx(0.4, abs=1e-6)
    assert plan.flows[-1].batteries[0].end_kwh == pytest.approx(0.25, abs=1e-6)


def test_same_vehicle_cannot_be_connected_to_two_charge_points():
    """Duplicate car identities cannot draw energy twice from independent virtual points."""
    first = work()
    with pytest.raises(ValueError, match="multiply connected"):
        problem((first, replace(first, session_id="other", point_id="other")))
