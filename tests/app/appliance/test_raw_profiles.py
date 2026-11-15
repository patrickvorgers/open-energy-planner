"""Irregular Recorder phases preserve actual peaks, energy and immutable legacy profile meaning."""

from dataclasses import asdict
from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles.model import CycleProfile, average_profiles
from open_energy_planner.appliances.cycles.service import profile
from tests.app.appliance.support import NOW


def test_irregular_profile_preserves_twenty_second_peak_and_exact_energy():
    """A 3-kW twenty-second pulse plus 880 seconds at 100 W is 37/900 kWh, not a five-minute mean."""
    cycle = CycleProfile((3000, 100), phase_durations_seconds=(20, 880))
    assert cycle.duration_seconds == 900
    assert cycle.energy_kwh == pytest.approx(37 / 900)
    assert cycle.energy(NOW, NOW, NOW + timedelta(seconds=20)) == pytest.approx(1 / 60)
    assert profile(asdict(cycle)) == cycle
    assert cycle.phase_boundaries == (0, 20, 900)


def test_average_irregular_cycles_uses_actual_phase_boundaries():
    """Two pulses at different offsets average to 1550 W over forty seconds and retain 37/900 kWh."""
    first = CycleProfile((3000, 100), phase_durations_seconds=(20, 880))
    second = CycleProfile((100, 3000, 100), phase_durations_seconds=(20, 20, 860))
    average = average_profiles(first, second)
    assert average.samples == 2
    assert average.phase_boundaries == (0, 20, 40, 900)
    assert average.power_w == pytest.approx((1550, 1550, 100))
    assert average.energy_kwh == pytest.approx(37 / 900)
    assert CycleProfile((1000,)).energy_kwh == pytest.approx(0.25)


def test_admitted_large_irregular_average_visits_source_edges_linearly(record_testsuite_property):
    """Two admitted 900-phase traces retain 13/32 kWh without repeatedly visiting their source edges."""
    reads = [0]

    class CountedPowers(tuple):
        """Count actual phase values consumed by iteration or indexed access, independently of execution time."""

        def __iter__(self):
            """Record each source phase read by an integration scan."""
            for value in super().__iter__():
                reads[0] += 1
                yield value

        def __getitem__(self, index):
            """Record source phase access during a boundary sweep."""
            value = super().__getitem__(index)
            reads[0] += len(value) if isinstance(value, tuple) else 1
            return value

    previous = CycleProfile(
        CountedPowers((1000, 3000) * 450), samples=7, phase_durations_seconds=(1,) * 900
    )
    learned = CycleProfile(
        CountedPowers((500,) * 900),
        evidence="synthetic",
        phase_durations_seconds=(0.5,) + (1,) * 898 + (1.5,),
    )
    reads[0] = 0
    result = average_profiles(previous, learned, previous_weight=3)
    record_testsuite_property("large_irregular_source_power_reads", reads[0])
    assert reads[0] <= 6 * (len(previous.power_w) + len(learned.power_w))
    assert result.samples == 8 and result.evidence == "synthetic"
    assert result.power_w[:4] == pytest.approx((875, 875, 2375, 2375))
    assert result.energy_kwh == pytest.approx(13 / 32)
    assert result.duration_seconds == 900


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_irregular_average_pads_shorter_source_without_changing_weighted_energy(reverse):
    """A partial uniform 1/2-kWh run and shorter irregular 3/8-kWh run average to11/24 kWh in either order."""
    longer = CycleProfile((1000, 2000), samples=4, measured_duration_seconds=1350)
    shorter = CycleProfile(
        (3000, 0), samples=4, evidence="synthetic", phase_durations_seconds=(450, 450)
    )
    result = average_profiles(
        shorter if reverse else longer,
        longer if reverse else shorter,
        previous_weight=0.5 if reverse else 2,
    )
    assert result.samples == 5 and result.evidence == "synthetic"
    assert result.phase_boundaries == (0, 450, 900, 1350)
    assert result.power_w == pytest.approx((5000 / 3, 2000 / 3, 4000 / 3))
    assert result.energy_kwh == pytest.approx(11 / 24)


def test_zero_retained_weight_preserves_new_shape_with_zero_tail():
    """Zero retained weight gives the new shorter curve plus explicit zero power through the retained longer duration."""
    previous = CycleProfile((1000, 2000), samples=4, measured_duration_seconds=1350)
    learned = CycleProfile((3000, 0), phase_durations_seconds=(450, 450))
    result = average_profiles(previous, learned, previous_weight=0)
    assert result.power_w == (3000, 0, 0)
    assert result.phase_durations_seconds == (450, 450, 450)
    assert result.samples == 5 and result.evidence == "measured"
    assert result.energy_kwh == pytest.approx(3 / 8)


