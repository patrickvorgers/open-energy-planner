"""Thermal trajectory deviations and fixed-service stationary battery references."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.cycles.model import ApplianceWork, CycleProfile
from open_energy_planner.appliances.ev.model import EVWork, refine
from open_energy_planner.home_assistant.observation_wire import parse_configuration, parse_report
from open_energy_planner.home_assistant.observations import PlanningBasis
from open_energy_planner.installation.model import BatteryState, PVUnit
from open_energy_planner.installation.valuation import TerminalValuation
from open_energy_planner.live_inputs import LiveInputs
from open_energy_planner.optimization.domain import Battery
from open_energy_planner.planning import InputSnapshot
from open_energy_planner.savings.context import physical_context
from open_energy_planner.savings.forecast import comparison
from open_energy_planner.savings.scenarios import scenario
from open_energy_planner.solar.calculation.model import SolarInverter
from tests.app.thermal.test_thermal_solver import START, problem, solve, work


def snapshot(inputs):
    """Freeze one original installation without making later forecasts part of its evidence."""
    return InputSnapshot("original", "sources", START, "measurement", "forecast", "prices", inputs)


def original_trajectory():
    """Declare independently known 40-to-50-to-60-C planned quarter temperatures."""
    inputs = problem(work())
    original = snapshot(inputs)
    flows = [
        {
            "batteries": [],
            "grid_import": 0,
            "grid_export": 0,
            "thermal": [{"device_id": "boiler", "end_c": temperature}],
        }
        for temperature in (50, 60, 60, 60)
    ]
    return original, {"expected_flows": flows}


@pytest.mark.parametrize("interval_index", [0, 1])
@pytest.mark.parametrize("difference, expected", [(0, False), (1.99, False), (2, True), (-2, True)])
def test_temperature_deviation_compares_with_original_trajectory_not_original_sample(
    interval_index,
    difference,
    expected,
):
    """Mid-quarter expected temperature is 45 or 55 C. Only a 2-C discrepancy is material."""
    original, document = original_trajectory()
    now = START + timedelta(minutes=15 * interval_index + 7.5)
    current = replace(
        original,
        problem=replace(
            original.problem,
            thermal_work=(
                replace(
                    original.problem.thermal_work[0],
                    initial_c=45 + 10 * interval_index + difference,
                ),
            ),
        ),
    )
    live = LiveInputs(SimpleNamespace(published=(original, document)), None)
    live.report = SimpleNamespace(readings=())
    assert live.deviated(current, now) is expected


def test_material_thermal_deviations_reuse_five_minute_throttle_and_quarter_refresh():
    """Repeated 3-C errors cannot submit before 300 seconds. A quarter boundary remains immediate."""
    inputs = replace(problem(work()), thermal_work=())
    original = snapshot(replace(inputs, thermal_work=(work(),)))
    document = {
        "expected_flows": [
            {
                "batteries": [],
                "grid_import": 0,
                "grid_export": 0,
                "thermal": [{"device_id": "boiler", "end_c": 40}],
            }
            for _step in inputs.intervals
        ]
    }
    submitted = []
    planner = SimpleNamespace(
        published=(original, document), submit=submitted.append, invalidate=lambda reason: None
    )
    repository = SimpleNamespace(invalidate_plan=lambda: None, store_sources=lambda value: None)
    config = parse_configuration(
        {"bindings": [{"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"}]},
        revision="sources",
    )
    future = SimpleNamespace(
        current=lambda configuration, now, report: PlanningBasis(
            "sources",
            "forecast",
            "prices",
            START + timedelta(hours=1),
            inputs,
        )
    )
    clock = [START]
    live = LiveInputs(planner, repository, future, clock=lambda: clock[0])
    live.configure(config)
    live.prepare_snapshot = lambda candidate, now: replace(
        candidate,
        thermal_revision="same",
        problem=replace(
            candidate.problem, thermal_work=(work(len(candidate.problem.intervals), initial_c=43),)
        ),
    )
    for seconds, expected in ((0, 1), (299, 1), (300, 2), (301, 2), (599, 2), (600, 3), (900, 4)):
        clock[0] = START + timedelta(seconds=seconds)
        body = {
            "config_revision": "sources",
            "captured_at": clock[0].isoformat(),
            "issues": [],
            "readings": [
                {
                    "key": "grid.power",
                    "entity_id": "sensor.grid",
                    "value": 0,
                    "unit": "W",
                    "observed_at": clock[0].isoformat(),
                    "reported_at": clock[0].isoformat(),
                    "timestamp_kind": "ha_report",
                }
            ],
        }
        live.receive(parse_report(body, config, clock[0]))
        assert len(submitted) == expected


def joint_plan(*, large_draw=False):
    """Solve 0.4-kWh household, 0.5-kWh heat, 0.125-kWh EV and 0.25-kWh appliance service."""
    current = work(initial_c=35, minimum_c=40, terminal_c=40)
    inputs = problem(current)
    vehicle = EVWork(
        "session", "point", "car", START, START + timedelta(hours=1), 10, 0, 1, 0, 1000, "variable"
    )
    inputs = replace(
        inputs,
        thermal_work=(),
        intervals=tuple(
            replace(step, load_kwh=0.1, pv_sources=(("roof", 0.25),)) for step in inputs.intervals
        ),
        inverters=(SolarInverter("hybrid", 5000, 1, True),),
        batteries=(BatteryState("battery", "hybrid", Battery(2, 1, 0, 4000, 4000)),),
        terminal_valuations=(
            TerminalValuation(
                "battery", 0, 2, 0.2, START, START + timedelta(hours=24), "trailing_window_proxy"
            ),
        ),
        pv_sources=(PVUnit("roof", "hybrid", False),),
        appliances=(
            ApplianceWork(
                "dishwasher",
                "sensor.dishwasher",
                START,
                START + timedelta(hours=1),
                CycleProfile((1000,)),
                fixed_start=START + timedelta(minutes=30),
            ),
        ),
    )
    inputs = refine(inputs, (vehicle,))
    inputs = replace(
        inputs,
        thermal_work=(
            replace(
                current,
                demand_kwh=tuple(
                    100 * step.duration_h
                    if large_draw and step.start < START + timedelta(minutes=15)
                    else 0
                    for step in inputs.intervals
                ),
            ),
        ),
    )
    solved = solve(inputs)
    return json.loads(
        json.dumps(
            {
                "input_snapshot": {"problem": asdict(inputs)},
                "expected_flows": [asdict(flow) for flow in solved.flows],
                "independent_costs": asdict(solved.costs),
                "appliance_schedule": [
                    {"task_id": task, "start": start} for task, start in solved.appliance_starts
                ],
            },
            default=lambda value: value.isoformat(),
        )
    )


@pytest.mark.parametrize("immediate_appliances", [False, True])
def test_physical_reference_adds_joint_service_energy_exactly_once(immediate_appliances):
    """Total AC load is 1.275 kWh. Moving the dishwasher changes timing, never its cycle energy."""
    plan = joint_plan()
    frozen = deepcopy(plan)
    physics = physical_context(plan, immediate_appliances=immediate_appliances)
    assert sum(step.load_kwh for step in physics.intervals) == pytest.approx(
        0.4 + 0.5 + 0.125 + 0.25
    )
    assert physics.ev_work == ()
    assert physics.thermal_work == ()
    assert physics.appliances == ()
    assert plan == frozen


@pytest.mark.parametrize("without_battery", [False, True])
def test_reference_replay_conserves_joint_thermal_ev_appliance_energy(without_battery):
    """Reference bars retain fixed EV/heat service without adding their consumption a second time."""
    plan = joint_plan()
    frozen = deepcopy(plan)
    reference = comparison(plan, without_battery=without_battery)
    projected = scenario(plan, reference, without_battery=without_battery)
    chart = projected["chart"]
    total_load = sum(step["load_kwh"] for step in reference["intervals"])
    assert total_load == pytest.approx(1.275)
    for step, flow in zip(reference["intervals"], chart["expected_flows"], strict=True):
        imported = flow["grid_import"]
        exported = flow["grid_export"]
        converter_supply = sum(row["export_kwh"] - row["import_kwh"] for row in flow["inverters"])
        assert imported + converter_supply - exported == pytest.approx(step["load_kwh"])
    assert sum(
        row["ac_kwh"] for flow in chart["expected_flows"] for row in flow["thermal"]
    ) == pytest.approx(0.5)
    assert sum(
        row["ac_kwh"] for flow in chart["expected_flows"] for row in flow["ev"]
    ) == pytest.approx(0.125)
    assert sum(step.load_kwh for step in physical_context(chart).intervals) == pytest.approx(1.275)
    assert plan == frozen


@pytest.mark.parametrize("without_battery", [False, True])
def test_reference_keeps_original_unmet_draw_without_billing_it_as_consumed_energy(without_battery):
    """Unserved thermal demand remains original service evidence. Only actual heater AC enters cash costs."""
    plan = joint_plan(large_draw=True)
    frozen = deepcopy(plan)
    original_unmet = [flow["thermal"][0]["unmet_draw_kwh"] for flow in plan["expected_flows"]]
    assert sum(original_unmet) == pytest.approx(25 - 35 * 0.1 - 0.5, abs=2e-7)
    reference = comparison(plan, without_battery=without_battery)
    chart = scenario(plan, reference, without_battery=without_battery)["chart"]
    assert [
        flow["thermal"][0]["unmet_draw_kwh"] for flow in chart["expected_flows"]
    ] == original_unmet
    heater_ac = sum(flow["thermal"][0]["ac_kwh"] for flow in plan["expected_flows"])
    assert sum(step.load_kwh for step in physical_context(chart).intervals) == pytest.approx(
        0.4 + 0.125 + 0.25 + heater_ac,
    )
    purchase = sum(
        step["buy_price"] * flow["import_kwh"]
        for step, flow in zip(reference["intervals"], reference["reference"], strict=True)
    )
    sale = sum(
        step["sell_price"] * flow["export_kwh"]
        for step, flow in zip(reference["intervals"], reference["reference"], strict=True)
    )
    assert reference["reference_costs"]["purchase"] == pytest.approx(purchase)
    assert reference["reference_costs"]["sale"] == pytest.approx(sale)
    assert reference["ems_costs"]["purchase"] == plan["independent_costs"]["import_cost"]
    assert plan == frozen
