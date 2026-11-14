"""Cycle evidence remains usable from the first run and refines without counting a run twice."""

import json
from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles import history
from open_energy_planner.appliances.cycles.service import ApplianceService
from tests.app.appliance.test_appliance_learning import ENTITY, NOW, POWER, readings
from tests.app.appliance.test_history_learning import begin, hourly_rows, upload


def test_first_complete_fine_cycle_is_usable_and_survives_restart(service):
    """One independently specified .5-kWh cycle is enough for a provisional planning profile."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    row = service.document()["devices"][0]
    assert row["profile"]["samples"] == 1
    assert sum(row["profile"]["power_w"]) / 12000 == pytest.approx(0.5)
    restored = ApplianceService(service.documents, service.consumers)
    restored.configure(ENTITY, True)
    task = restored.submit(ENTITY, "first-cycle", NOW, NOW + timedelta(hours=4), NOW)
    assert task["profile"] == json.loads(json.dumps(row["profile"]))


def test_first_hourly_cycle_is_usable_before_the_backfill_finishes(service):
    """A .75-kWh hourly run in the first week is usable while the second week remains pending."""
    first = NOW - timedelta(days=15)
    begin(service, first, NOW - timedelta(hours=1))
    upload(service, hourly_rows(first, [0, 0, 500, 250, 0, 0]))
    row = service.document()["devices"][0]
    assert row["history_import"]["phase"] == "processing"
    assert row["profile"]["samples"] == 1
    assert sum(row["profile"]["power_w"]) / 4000 == pytest.approx(0.75)


def test_fine_observation_replaces_the_same_hourly_cycle_without_double_counting(service):
    """A two-hour coarse .5-kWh range becomes the same 75-minute fine run, not a second sample."""
    first = NOW - timedelta(days=2)
    fine = readings(cycles=1)
    # The fine cycle starts one hour after its quiet bracket and occupies 75 minutes.
    coarse = hourly_rows(first - timedelta(hours=1), [0, 0, 1250 / 3, 250 / 3, 0, 0])
    begin(service, first - timedelta(hours=1), first + timedelta(hours=5))
    upload(service, coarse)
    service.learn_history(ENTITY, POWER, fine, False, NOW)
    row = service.document()["devices"][0]
    assert row["profile"]["samples"] == 1
    assert row["profile_sources"] == {"hourly_cycles": 0, "fine_cycles": 1}
    assert sum(row["profile"]["power_w"]) / 12000 == pytest.approx(0.5)
    assert len(list(service.documents.iter_prefix("appliance-hourly-"))) == 1
    assert len(list(service.documents.iter_prefix("appliance-detected-"))) == 1
    service.learn_history(ENTITY, POWER, fine, False, NOW)
    assert service.document()["devices"][0]["profile"]["samples"] == 1


def test_older_fine_cycles_are_retained_when_home_assistant_has_longer_history(service):
    """A valid run thirty days old remains admissible and is not rejected by a seven-day lookback."""
    rows = readings(cycles=1)
    for row in rows:
        row["at"] = (history.instant(row["at"]) - timedelta(days=30)).isoformat()
    service.learn_history(ENTITY, POWER, rows, False, NOW)
    assert service.document()["devices"][0]["profile"]["samples"] == 1


def test_partial_final_phase_preserves_measured_duration_and_energy():
    """A 32-minute 1-kW run retains its two-minute final phase and 8/15 kWh across averaging and restart."""
    from open_energy_planner.appliances.cycles.model import average_profiles, learn_cycle

    points = [
        {"at": (NOW + timedelta(minutes=minute)).isoformat(), "kwh": minute / 60}
        for minute in range(0, 36, 5)
    ]
    longer = learn_cycle(points, NOW, NOW + timedelta(minutes=32))
    shorter = learn_cycle(points[:5], NOW, NOW + timedelta(minutes=20))
    assert longer.duration_seconds == 1920
    assert longer.phase_seconds == 300
    assert longer.power_w == pytest.approx((1000,) * 7)
    assert longer.energy_kwh == pytest.approx(8 / 15)
    assert longer.energy(
        NOW, NOW + timedelta(minutes=30), NOW + timedelta(hours=1)
    ) == pytest.approx(1 / 30)
    combined = average_profiles(shorter, longer)
    assert combined.duration_seconds == 1920
    assert combined.energy_kwh == pytest.approx(13 / 30)
    from dataclasses import asdict

    from open_energy_planner.appliances.cycles.service import profile

    restored = profile(json.loads(json.dumps(asdict(longer))))
    assert restored.duration_seconds == 1920
    assert restored.energy_kwh == pytest.approx(8 / 15)
    assert restored.energy(
        NOW, NOW + timedelta(minutes=30), NOW + timedelta(hours=1)
    ) == pytest.approx(1 / 30)


def test_fine_backfill_crosses_empty_weeks_and_resumes_after_restart(service):
    """An empty recent week cannot hide a fine cycle thirty days old or duplicate its checkpoint."""
    from open_energy_planner.appliances.cycles.model import instant

    rows = readings(cycles=1)
    for row in rows:
        row["at"] = (instant(row["at"]) - timedelta(days=30)).isoformat()
    first, end = NOW - timedelta(days=40), NOW - timedelta(hours=1)
    history.scan(
        service,
        ENTITY,
        POWER,
        "processing",
        first.isoformat(),
        end.isoformat(),
        NOW,
        resolution="5minute",
    )
    visited = 0
    while pending := history.request(service.state["devices"][ENTITY], resolution="5minute"):
        selected = [
            row
            for row in rows
            if instant(pending["read_start"]) <= instant(row["at"]) < instant(pending["read_end"])
        ]
        history.batch(
            service, ENTITY, POWER, selected, False, **pending, now=NOW, resolution="5minute"
        )
        previous = service.state
        history.batch(service, ENTITY, POWER, [], False, **pending, now=NOW, resolution="5minute")
        assert service.state == previous
        service = ApplianceService(service.documents, service.consumers)
        visited += 1
    assert visited == 6
    row = service.document()["devices"][0]
    assert row["fine_import"]["phase"] == "complete"
    assert row["profile"]["samples"] == 1
    assert len(list(service.documents.iter_prefix("appliance-detected-"))) == 1


def test_failed_evidence_commit_cannot_advance_profile_or_cache(service, monkeypatch):
    """A failed SQL document write rolls back raw evidence and leaves a retry equivalent to a fresh import."""
    original = service.documents.write

    def fail(name, value):
        """Fail after raw evidence insertion to exercise transaction rollback."""
        if name == "appliance-state.json":
            raise OSError("Synthetic model write failure")
        original(name, value)

    monkeypatch.setattr(service.documents, "write", fail)
    with pytest.raises(OSError):
        service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    assert not list(service.documents.iter_prefix("appliance-detected-"))
    assert not service._cycle_records.get((ENTITY, POWER))
    monkeypatch.setattr(service.documents, "write", original)
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    assert service.document()["devices"][0]["profile"]["samples"] == 1


def running_readings():
    """Expose 45 minutes of the known 75-minute cycle, ending exactly at NOW."""
    from open_energy_planner.appliances.cycles.model import instant

    rows = readings(cycles=1)[:21]
    offset = NOW - (instant(rows[-1]["at"]) + timedelta(minutes=5))
    return [{**row, "at": (instant(row["at"]) + offset).isoformat()} for row in rows]


def complete_running_readings():
    """Return the full same run plus sixty quiet minutes, completing ninety minutes after NOW."""
    from open_energy_planner.appliances.cycles.model import instant

    rows = readings(cycles=1)
    offset = instant(running_readings()[0]["at"]) - instant(rows[0]["at"])
    return [{**row, "at": (instant(row["at"]) + offset).isoformat()} for row in rows]


def test_observed_cycle_blocks_pending_start_without_forging_execution_feedback(service):
    """A manual start blocks the accepted task permanently until cancellation, even after measured completion."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    service.configure(ENTITY, True)
    task = service.submit(ENTITY, "scheduled", NOW, NOW + timedelta(hours=4), NOW)
    service.learn_history(ENTITY, POWER, running_readings(), False, NOW)
    row = service.document()["devices"][0]
    assert row["observed_run"]["status"] == "observed"
    assert row["task"]["status"] == "execution_observed"
    assert row["task"]["attempt"] is None and row["task"]["events"] == []
    plan = {
        "plan_id": "next",
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
        "appliance_schedule": [{"task_id": task["id"], "start": NOW.isoformat()}],
    }
    service.accept_plan(plan)
    service.tick(NOW, True, plan)
    assert service.document(NOW, True, plan)["devices"][0]["request"] is None
    service.learn_history(
        ENTITY, POWER, complete_running_readings(), False, NOW + timedelta(minutes=90)
    )
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "complete"
    restored = ApplianceService(service.documents, service.consumers)
    assert restored.document()["devices"][0]["task"]["status"] == "execution_observed"
    assert (
        restored.document(NOW + timedelta(minutes=90), True, plan)["devices"][0]["request"] is None
    )
    with pytest.raises(ValueError, match="unresolved task"):
        restored.submit(
            ENTITY,
            "after-completion",
            NOW + timedelta(minutes=90),
            NOW + timedelta(hours=4),
            NOW + timedelta(minutes=90),
        )
    restored.cancel_task(ENTITY, task["id"])
    assert restored.state["tasks"][ENTITY]["status"] == "cancelled"