def test_dense_profile_expands_only_its_three_feasible_origins(service):
    """A 1,900-phase half-hour task with three starts needs 5,703 edges, not translations across every horizon quarter."""
    from copy import deepcopy
    from types import SimpleNamespace

    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot
    from tests.app.appliance.support import ENTITY

    shape = CycleProfile((1000, 3000) * 950, phase_durations_seconds=(0.5,) * 1899 + (850.5,))
    assert shape.energy_kwh == pytest.approx(89 / 72)
    data = deepcopy(service.state)
    data["devices"][ENTITY] = {"shiftable": True, "profile": asdict(shape)}
    service.commit(data)
    service.submit(
        ENTITY, "three-origins", NOW + timedelta(minutes=15), NOW + timedelta(minutes=80), NOW
    )
    frozen = deepcopy(service.state["tasks"][ENTITY]["profile"])
    residual = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 2, "kwh": 0.4}],
    }
    forecast = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 2, "kwh": 89 / 72}],
    }
    service.consumers.result = {
        "residual_baseline": residual,
        "devices": [{"entity_id": ENTITY, "forecast": forecast, "baseline_forecast": forecast}],
    }
    problem = InstallationProblem(
        tuple(
            Interval(NOW + timedelta(hours=index), 1, 0.2, 0, (0.4 + 89 / 72) / 2)
            for index in range(2)
        ),
        (),
        (),
        (),
        5000,
        5000,
    )
    snapshot = InputSnapshot(
        "snapshot", "configuration", NOW, "measurement", "forecast", "prices", problem
    )
    prepared = service.prepare(snapshot, SimpleNamespace(), NOW)
    assert service.issue is None
    [work] = prepared.problem.appliances
    assert work.profile == shape
    assert 5000 < len(prepared.problem.intervals) < 8192
    assert service.state["tasks"][ENTITY]["profile"] == frozen
    # The 65-minute ready/deadline window removes 65/120 of the two-hour expected cycle energy.
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(
        0.4 + 89 / 72 * 55 / 120
    )


def test_dense_raw_task_keeps_other_appliance_and_household_forecasts_usable(service):
    """A dense immutable raw task cannot exhaust the whole planning path or erase another appliance."""
    from copy import deepcopy
    from types import SimpleNamespace

    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot
    from tests.app.appliance.support import ENTITY, POWER

    other = "sensor.other_energy"
    service.consumers.sync(
        {
            "entities": [
                {"entity_id": ENTITY, "name": "Detailed"},
                {"entity_id": other, "name": "Other"},
            ],
            "suggestions": {
                "consumers": [
                    {"entity_id": ENTITY, "upstream": None, "power_entity": POWER},
                    {"entity_id": other, "upstream": None, "power_entity": "sensor.other_power"},
                ]
            },
        }
    )
    service.consumers.save(
        [{"entity_id": entity, "weather_sensitive": False} for entity in (ENTITY, other)],
        service.consumers.version,
    )
    dense = CycleProfile(
        tuple([1000, 3000] * 300 + [1000]), phase_durations_seconds=tuple([1.1] * 600 + [1140])
    )
    data = deepcopy(service.state)
    data["devices"][ENTITY] = {"shiftable": True, "profile": asdict(dense)}
    data["devices"][other] = {"shiftable": True, "profile": asdict(CycleProfile((1000,)))}
    service.commit(data)
    first = service.submit(ENTITY, "dense-ready", NOW, NOW + timedelta(hours=4), NOW)
    service.submit(other, "ordinary-ready", NOW, NOW + timedelta(hours=4), NOW)
    frozen = deepcopy(first["profile"])
    residual = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 4, "kwh": 0.4}],
    }
    forecast = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 4, "kwh": 0.5}],
    }
    service.consumers.result = {
        "residual_baseline": residual,
        "devices": [
            {"entity_id": entity, "forecast": forecast, "baseline_forecast": forecast}
            for entity in (ENTITY, other)
        ],
    }
    problem = InstallationProblem(
        tuple(
            Interval(NOW + timedelta(minutes=15 * index), 0.25, 0.2, 0, 0.0875)
            for index in range(16)
        ),
        (),
        (),
        (),
        5000,
        5000,
    )
    snapshot = InputSnapshot(
        "snapshot", "configuration", NOW, "measurement", "forecast", "prices", problem
    )
    prepared = service.prepare(snapshot, SimpleNamespace(), NOW)
    assert [work.entity_id for work in prepared.problem.appliances] == [other]
    assert service.state["tasks"][ENTITY]["profile"] == frozen
    component = next(item for item in prepared.load_components if item.entity_id == ENTITY)
    assert component.steps == component.forecast_steps
    assert sum(step.kwh for step in component.steps) == pytest.approx(0.5)
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(1.15)
    service.accept_plan(
        {
            "plan_id": "budget-proof",
            "appliance_schedule": [
                {"task_id": service.state["tasks"][other]["id"], "start": NOW.isoformat()}
            ],
        }
    )
    assert service.state["tasks"][ENTITY]["status"] == "forecast_not_ready"
    assert service.state["tasks"][other]["status"] == "scheduled"
    assert service.document()["devices"][0]["request"] is None
    assert service.issue is None


