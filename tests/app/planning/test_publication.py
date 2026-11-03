"""Prove bounded publication progress and independently calculated battery availability gates."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.installation.model import BatteryState, InstallationProblem
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.planning import PlanningCoordinator, battery_dispatch_available
from open_energy_planner.solar.calculation.model import SolarInverter
from open_energy_planner.synthetic import capture
from tests.app.planning.test_planning_pipeline import NOW, ImmediateSolver, service


@pytest.mark.parametrize("hard_change", [False, True])
def test_updates_during_every_solve_do_not_starve_publication(hard_change):
    """Ordinary updates permit each valid result. A changed configuration blocks only the old generation."""

    async def scenario():
        """Inject twenty newer captures during each of three independently validated real solves."""
        published = []

        class UpdatingSolver(ImmediateSolver):
            """Deliver a fresh batch during every solve, then stop feeding after the third round."""

            def __init__(self):
                """Track finite test rounds without depending on scheduler timing."""
                self.calls = 0

            async def solve(self, problem):
                """Queue replacements before returning the active calculation's unchanged valid result."""
                self.calls += 1
                if self.calls <= 3:
                    for _ in range(20):
                        snapshot = capture(NOW, 0.5)
                        if hard_change:
                            snapshot = replace(snapshot, config_revision="changed-installation")
                        coordinator.submit(snapshot)
                return await super().solve(problem)

        async def publish(document):
            """Retain the actual generation and round in which publication made progress."""
            published.append((solver.calls, document))

        solver = UpdatingSolver()
        coordinator = PlanningCoordinator(service(solver), publish)
        first = capture(NOW, 0.5)
        coordinator.submit(first)
        await coordinator.wait()
        assert solver.calls == 4
        assert [round_number for round_number, _ in published] == (
            [2, 3, 4] if hard_change else [1, 2, 3, 4]
        )
        if not hard_change:
            assert published[0][1]["input_snapshot_id"] == first.snapshot_id
        assert (coordinator.status, coordinator.reason) == ("ready", "validated")

    asyncio.run(scenario())


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize(
    ("end_kwh", "current_kwh", "allowed"),
    [(0.2, 0.51, True), (0.2, 0.3, True), (0.2, 0.29, False), (1, 0.8, True), (1, 0.84, False)],
)
def test_storage_gate_uses_losses_and_only_the_valid_command_window(
    multiple, end_kwh, current_kwh, allowed
):
    """A 15-minute internal delta of -0.3/+0.5 uses one third in five minutes. Retain 0.2–1 kWh per battery."""
    cell = Battery(1, 0.5, minimum_kwh=0.2)
    original = replace(
        capture(NOW, 0.5),
        problem=replace(
            capture(NOW).problem, batteries=(BatteryState("battery", "battery", cell),)
        ),
    )
    original = replace(
        original, problem=replace(original.problem, intervals=original.problem.intervals[:1])
    )
    document = {
        "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
        "steps": [{"start": NOW.isoformat(), "end": (NOW + timedelta(minutes=15)).isoformat()}],
        "expected_flows": [{"batteries": [{"device_id": "battery", "end_kwh": end_kwh}]}],
    }
    if multiple:
        problem = InstallationProblem(
            (Interval(NOW, 0.25, 0, 0, 0, 0),),
            (SolarInverter("inverter", 4000, 1, True),),
            (BatteryState("one", "inverter", cell), BatteryState("two", "inverter", cell)),
            (),
            4000,
            4000,
        )
        original = replace(original, problem=problem)
        current = replace(
            original,
            problem=replace(
                problem,
                batteries=(
                    replace(problem.batteries[0], battery=replace(cell, initial_kwh=current_kwh)),
                    problem.batteries[1],
                ),
            ),
        )
        document["expected_flows"] = [
            {
                "batteries": [
                    {"device_id": "one", "end_kwh": end_kwh},
                    {"device_id": "two", "end_kwh": 0.5},
                ]
            }
        ]
    else:
        current = replace(
            original,
            problem=replace(
                original.problem,
                batteries=(
                    replace(
                        original.problem.batteries[0],
                        battery=replace(cell, initial_kwh=current_kwh),
                    ),
                ),
            ),
        )
    assert battery_dispatch_available(document, original, current) is allowed


def test_storage_gate_accepts_an_installation_without_batteries():
    """A grid/solar installation has no required battery state or storage dispatch gate."""
    original = capture(NOW)
    original = replace(original, problem=replace(original.problem, batteries=()))
    original = replace(
        original, problem=replace(original.problem, intervals=original.problem.intervals[:1])
    )
    document = {
        "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
        "steps": [{"start": NOW.isoformat(), "end": (NOW + timedelta(minutes=15)).isoformat()}],
        "expected_flows": [{"batteries": []}],
    }
    assert battery_dispatch_available(document, original, original)


@pytest.mark.parametrize("delta,allowed", [(0.3, True), (0, True), (-0.1, False)])
def test_storage_gate_uses_actual_below_reserve_energy_without_clamping(delta, allowed):
    """At 0.1 kWh below a 0.2 reserve, charging or idling remains executable. Any further discharge is rejected."""
    original = capture(NOW, 0.5)
    original = replace(
        original,
        problem=replace(
            original.problem,
            batteries=(
                BatteryState(
                    "battery", "battery", Battery(1, 0.1, minimum_kwh=0.2, recover_reserve=True)
                ),
            ),
            intervals=original.problem.intervals[:1],
        ),
    )
    document = {
        "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
        "steps": [{"start": NOW.isoformat(), "end": (NOW + timedelta(minutes=15)).isoformat()}],
        "expected_flows": [{"batteries": [{"device_id": "battery", "end_kwh": 0.1 + delta}]}],
    }
    assert battery_dispatch_available(document, original, original) is allowed


def test_storage_gate_uses_billing_intervals_when_commands_are_grouped():
    """One merged command can cover multiple billing flows. Integrate each internal delta, not command count."""
    original = capture(NOW, 0.5)
    original = replace(
        original,
        problem=replace(
            original.problem,
            intervals=(
                Interval(NOW, 0.125, 0, 0, 0, 0, control_group="same-quarter"),
                Interval(
                    NOW + timedelta(minutes=7.5), 0.125, 0, 0, 0, 0, control_group="same-quarter"
                ),
            ),
        ),
    )
    document = {
        "valid_until": (NOW + timedelta(minutes=10)).isoformat(),
        "steps": [{"start": NOW.isoformat(), "end": (NOW + timedelta(minutes=15)).isoformat()}],
        "expected_flows": [
            {"batteries": [{"device_id": "battery", "end_kwh": 0.7}]},
            {"batteries": [{"device_id": "battery", "end_kwh": 0.9}]},
        ],
    }
    assert battery_dispatch_available(document, original, original)