@pytest.mark.parametrize("attempt_status", [None, "unknown", "not_started"])
def test_observed_cycle_enters_short_term_plan_once_as_fixed_remaining_load(
    service, attempt_status
):
    """The remaining half-hour consumes 1/6 kWh at its measured start, replacing the same forecast share."""
    from types import SimpleNamespace

    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.installation.validation import validate
    from open_energy_planner.optimization.adapters.highs.solver import HighsSolver
    from open_energy_planner.optimization.domain import Interval, SolveOptions, Strategy
    from open_energy_planner.planning import InputSnapshot

    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    if attempt_status:
        before = NOW - timedelta(hours=1)
        service.configure(ENTITY, True)
        task = service.submit(ENTITY, "old-readiness", before, NOW + timedelta(hours=3), before)
        plan = {
            "plan_id": "old-plan",
            "valid_until": (NOW + timedelta(hours=1)).isoformat(),
            "appliance_schedule": [{"task_id": task["id"], "start": before.isoformat()}],
        }
        service.accept_plan(plan)
        service.tick(before, True, plan)
        attempt = service.state["tasks"][ENTITY]["attempt"]
        if attempt_status == "unknown":
            service.tick(NOW, True, plan)
        else:
            service.feedback(
                ENTITY, task["id"], attempt["id"], "not_started", before, NOW, cancelled=True
            )
    service.learn_history(ENTITY, POWER, running_readings(), False, NOW)
    if attempt_status == "unknown":
        assert service.state["tasks"][ENTITY]["attempt"]["status"] == "unknown"
        assert service.state["tasks"][ENTITY]["events"] == []
    elif attempt_status == "not_started":
        assert service.state["tasks"][ENTITY]["status"] == "execution_observed"
        assert service.state["tasks"][ENTITY]["attempt"]["status"] == "not_started"
    steps = [{"start": NOW.isoformat(), "duration_h": 1, "kwh": 0.3}]
    residual = {"issued_at": NOW.isoformat(), "steps": steps}
    forecast = {"issued_at": NOW.isoformat(), "steps": [{**steps[0], "kwh": 1 / 6}]}
    service.consumers.result = {
        "residual_baseline": residual,
        "devices": [{"entity_id": ENTITY, "forecast": forecast, "baseline_forecast": forecast}],
    }
    original = InstallationProblem((Interval(NOW, 1, 0.2, 0, 0.3 + 1 / 6),), (), (), (), 3000, 3000)
    snapshot = InputSnapshot(
        "snapshot", "configuration", NOW, "measurement", "forecast", "prices", original
    )
    prepared = service.prepare(snapshot, SimpleNamespace(), NOW)
    assert service.issue is None
    [work] = prepared.problem.appliances
    assert work.fixed_start == NOW - timedelta(minutes=45)
    assert work.profile.energy(work.fixed_start, NOW, NOW + timedelta(hours=1)) == pytest.approx(
        1 / 6
    )
    # Only the expected share inside the actual remaining half-hour can be replaced.
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(0.3 + 1 / 12)
    result = HighsSolver().solve(prepared.problem, SolveOptions(strategy=Strategy.COST))
    validate(prepared.problem, result.plan)
    assert dict(result.plan.appliance_starts)[work.task_id] == work.fixed_start
    assert sum(flow.grid_import for flow in result.plan.flows) == pytest.approx(0.55)


