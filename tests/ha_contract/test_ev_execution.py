"""Immutable EV evidence, bounded native execution and comparable stationary-battery references."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.open_energy_planner.protocol import Plan
from open_energy_planner.demand.calculation.composition import LoadComponent, LoadStep
from open_energy_planner.installation.commands import execution_document, plan_document
from open_energy_planner.installation.validation import validate
from open_energy_planner.live_inputs import LiveInputs
from open_energy_planner.optimization.adapters.process.worker import ProcessSolver
from open_energy_planner.optimization.domain import Status
from open_energy_planner.savings.forecast import comparison
from open_energy_planner.savings.scenarios import scenario
from open_energy_planner.synthetic import capture
from tests.app.ev.test_ev_solver import START, problem, solve, work


def ev_snapshot(inputs, revision):
    """Own unchanged fixture household load and zero spontaneous EV use before scheduled charging.

    These scenarios specify household kWh in the input intervals. Each charger
    has zero expected spontaneous use, so its concrete schedule replaces no
    forecast energy. Bind every measured source to its requested point, vehicle
    and session without deriving ownership or energy from solved flows.
    """
    household = tuple(
        LoadStep(interval.start, interval.duration_h, interval.load_kwh)
        for interval in inputs.intervals
    )
    empty = tuple(LoadStep(interval.start, interval.duration_h, 0) for interval in inputs.intervals)
    evidence = tuple(
        {
            "work": asdict(request),
            "energy_entity": f"sensor.{request.point_id.replace('-', '_')}_energy",
        }
        for request in inputs.ev_work
    )
    return replace(
        capture(START),
        problem=inputs,
        ev_revision=revision,
        ev_evidence=evidence,
        load_components=(LoadComponent(None, "fixture-household", household, household),)
        + tuple(
            LoadComponent(source["energy_entity"], "fixture-ev", empty, empty)
            for source in evidence
        ),
    )


@pytest.mark.parametrize("difference, expected", [(0, False), (4.99, False), (5.01, True)])
def test_vehicle_deviation_uses_planned_charge_progress(difference, expected):
    """Normal half-pulse SoC progress is not an error. More than five percentage points requests a throttled round."""
    inputs = problem((work(),))
    plan = solve(inputs)
    index = next(index for index, flow in enumerate(plan.flows) if flow.ev[0].stored_kwh > 0)
    step = inputs.intervals[index]
    now = step.start + timedelta(hours=step.duration_h / 2)
    original = replace(capture(START), problem=inputs)
    expected_soc = 50 + plan.flows[index].ev[0].stored_kwh / 60 * 100 / 2
    current = replace(
        original,
        problem=replace(
            inputs, ev_work=(replace(inputs.ev_work[0], initial_soc=expected_soc + difference),)
        ),
    )
    live = LiveInputs(
        SimpleNamespace(
            published=(original, {"expected_flows": [asdict(flow) for flow in plan.flows]})
        ),
        None,
    )
    live.report = SimpleNamespace(readings=())
    assert live.deviated(current, now) is expected


def test_tolerance_sized_minimum_power_roundoff_cannot_reject_native_plan():
    """A validated 5-microwatt roundoff below 11 kW must serialize to the exact supported lower bound."""
    inputs = problem((work(control="variable", minimum_w=11000),))
    solved = solve(inputs)
    changed = replace(
        solved,
        flows=tuple(
            replace(
                flow,
                ev=tuple(
                    replace(row, power_w=row.power_w - 0.000005) if row.power_w else row
                    for row in flow.ev
                ),
            )
            for flow in solved.flows
        ),
    )
    costs = validate(inputs, changed)
    snapshot = ev_snapshot(inputs, "minimum-roundoff")
    original = plan_document(
        snapshot, SimpleNamespace(plan=changed, status=Status.OPTIMAL), costs, START
    )
    packet = execution_document(original)
    assert Plan.parse(packet, packet["config_revision"])
    assert {step["devices"]["point-a"]["power_w"] for step in packet["steps"]} == {0, 11000}
    assert any(0 < flow["ev"][0]["power_w"] < 11000 for flow in original["expected_flows"])


def document():
    """Publish the independently checked 0.6-kWh car target, keeping its original refined physics."""
    inputs = problem((work(),))
    plan = solve(inputs)
    snapshot = ev_snapshot(inputs, "ev-example")
    return plan_document(
        snapshot, SimpleNamespace(plan=plan, status=Status.OPTIMAL), validate(inputs, plan), START
    )


def test_ev_execution_is_compact_without_removing_original_energy_evidence():
    """Identical controls coalesce, while the native receiver gets exact stop times and the archive retains every flow."""
    original = document()
    before = deepcopy(original)
    packet = execution_document(original)
    assert "input_snapshot" not in packet
    assert "expected_flows" not in packet
    assert packet["ev_context"] == original["ev_context"]
    assert len(packet["steps"]) < len(original["expected_flows"])
    assert Plan.parse(packet, original["config_revision"]).plan_id == original["plan_id"]
    assert original == before
    assert original["ev_schedule"][0]["planned_kwh"] == pytest.approx(0.6)
    assert original["ev_schedule"][0]["predicted_soc"] == pytest.approx(51)
    assert original["ev_schedule"][0]["shortfall_kwh"] == pytest.approx(0, abs=1e-8)


def test_four_ev_mixed_horizon_produces_bounded_native_execution():
    """Four distinct departures reach their targets through the production bounded worker.

    The ordinary horizon contains 96 quarters followed by 24 hours. The cars need
    (80 - initial SoC) percent of their respective capacities: 27, 26.84, 26.66
    and 26.46 useful kWh. All targets fit the shared 11-kW connection before their
    departures, so positive prices cannot justify missing service. This case
    covers the finite charging controls, not arbitrary continuous-time switching.
    Financial optimization may stop with a feasible candidate after service is
    proved. The default process deadline also bounds construction and transport.
    """
    from open_energy_planner.appliances.ev.model import EVWork, refine
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.optimization.domain import Interval

    intervals = tuple(
        Interval(
            START + timedelta(minutes=15 * index),
            0.25,
            0.15 + (index % 8) / 100,
            0.05,
            load_kwh=0.05,
        )
        for index in range(96)
    ) + tuple(
        Interval(START + timedelta(hours=24 + index), 1, 0.2, 0.05, load_kwh=0.2)
        for index in range(24)
    )
    works = tuple(
        EVWork(
            f"session-{index}",
            f"point-{index}",
            f"car-{index}",
            START + timedelta(seconds=40),
            START + timedelta(hours=18 + index, minutes=index * 3),
            60 + index,
            35 + index,
            80,
            1400,
            11000 - index * 500,
            "variable",
        )
        for index in range(4)
    )
    inputs = refine(InstallationProblem(intervals, (), (), (), 11000, 11000), works)
    result = asyncio.run(ProcessSolver().solve(inputs))
    assert result.status in (Status.OPTIMAL, Status.FEASIBLE), result.stop_reason
    assert result.plan is not None
    costs = validate(inputs, result.plan)
    expected_energy = (27, 26.84, 26.66, 26.46)
    for flow in result.plan.flows:
        assert {row.session_id for row in flow.ev} == {f"session-{index}" for index in range(4)}
    for index, expected in enumerate(expected_energy):
        assert sum(
            row.stored_kwh
            for flow in result.plan.flows
            for row in flow.ev
            if row.session_id == f"session-{index}"
        ) == pytest.approx(expected, abs=1e-6)
    original = plan_document(
        ev_snapshot(inputs, "four-ev-mixed-horizon"),
        result,
        costs,
        START,
    )
    packet = execution_document(original)
    assert 1 <= len(packet["steps"]) <= 1024
    assert len(json.dumps(packet, allow_nan=False).encode("utf-8")) < 4 * 1024 * 1024
    parsed = Plan.parse(packet, original["config_revision"])
    expected_identities = {
        (f"point-{index}", f"car-{index}", f"session-{index}") for index in range(4)
    }
    for step in parsed.steps:
        assert {
            (command.device_id, command.vehicle_id, command.session_id)
            for command in step.command.devices
        } == expected_identities


@pytest.mark.parametrize(
    "defect", ["missing_context", "wrong_car", "wrong_session", "after_departure", "negative_power"]
)
def test_native_ev_receiver_rejects_unbound_or_unsafe_commands(defect):
    """A valid EV plan cannot be repurposed for another car/session, execute after departure or discharge."""
    packet = execution_document(document())
    active = next(step for step in packet["steps"] if step["devices"]["point-a"]["power_w"] > 0)
    if defect == "missing_context":
        packet.pop("ev_context")
    elif defect == "wrong_car":
        active["devices"]["point-a"]["vehicle_id"] = "another-car"
    elif defect == "wrong_session":
        active["devices"]["point-a"]["session_id"] = "another-connection"
    elif defect == "after_departure":
        packet["ev_context"]["work"][0]["deadline"] = active["start"]
    else:
        active["devices"]["point-a"]["power_w"] = -1
    with pytest.raises(ValueError, match="EV"):
        Plan.parse(packet, packet["config_revision"])


def test_stationary_reference_keeps_identical_ev_service_and_does_not_claim_ev_savings():
    """With no stationary battery, the common 2/3 AC kWh costs the same in both scenarios, yielding zero battery savings."""
    original = document()
    reference = comparison(original, without_battery=True)
    assert reference["expected_savings"] == pytest.approx(0)
    view = scenario(original, reference, without_battery=True)["chart"]
    assert sum(
        step["load_kwh"] for step in view["input_snapshot"]["problem"]["intervals"]
    ) == pytest.approx(0)
    assert sum(
        row["ac_kwh"] for flow in view["expected_flows"] for row in flow["ev"]
    ) == pytest.approx(2 / 3)
