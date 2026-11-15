"""Shared uniform phase grids cannot exhaust planning for unrelated appliances."""

from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.cycles.model import CycleProfile
from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.optimization.domain import Interval
from open_energy_planner.planning import InputSnapshot
from tests.app.appliance.support import NOW


@pytest.mark.parametrize("distinct", [True, False])
@pytest.mark.parametrize("fixed", [False, True])
def test_many_partial_uniform_profiles_keep_other_tasks_and_household_energy(
    service, distinct, fixed
):
    """Partial or fixed shapes share identical grids or limit only excess distinct grids over 48 hours."""
    count = 60 if fixed else 15
    partials = {
        f"sensor.partial_{index}_energy": index if distinct else 1 for index in range(1, count + 1)
    }
    other = "sensor.ordinary_energy"
    entities = (*partials, other)
    service.consumers.sync(
        {
            "entities": [{"entity_id": entity, "name": entity} for entity in entities],
            "suggestions": {
                "consumers": [
                    {"entity_id": entity, "upstream": None, "power_entity": entity + "_power"}
                    for entity in entities
                ]
            },
        }
    )
    service.consumers.save(
        [{"entity_id": entity, "weather_sensitive": False} for entity in entities],
        service.consumers.version,
    )
    data = deepcopy(service.state)
    for entity, remainder in partials.items():
        data["devices"][entity] = {
            "shiftable": True,
            "profile": asdict(
                CycleProfile(
                    (1000,) * (144 if fixed else 4),
                    measured_duration_seconds=(42900 if fixed else 900) + remainder,
                    phase_seconds=300,
                )
            ),
        }
    data["devices"][other] = {"shiftable": True, "profile": asdict(CycleProfile((1000,)))}
    service.commit(data)
    for entity in entities:
        ready = NOW - timedelta(hours=1) if fixed and entity != other else NOW
        service.submit(
            entity,
            "ready-" + entity,
            ready,
            NOW + timedelta(hours=1 if entity == other else 48),
            ready,
        )
    if fixed:
        data = deepcopy(service.state)
        for entity, offset in partials.items():
            # Represent immutable already-started work. No measurement or test plan grants start rights.
            data["tasks"][entity].update(
                status="running",
                attempt={
                    "id": "already-started",
                    "status": "started",
                    "start": (NOW - timedelta(seconds=offset)).isoformat(),
                },
            )
        service.commit(data)
    frozen = {entity: deepcopy(task["profile"]) for entity, task in service.state["tasks"].items()}
    residual = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 48, "kwh": 2}],
    }
    forecast = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 48, "kwh": 0.5}],
    }
    service.consumers.result = {
        "residual_baseline": residual,
        "devices": [
            {"entity_id": entity, "forecast": forecast, "baseline_forecast": forecast}
            for entity in entities
        ],
    }
    problem = InstallationProblem(
        tuple(
            Interval(NOW + timedelta(hours=index), 1, 0.2, 0, (2 + len(entities) * 0.5) / 48)
            for index in range(48)
        ),
        (),
        (),
        (),
        100000,
        100000,
    )
    snapshot = InputSnapshot(
        "snapshot", "configuration", NOW, "measurement", "forecast", "prices", problem
    )
    prepared = service.prepare(snapshot, SimpleNamespace(), NOW)
    assert service.issue is None
    admitted = {work.entity_id for work in prepared.problem.appliances}
    assert other in admitted
    assert (
        (45 if fixed else 13) <= len(admitted) < len(entities)
        if distinct
        else len(admitted) == len(entities)
    )
    assert len(prepared.problem.intervals) <= 8192
    if not distinct:
        assert len(prepared.problem.intervals) < 1200
    # Every fixed run ends 42,900 seconds into the horizon, replacing .5*42900/(48*3600) kWh of expectation.
    # Short partial tasks replace only their cycle kWh within the full ready/deadline window.
    # The ordinary one-hour window replaces .5/48 kWh in either case.
    expected = (
        2
        + len(entities) * 0.5
        - sum(
            0.5 * 42900 / (48 * 3600) if fixed else (900 + remainder) / 3600
            for entity, remainder in partials.items()
            if entity in admitted
        )
        - 0.5 / 48
    )
    assert sum(interval.load_kwh for interval in prepared.problem.intervals) == pytest.approx(
        expected
    )
    for component in prepared.load_components:
        if component.entity_id in set(entities) - admitted:
            assert component.steps == component.forecast_steps
            assert sum(step.kwh for step in component.steps) == pytest.approx(0.5)
    assert {entity: task["profile"] for entity, task in service.state["tasks"].items()} == frozen
    service.accept_plan(
        {
            "plan_id": "bounded",
            "appliance_schedule": [
                {"task_id": service.state["tasks"][entity]["id"], "start": NOW.isoformat()}
                for entity in admitted
            ],
        }
    )
    assert service.state["tasks"][other]["status"] == "scheduled"
    assert all(
        service.state["tasks"][entity]["status"] == ("running" if fixed else "forecast_not_ready")
        for entity in set(entities) - admitted
    )