def test_unknown_measurements_never_complete_a_run_and_fresh_quiet_only_establishes_idle(service):
    """A missing interval leaves execution uncertain. Thirty quiet minutes clear activity without a new sample."""
    from open_energy_planner.appliances.cycles.model import instant

    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    rows = running_readings()
    service.learn_history(ENTITY, POWER, rows, False, NOW)
    rows.append({"at": NOW.isoformat(), "power_w": None, "kwh": None})
    service.learn_history(ENTITY, POWER, rows, False, NOW + timedelta(minutes=5))
    assert service.observed(ENTITY)["status"] == "uncertain"
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    for index in range(1, 9):
        rows.append(
            {"at": (NOW + timedelta(minutes=index * 5)).isoformat(), "power_w": 0, "kwh": 11}
        )
    service.learn_history(
        ENTITY, POWER, rows, False, instant(rows[-1]["at"]) + timedelta(minutes=5)
    )
    assert service.observed(ENTITY) is None
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "idle"
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    assert not service.state["tasks"]


def test_stale_activity_retains_forecast_and_relinked_source_cannot_block_new_readiness(service):
    """A stale run supplies no invented remaining load, and evidence belongs to its original sensor pair."""
    from open_energy_planner.appliances.cycles.observed import work

    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    service.learn_history(ENTITY, POWER, running_readings(), False, NOW)
    assert work(ENTITY, service.observed(ENTITY), NOW + timedelta(minutes=11)) is None
    service.consumers.options = (
        {"entity_id": ENTITY, "name": "Washer", "power_entity": "sensor.new_power"},
    )
    service.sync_sources()
    assert service.observed(ENTITY) is None
    assert "fine_import" not in service.state["devices"][ENTITY]
    assert "profile" not in service.state["devices"][ENTITY]


