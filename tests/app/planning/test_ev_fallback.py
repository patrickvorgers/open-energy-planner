"""Verify that only a checked, command-free EV fallback can replace a planning round."""

import asyncio
from dataclasses import replace

import pytest

from custom_components.open_energy_planner.protocol import Plan
from open_energy_planner.appliances.ev.fallback import uncontrolled_snapshot
from open_energy_planner.demand.calculation.composition import LoadComponent, LoadStep
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.adapters.process.worker import solve_with_ev_fallback_in_child
from open_energy_planner.optimization.domain import Battery, Interval, SolveResult, Status, Strategy
from open_energy_planner.planning import PlanningCoordinator, PlanningService
from open_energy_planner.runtime.diagnostics.planning_notifications import (
    PlanningStatus,
    planning_notifications,
)
from open_energy_planner.synthetic import capture
from tests.app.ev.test_ev_solver import START, work
from tests.app.installation.support.problems import installation_problem


def snapshot():
    """Two intervals with 2 and 3 kWh of expected charging and 1 kWh of other load each."""
    from datetime import timedelta

    intervals = (
        Interval(START, 1, 0.5, 0, load_kwh=1),
        Interval(START + timedelta(hours=1), 1, 0.1, 0, load_kwh=1),
    )
    inputs = installation_problem(
        intervals,
        Battery(1, 0, charge_limit_w=0, discharge_limit_w=0),
        grid_import_limit_w=16000,
        grid_export_limit_w=11000,
    )
    household = tuple(LoadStep(interval.start, 1, 1) for interval in intervals)
    replaced = tuple(LoadStep(interval.start, 1, 0) for interval in intervals)
    charger = tuple(
        LoadStep(interval.start, 1, energy)
        for interval, energy in zip(intervals, (2, 3), strict=True)
    )
    return replace(
        capture(START),
        problem=replace(inputs, ev_work=(work(),)),
        ev_revision="ev-settings",
        ev_power_bounds=(("sensor.charger", 11000),),
        load_components=(
            LoadComponent(None, "household", household, household),
            LoadComponent("sensor.charger", "charger", charger, replaced),
        ),
    )


def test_fallback_restores_load_and_protects_both_directions():
    """Reserve import for 11 kW charging and export for the chance of no charging."""
    original = snapshot()
    fallback = uncontrolled_snapshot(original)
    assert tuple(interval.load_kwh for interval in fallback.problem.intervals) == (3, 4)
    assert fallback.problem.grid_import_limit_w == 7000
    assert fallback.problem.grid_export_limit_w == 8000
    assert fallback.problem.ev_work == ()
    assert fallback.load_components[1].steps == original.load_components[1].forecast_steps
    assert original.problem.intervals[0].load_kwh == 1
    assert (
        uncontrolled_snapshot(replace(original, load_components=original.load_components[:1]))
        is None
    )
    assert (
        uncontrolled_snapshot(
            replace(original, problem=replace(original.problem, grid_export_limit_w=1000))
        )
        is None
    )


def test_verified_fallback_publishes_and_has_no_ev_permissions(monkeypatch):
    """Use the real fallback optimizer after an injected EV proof failure, then validate the HA plan."""
    original_solve = HighsSolver.solve

    def solve(solver, inputs, options):
        """Only inject the proof failure on the controlled attempt."""
        if inputs.ev_work:
            assert options.ev_fallback_reserve_s == 20
            return SolveResult(Status.NO_CANDIDATE, None, "ev_service_unproven", 0, 0, None)
        assert 0 < options.time_limit_s <= 120
        return original_solve(solver, inputs, options)

    class Solver:
        """Run the actual child entry point inline for the injected failure."""

        async def solve_with_ev_fallback(self, primary, fallback, *, strategy):
            """Capture the result and selected inputs without a spawned test process."""
            received = []

            class Sender:
                """Collect a single worker response."""

                def send(self, value):
                    """Collect the transmitted result."""
                    received.append(value)

                def close(self):
                    """Complete the in-memory worker channel."""

            solve_with_ev_fallback_in_child(primary, Sender(), 120, strategy, fallback)
            return received[0]

    async def scenario():
        """Publish through the coordinator using the independent validation path."""
        documents = []

        async def publish(document):
            """Collect the exact candidate approved for publication."""
            documents.append(document)

        coordinator = PlanningCoordinator(PlanningService(Solver(), clock=lambda: START), publish)
        coordinator.submit(snapshot())
        await coordinator.wait()
        assert coordinator.status == "ready"
        assert coordinator.reason == "ev_control_deferred"
        (document,) = documents
        assert document["ev_control_deferred"] is True
        assert [flow["grid_import"] for flow in document["expected_flows"]] == pytest.approx([3, 4])
        assert document["ev_context"]["work"] == []
        assert document["ev_schedule"] == []
        assert all(
            device.kind != "ev"
            for step in Plan.parse(document, "demo-1").steps
            for device in step.command.devices
        )

    monkeypatch.setattr(HighsSolver, "solve", solve)
    asyncio.run(scenario())


def test_unrelated_solver_failure_does_not_remove_charging(monkeypatch):
    """Restrict the fallback to the EV priority proof failure."""
    inputs = snapshot().problem
    result = SolveResult(Status.NO_CANDIDATE, None, "reserve_recovery_time_limit", 0, 0, None)

    def solve(_solver, problem, _options):
        """Reject every second attempt for this unrelated stop."""
        assert problem is inputs
        return result

    class Sender:
        """Check the response that would cross the worker boundary."""

        def send(self, value):
            """Require the original failure rather than a candidate."""
            assert value == (result, False)

        def close(self):
            """Complete the in-memory worker channel."""

    monkeypatch.setattr(HighsSolver, "solve", solve)
    solve_with_ev_fallback_in_child(inputs, Sender(), 120, Strategy.COST, inputs)


def test_wait_notice_distinguishes_an_active_plan_from_a_blocker():
    """Keep automatic retry visible while clearly describing whether commands are available."""
    active = PlanningStatus("calculating", None, None, ev_fallback_active=True)
    (notice,) = planning_notifications(active, lambda _reason: None)
    assert notice.blocks == ()
    assert "plan active" in notice.text.title
    assert "next planning round" in notice.text.message
    blocked = PlanningStatus("failed", "ev_service_unproven", START)
    (notice,) = planning_notifications(blocked, lambda _reason: None)
    assert notice.blocks == ("planning",)
    assert "retries automatically" in notice.text.message
