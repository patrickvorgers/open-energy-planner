"""Independently verify one-count demand replacement and per-plan attribution guards."""

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.appliances.cycles.model import ApplianceWork, CycleProfile
from open_energy_planner.demand.calculation.composition import (
    LoadComponent,
    LoadStep,
    integral,
    integrals,
    replace_cycle,
    scale_window,
    validate_components,
)
from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.optimization.domain import Interval

START = datetime(2026, 11, 4, tzinfo=UTC)


def test_many_device_integrations_visit_each_source_step_once():
    """Thirty-four hourly device series cover 192 quarters without rescanning their source history."""
    windows = tuple(
        (START + timedelta(minutes=15 * index), START + timedelta(minutes=15 * (index + 1)))
        for index in range(192)
    )
    for device_index in range(34):
        visited = []

        def steps(visited=visited, device_index=device_index):
            """Count actual source traversal rather than relying on machine-dependent timing."""
            for hour in range(48):
                visited.append(hour)
                yield LoadStep(START + timedelta(hours=hour), 1, (device_index + 1) / 100)

        assert integrals(steps(), windows) == pytest.approx([(device_index + 1) / 400] * 192)
        assert visited == list(range(48))


def test_batch_integration_preserves_fractional_boundaries_and_rejects_bad_coverage():
    """Refined windows retain fractional-second energy and reject gaps, overlaps and reordered inputs."""
    split = START + timedelta(seconds=0.123456)
    windows = ((START, split), (split, START + timedelta(hours=2)))
    steps = (LoadStep(START, 1, 0.04), LoadStep(START + timedelta(hours=1), 1, 0.08))
    assert integrals(steps, windows) == pytest.approx(
        [0.04 * 0.123456 / 3600, 0.12 - 0.04 * 0.123456 / 3600]
    )
    for invalid in (steps[:1], steps[::-1], (steps[0], steps[0], steps[1])):
        with pytest.raises(ValueError, match="coverage"):
            integrals(invalid, windows)


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


@pytest.mark.parametrize(
    "kind,evidence,allowed",
    [
        ("household", "empty", True),
        ("household", "base", True),
        ("cycle", "empty", False),
        ("cycle", "base", False),
        ("cycle", "owned", True),
        ("ev", "empty", False),
        ("ev", "base", False),
        ("ev", "owned", True),
        ("ev", "wrong_binding", False),
        ("thermal", "empty", False),
        ("thermal", "base", False),
        ("thermal", "owned", True),
        ("thermal", "wrong_binding", False),
    ],
)
def test_publication_requires_owned_controlled_consumption_but_not_optional_household_components(
    kind, evidence, allowed
):
    """A genuine feasible solve needs each controlled meter's ownership. Ordinary .3-kWh household plans remain valid."""
    from open_energy_planner.appliances.ev.model import EVWork, refine
    from open_energy_planner.appliances.thermal.model import ThermalWork
    from open_energy_planner.installation.commands import plan_document
    from open_energy_planner.installation.validation import validate
    from open_energy_planner.optimization.adapters.highs import HighsSolver
    from open_energy_planner.planning import InputSnapshot

    problem = InstallationProblem(
        (Interval(START, 0.25, 0.1, 0, load_kwh=0.3),), (), (), (), 3000, 3000
    )
    ev_evidence, thermal_evidence = (), ()
    if kind == "cycle":
        problem = replace(
            problem,
            appliances=(
                ApplianceWork(
                    "cycle",
                    "sensor.controlled",
                    START,
                    START + timedelta(hours=1),
                    CycleProfile((1000,)),
                ),
            ),
        )
    elif kind == "ev":
        work = EVWork(
            "session",
            "point",
            "car",
            START,
            START + timedelta(minutes=15),
            10,
            0,
            1,
            0,
            1000,
            "variable",
        )
        problem = refine(problem, (work,))
        if evidence in ("owned", "wrong_binding"):
            ev_evidence = (
                {
                    "work": {
                        **asdict(work),
                        "point_id": "wrong" if evidence == "wrong_binding" else "point",
                    },
                    "energy_entity": "sensor.controlled",
                },
            )
    elif kind == "thermal":
        work = ThermalWork("boiler", START, 40, 40, 80, 20, 0.1, 0, 2000, 1, (0,), 40)
        problem = replace(problem, thermal_work=(work,))
        if evidence in ("owned", "wrong_binding"):
            thermal_evidence = (
                {
                    "configuration": {
                        "id": "wrong" if evidence == "wrong_binding" else "boiler",
                        "energy_entity": "sensor.controlled",
                    }
                },
            )
    components = (
        ()
        if evidence == "empty"
        else (
            LoadComponent(
                None, "household", (LoadStep(START, 0.25, 0.3),), (LoadStep(START, 0.25, 0.3),)
            ),
        )
    )
    if evidence in ("owned", "wrong_binding"):
        components += (
            LoadComponent(
                "sensor.controlled",
                "device",
                (LoadStep(START, 0.25, 0),),
                (LoadStep(START, 0.25, 0),),
            ),
        )
    snapshot = InputSnapshot(
        "snapshot",
        "settings",
        START,
        "meters",
        "forecast",
        "prices",
        problem,
        load_components=components,
        ev_evidence=ev_evidence,
        thermal_evidence=thermal_evidence,
    )
    solved = HighsSolver().solve(problem)
    assert solved.plan is not None, solved.stop_reason
    if kind == "cycle":
        assert solved.plan.appliance_starts == (("cycle", START),)
    costs = validate(problem, solved.plan)
    if allowed:
        published = plan_document(snapshot, solved, costs, START)
        assert published["expected_flows"]
        assert published["input_snapshot_id"] == "snapshot"
    else:
        with pytest.raises(ValueError):
            plan_document(snapshot, solved, costs, START)


def test_empty_components_cannot_bypass_explicit_controlled_meter_validation():
    """Optional household evidence is allowed, but explicitly controlled meter ownership is mandatory."""
    problem = InstallationProblem(
        (Interval(START, 1, 0.1, 0, load_kwh=0.3),), (), (), (), 3000, 3000
    )
    validate_components(problem, ())
    with pytest.raises(ValueError, match="Household component"):
        validate_components(problem, (), controlled_entities=("sensor.controlled",))