def test_equal_hourly_and_fine_bounds_keep_both_archives_and_count_once_after_restart(service):
    """Resolution-qualified identities survive restart even when one-hour bounds have the same hash."""
    from open_energy_planner.appliances.cycles.model import instant

    rows = readings(cycles=1)
    # A full hour at 500 W has .5 kWh and the exact same coarse/fine boundaries.
    start = instant(rows[0]["at"])
    total = 0
    fine = []
    for index in range(36):
        power = 500 if 12 <= index < 24 else 0
        total += power / 12000
        fine.append(
            {
                "at": (start + timedelta(minutes=5 * index)).isoformat(),
                "power_w": power,
                "kwh": total,
            }
        )
    service.learn_history(
        ENTITY,
        POWER,
        hourly_rows(start - timedelta(hours=1), [0, 0, 500, 0]),
        False,
        NOW,
        sample_seconds=3600,
    )
    service.learn_history(ENTITY, POWER, fine, False, NOW)
    restored = ApplianceService(service.documents, service.consumers)
    restored.learn_history(ENTITY, POWER, fine, False, NOW)
    row = restored.document()["devices"][0]
    assert row["profile"]["samples"] == 1
    assert row["profile_sources"] == {"hourly_cycles": 0, "fine_cycles": 1}
    [hourly] = list(restored.documents.iter_prefix("appliance-hourly-"))
    [measured] = list(restored.documents.iter_prefix("appliance-detected-"))
    assert hourly["id"] == measured["id"]


def test_coarse_energy_cannot_force_fine_timing_beyond_the_power_boundary(service):
    """A 120-kWh coarse run cannot scale a 75-minute .5-kWh shape above the 20-kW physical limit."""
    coarse = hourly_rows(NOW - timedelta(days=10), [0, 0] + [20000] * 6 + [0])
    service.learn_history(ENTITY, POWER, coarse, False, NOW, sample_seconds=3600)
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    row = service.document()["devices"][0]
    assert row["profile"]["samples"] == 1
    assert row["profile_sources"] == {"hourly_cycles": 0, "fine_cycles": 1}
    assert sum(row["profile"]["power_w"]) / 12000 == pytest.approx(0.5)
    assert len(list(service.documents.iter_prefix("appliance-hourly-"))) == 1


