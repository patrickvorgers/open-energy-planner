"""Independently verify one-count demand replacement and per-plan attribution guards."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.appliances.cycles.model import ApplianceWork, CycleProfile
from open_energy_planner.demand.calculation.composition import (
    LoadComponent,
    LoadStep,
    integral,
    replace_cycle,
    scale_window,
    validate_components,
)
from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.optimization.domain import Interval

START = datetime(2026, 11, 4, tzinfo=UTC)


def test_fractional_second_integration_retains_energy_without_rounding_a_timedelta():
    """A .123456-second portion of .04 kWh/hour contains .04*.123456/3600 kWh exactly."""
    boundary = START + timedelta(microseconds=123456)
    steps = (LoadStep(START, 1, 0.04),)
    expected = 0.04 * 0.123456 / 3600
    assert integral(steps, START, boundary) == pytest.approx(expected, abs=1e-15)
    split = scale_window(steps, boundary, START + timedelta(hours=1), 0)
    assert split[0].kwh == pytest.approx(expected, abs=1e-15)
    assert integral(split, START, boundary) == pytest.approx(expected, abs=1e-15)
    assert sum(step.kwh for step in split) == pytest.approx(0.04, abs=1e-15)


def test_cycle_replaces_only_its_energy_and_service_window():
    """A 1-kWh request consumes 1 of 2 expected kWh in its two-hour window, preserving 4 kWh outside."""
    steps = tuple(LoadStep(START + timedelta(hours=index), 1, 1) for index in range(6))
    work = ApplianceWork(
        "task",
        "sensor.washer",
        START + timedelta(hours=2),
        START + timedelta(hours=4),
        CycleProfile((1000,) * 4, evidence="manual"),
    )
    remaining = replace_cycle(steps, work, START, START + timedelta(hours=6))
    assert integral(remaining, START, START + timedelta(hours=6)) == pytest.approx(5)
    assert integral(remaining, work.ready_at, work.deadline) == pytest.approx(1)
    assert integral(remaining, START, work.ready_at) == pytest.approx(2)
    assert integral(remaining, work.deadline, START + timedelta(hours=6)) == pytest.approx(2)


def test_a_confirmed_large_cycle_never_creates_negative_expectation():
    """An 8-kWh concrete charge replaces all 6 expected kWh, then contributes 8 once."""
    steps = (LoadStep(START, 6, 6),)
    work = ApplianceWork(
        "task",
        "sensor.charger",
        START,
        START + timedelta(hours=6),
        CycleProfile((2000,) * 16, evidence="manual"),
    )
    remaining = replace_cycle(steps, work, START, START + timedelta(hours=6))
    assert sum(step.kwh for step in remaining) == 0
    assert 4 + sum(step.kwh for step in remaining) + work.profile.energy_kwh == 12


def test_running_work_debits_only_remaining_cycle_and_accepts_partial_quarters():
    """A running 1-kWh cycle with 45 minutes left debits .75 kWh, even at a non-quarter boundary."""
    now = START + timedelta(minutes=15, seconds=3)
    steps = (LoadStep(now, 2, 2),)
    work = ApplianceWork(
        "task",
        "sensor.washer",
        START,
        START + timedelta(hours=3),
        CycleProfile((1000,) * 4),
        START + timedelta(seconds=3),
    )
    remaining = replace_cycle(steps, work, now, now + timedelta(hours=2))
    assert integral(remaining, now, now + timedelta(hours=2)) == pytest.approx(1.25)


def test_component_guard_rejects_duplicate_missing_or_double_counted_load():
    """Only a .3-kWh household base plus .2-kWh appliance may represent a .5-kWh input interval."""
    problem = InstallationProblem(
        (Interval(START, 1, 0.1, 0, load_kwh=0.5),), (), (), (), 3000, 3000
    )
    base = LoadComponent(None, "base", (LoadStep(START, 1, 0.3),), (LoadStep(START, 1, 0.3),))
    device = LoadComponent(
        "sensor.washer", "device", (LoadStep(START, 1, 0.2),), (LoadStep(START, 1, 0.2),)
    )
    validate_components(problem, (base, device))
    for components in (
        (base, device, device),
        (base,),
        (base, replace(device, steps=(LoadStep(START, 1, 0.4),))),
    ):
        with pytest.raises(ValueError):
            validate_components(problem, components)
    with pytest.raises(ValueError, match="coverage"):
        validate_components(problem, (base, replace(device, steps=())))


def test_bad_attribution_cannot_publish_an_otherwise_valid_solver_plan():
    """A valid .5-kWh physical solve is rejected when its attribution incorrectly claims .7 kWh."""
    from open_energy_planner.optimization.adapters.highs import HighsSolver
    from open_energy_planner.planning import InputSnapshot, PlanningService

    class Solver:
        """Use the actual native solver through the existing asynchronous planning port."""

        async def solve(self, problem):
            """Return a genuine feasible physical plan without changing its input accounting."""
            return HighsSolver().solve(problem)

    problem = InstallationProblem(
        (Interval(START, 1, 0.1, 0, load_kwh=0.5),), (), (), (), 3000, 3000
    )
    components = tuple(
        LoadComponent(
            entity, "forecast", (LoadStep(START, 1, amount),), (LoadStep(START, 1, amount),)
        )
        for entity, amount in ((None, 0.3), ("sensor.washer", 0.4))
    )
    snapshot = InputSnapshot(
        "snapshot",
        "configuration",
        START,
        "meters",
        "forecasts",
        "prices",
        problem,
        load_components=components,
    )
    outcome = asyncio.run(PlanningService(Solver(), clock=lambda: START).calculate(snapshot))
    assert outcome.document is None
    assert outcome.reason == "candidate_rejected"