def test_phase_union_stops_before_integrating_over_budget_shifted_cycles(monkeypatch):
    """Individually valid shifted traces cannot create an oversized learned phase union or quadratic integration."""
    first = CycleProfile(tuple([1000] * 1025), phase_durations_seconds=tuple([1] * 1024 + [776]))
    second = CycleProfile(
        tuple([1000] * 1025), phase_durations_seconds=tuple([1.1] + [1] * 1023 + [775.9])
    )
    calls = []
    original = CycleProfile.energy

    def tracked(self, *arguments):
        """Observe whether bounded admission rejects before expanding all pairwise integrals."""
        calls.append(True)
        return original(self, *arguments)

    monkeypatch.setattr(CycleProfile, "energy", tracked)
    with pytest.raises(ValueError, match="bounded phase complexity"):
        average_profiles(first, second)
    assert not calls
    assert first.power_w == second.power_w == (1000,) * 1025


def test_twenty_second_raw_peak_remains_in_physical_grid_limits_and_savings_replay(service):
    """A 3-kW twenty-second pulse cannot fit a 2-kW grid, while an equal-kWh flat mean would fit."""
    import json
    from dataclasses import replace
    from types import SimpleNamespace

    from open_energy_planner.appliances.cycles.model import instant
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.optimization.adapters.highs.solver import HighsSolver
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot
    from open_energy_planner.savings.context import appliance_schedule
    from tests.app.appliance.raw_support import raw_trace
    from tests.app.appliance.support import ENTITY

    body = raw_trace()
    service.learn_raw_history(body, instant(body["end"]))
    service.configure(ENTITY, True)
    service.submit(ENTITY, "raw-ready", NOW, NOW + timedelta(hours=1), NOW)
    residual = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 1, "kwh": 0.1}],
    }
    forecast = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 1, "kwh": 23 / 45}],
    }
    service.consumers.result = {
        "residual_baseline": residual,
        "devices": [{"entity_id": ENTITY, "forecast": forecast, "baseline_forecast": forecast}],
    }
    original = InstallationProblem(
        (Interval(NOW, 1, 0.2, 0, 0.1 + 23 / 45),), (), (), (), 2000, 2000
    )
    snapshot = InputSnapshot(
        "snapshot", "configuration", NOW, "measurement", "forecast", "prices", original
    )
    prepared = service.prepare(snapshot, SimpleNamespace(), NOW)
    [work] = prepared.problem.appliances
    assert work.profile.power_w == (1000, 3000, 1000)
    assert any(step.duration_h == pytest.approx(20 / 3600) for step in prepared.problem.intervals)
    assert HighsSolver().solve(prepared.problem).plan.appliance_starts == ()
    flattened = replace(
        prepared.problem, appliances=(replace(work, profile=CycleProfile((46000 / 45,) * 2)),)
    )
    assert HighsSolver().solve(flattened).plan.appliance_starts
    frozen = json.loads(json.dumps(asdict(work), default=lambda value: value.isoformat()))
    plan = {
        "input_snapshot": {
            "problem": {
                "intervals": [{"start": NOW.isoformat(), "duration_h": 1}],
                "appliances": [frozen],
            }
        },
        "appliance_schedule": [{"task_id": work.task_id, "start": NOW.isoformat()}],
    }
    [replay] = appliance_schedule(plan)
    assert replay["interval_kwh"] == pytest.approx([23 / 45])
    assert replay["end"] == (NOW + timedelta(minutes=30)).isoformat()