def test_revised_overlapping_coarse_views_count_once_without_discarding_originals(service):
    """Two overlapping .5-kWh counter views retain both archives and one independent cycle sample."""
    start = NOW - timedelta(days=10)
    for powers in ([0, 0, 250, 250, 0], [0, 0, 0, 500, 0]):
        service.learn_history(
            ENTITY, POWER, hourly_rows(start, powers), False, NOW, sample_seconds=3600
        )
    row = service.document()["devices"][0]
    assert row["profile"]["samples"] == 1
    assert sum(row["profile"]["power_w"]) / 4000 == pytest.approx(0.5)
    assert len(list(service.documents.iter_prefix("appliance-hourly-"))) == 2


def test_incomplete_restored_evidence_cannot_reduce_an_accepted_model(service, monkeypatch):
    """A missing archived cycle after restart rejects rebuilding instead of silently reducing the saved model."""
    service.learn_history(ENTITY, POWER, readings(), False, NOW)
    restored = ApplianceService(service.documents, service.consumers)
    before = restored.document()
    original = restored.documents.iter_prefix

    def missing_archive(prefix):
        """Expose a damaged archive containing one fewer fine record than its durable model."""
        rows = list(original(prefix))
        yield from rows[1:] if prefix == "appliance-detected-" else rows

    monkeypatch.setattr(restored.documents, "iter_prefix", missing_archive)
    with pytest.raises(ValueError, match="Archived cycle evidence is incomplete"):
        restored.learn_history(ENTITY, POWER, readings(cycles=3), False, NOW)
    assert restored.document() == before


def test_five_minute_peak_is_not_hidden_by_the_quarter_hour_average(service):
    """A 3-kW five-minute pulse cannot use a 2-kW grid, even though its quarter-hour mean is only 1 kW."""
    from dataclasses import replace
    from types import SimpleNamespace

    from open_energy_planner.appliances.cycles.model import CycleProfile
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.optimization.adapters.highs.solver import HighsSolver
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot

    first = NOW - timedelta(days=1)
    points = [
        {"at": (first + timedelta(minutes=minute)).isoformat(), "kwh": energy}
        for minute, energy in ((0, 0), (5, 0.25), (10, 0.25), (15, 0.25))
    ]
    service.record_cycle(ENTITY, "measured-pulse", first, first + timedelta(minutes=15), points)
    service.configure(ENTITY, True)
    service.submit(ENTITY, "ready-for-pulse", NOW, NOW + timedelta(hours=1), NOW)
    residual = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 1, "kwh": 0.1}],
    }
    forecast = {
        "issued_at": NOW.isoformat(),
        "steps": [{"start": NOW.isoformat(), "duration_h": 1, "kwh": 0.25}],
    }
    service.consumers.result = {
        "residual_baseline": residual,
        "devices": [{"entity_id": ENTITY, "forecast": forecast, "baseline_forecast": forecast}],
    }
    original = InstallationProblem((Interval(NOW, 1, 0.2, 0, 0.35),), (), (), (), 2000, 2000)
    snapshot = InputSnapshot(
        "snapshot", "configuration", NOW, "measurement", "forecast", "prices", original
    )
    prepared = service.prepare(snapshot, SimpleNamespace(), NOW)
    [work] = prepared.problem.appliances
    assert work.profile.phase_seconds == 300
    assert work.profile.energy_kwh == pytest.approx(0.25)
    assert all(step.duration_h <= 1 / 12 + 1e-12 for step in prepared.problem.intervals)
    assert HighsSolver().solve(prepared.problem).plan.appliance_starts == ()
    averaged = replace(prepared.problem, appliances=(replace(work, profile=CycleProfile((1000,))),))
    assert HighsSolver().solve(averaged).plan.appliance_starts
