"""Explicit evidence and intercepted readiness survive refresh, physical feedback and restart."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.service import ApplianceService, profile
from tests.app.appliance.test_appliance_learning import ENTITY, NOW, POWER, readings
from tests.app.appliance.test_cycle_refinement import complete_running_readings, running_readings


def one_kwh_readings(*, cycles=1):
    """Scale the independently specified .5-kWh cycles to exactly 1 kWh each."""
    rows = readings(cycles=cycles)
    origin = rows[0]["kwh"]
    return [
        {**row, "power_w": row["power_w"] * 2, "kwh": origin + (row["kwh"] - origin) * 2}
        for row in rows
    ]


def explicit_three_kwh(service, *, cycle_id="explicit-three"):
    """Record a distinct one-hour, 3-kWh completed energy-counter run without claiming a power sensor."""
    start = NOW - timedelta(days=1)
    points = [
        {"at": (start + timedelta(minutes=minute)).isoformat(), "kwh": minute / 20}
        for minute in (0, 15, 30, 45, 60)
    ]
    service.record_cycle(ENTITY, cycle_id, start, start + timedelta(hours=1), points)


def test_explicit_completion_remains_in_model_after_empty_refresh_restart_and_recovery(service):
    """Detected 1 kWh plus explicit 3 kWh stays 2 kWh, then a new 1-kWh cycle gives 5/3 without changing accepted work."""
    service.learn_history(ENTITY, POWER, one_kwh_readings(), False, NOW)
    explicit_three_kwh(service)
    before = deepcopy(service.state["devices"][ENTITY]["profile"])
    assert profile(before).energy_kwh == pytest.approx(2)
    assert before["samples"] == 2
    service.configure(ENTITY, True)
    task = service.submit(ENTITY, "frozen-two-kwh", NOW, NOW + timedelta(hours=4), NOW)
    frozen = json.loads(json.dumps(task["profile"]))
    for _ in range(2):
        service.learn_history(ENTITY, POWER, [], False, NOW)
        service = ApplianceService(service.documents, service.consumers)
        assert service.state["devices"][ENTITY]["profile"] == json.loads(json.dumps(before))
        assert service.state["tasks"][ENTITY]["profile"] == frozen
    explicit_three_kwh(service)
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 2
    service.learn_history(ENTITY, POWER, one_kwh_readings(cycles=2), False, NOW)
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(5 / 3)
    assert service.state["tasks"][ENTITY]["profile"] == frozen
    [raw] = list(service.documents.iter_prefix("appliance-cycle-"))
    assert raw["entity_id"] == ENTITY
    assert "power_entity" not in raw


def issued_task(service, *, expire):
    """Create a real requested attempt one hour ago, optionally letting its two-minute lease expire."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    service.configure(ENTITY, True)
    issued = NOW - timedelta(hours=1)
    task = service.submit(ENTITY, "original-readiness", issued, NOW + timedelta(hours=6), issued)
    plan = {
        "plan_id": "original",
        "valid_until": (NOW + timedelta(hours=4)).isoformat(),
        "appliance_schedule": [{"task_id": task["id"], "start": issued.isoformat()}],
    }
    service.accept_plan(plan)
    service.tick(issued, True, plan)
    attempt = deepcopy(service.state["tasks"][ENTITY]["attempt"])
    if expire:
        service.tick(NOW, True, plan)
    return task["id"], attempt


@pytest.mark.parametrize(
    "feedback_order", ["before_activity", "after_activity", "after_completion"]
)
@pytest.mark.parametrize("expire", [False, True])
def test_interception_cannot_reopen_same_readiness_after_nonexecution_feedback(
    service, feedback_order, expire
):
    """Either feedback ordering retains intercepted work through closing quiet, restart and stale rescheduling."""
    task_id, attempt = issued_task(service, expire=expire)

    def not_started(now):
        """Report genuine authenticated nonexecution of the old attempt without acknowledging the manual run."""
        service.feedback(
            ENTITY,
            task_id,
            attempt["id"],
            "not_started",
            instant(attempt["issued_at"]),
            now,
            cancelled=True,
        )

    if feedback_order == "before_activity":
        not_started(NOW)
    service.learn_history(ENTITY, POWER, running_readings(), False, NOW)
    if feedback_order != "before_activity":
        assert service.state["tasks"][ENTITY]["events"] == []
        assert service.state["tasks"][ENTITY]["attempt"]["status"] == (
            "unknown" if expire else "requested"
        )
    if feedback_order == "after_activity":
        not_started(NOW)
    service.learn_history(
        ENTITY, POWER, complete_running_readings(), False, NOW + timedelta(minutes=90)
    )
    later = NOW + timedelta(minutes=90)
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "complete"
    if feedback_order == "after_completion":
        not_started(later)
    service = ApplianceService(service.documents, service.consumers)
    retry = {
        "plan_id": "stale-reschedule",
        "valid_until": (later + timedelta(hours=1)).isoformat(),
        "appliance_schedule": [{"task_id": task_id, "start": later.isoformat()}],
    }
    service.accept_plan(retry)
    service.tick(later, True, retry)
    assert service.document(later, True, retry)["devices"][0]["request"] is None
    assert service.state["tasks"][ENTITY]["status"] == "execution_observed"
    with pytest.raises(ValueError):
        service.claim(ENTITY, task_id, attempt["id"], later, True, retry)
    with pytest.raises(ValueError, match="unresolved task"):
        service.submit(ENTITY, "second-readiness", later, later + timedelta(hours=3), later)
    service.cancel_task(ENTITY, task_id)
    replacement = service.submit(
        ENTITY, "replacement-readiness", later, later + timedelta(hours=3), later
    )
    assert replacement["id"] != task_id


def test_expiry_and_authenticated_completion_do_not_clear_interception(service):
    """Expiry preserves unknown and late physical feedback remains truthful without completing the intercepted readiness."""
    task_id, attempt = issued_task(service, expire=False)
    service.learn_history(ENTITY, POWER, running_readings(), False, NOW)
    service.tick(NOW, True, None)
    assert service.state["tasks"][ENTITY]["attempt"]["status"] == "unknown"
    assert service.state["tasks"][ENTITY]["events"] == []
    service.learn_history(
        ENTITY, POWER, complete_running_readings(), False, NOW + timedelta(minutes=90)
    )
    for event, at in (
        ("started", NOW - timedelta(minutes=45)),
        ("completed", NOW + timedelta(minutes=30)),
    ):
        service.feedback(ENTITY, task_id, attempt["id"], event, at, NOW + timedelta(minutes=90))
    assert service.state["tasks"][ENTITY]["status"] == "execution_observed"
    assert service.state["tasks"][ENTITY]["attempt"]["status"] == "completed"
    assert [event["event"] for event in service.state["tasks"][ENTITY]["events"]] == [
        "started",
        "completed",
    ]


def test_late_feedback_for_cancelled_interception_does_not_modify_replacement(service):
    """Old authenticated outcomes update only the cancelled task archive after new explicit readiness."""
    task_id, attempt = issued_task(service, expire=True)
    service.learn_history(ENTITY, POWER, running_readings(), False, NOW)
    service.learn_history(
        ENTITY, POWER, complete_running_readings(), False, NOW + timedelta(minutes=90)
    )
    service.cancel_task(ENTITY, task_id)
    later = NOW + timedelta(minutes=90)
    replacement = service.submit(ENTITY, "replacement", later, later + timedelta(hours=3), later)
    expected = deepcopy(replacement)
    old = service.feedback(
        ENTITY,
        task_id,
        attempt["id"],
        "not_started",
        instant(attempt["issued_at"]),
        later,
        cancelled=True,
    )
    assert old["status"] == "cancelled"
    assert service.state["tasks"][ENTITY] == expected


def test_explicit_alias_and_detected_view_of_same_run_count_once(service):
    """Exact identity retries and overlapping explicit/detected views keep all originals but one sample."""
    from open_energy_planner.appliances.cycles.learning import detect_cycles

    rows = one_kwh_readings()
    service.learn_history(ENTITY, POWER, rows, False, NOW)
    [(start, end, points)] = detect_cycles(rows, NOW)
    for cycle_id in ("same-run", "same-run", "same-run-alias"):
        service.record_cycle(ENTITY, cycle_id, start, end, points)
    restored = ApplianceService(service.documents, service.consumers)
    restored.learn_history(ENTITY, POWER, rows, False, NOW)
    selected = restored.state["devices"][ENTITY]["profile"]
    assert selected["samples"] == 1
    assert profile(selected).energy_kwh == pytest.approx(1)
    assert len(list(restored.documents.iter_prefix("appliance-cycle-"))) == 2
    assert len(list(restored.documents.iter_prefix("appliance-detected-"))) == 1


def test_energy_source_change_excludes_old_explicit_records(service):
    """A different selected energy meter cannot inherit another meter's explicit 3-kWh completion."""
    explicit_three_kwh(service)
    other = "sensor.other_energy"
    other_power = "sensor.other_power"
    service.consumers.sync(
        {
            "entities": [{"entity_id": other, "name": "Other appliance"}],
            "suggestions": {
                "consumers": [{"entity_id": other, "upstream": None, "power_entity": other_power}]
            },
        }
    )
    service.consumers.save(
        [{"entity_id": other, "weather_sensitive": False}], service.consumers.version
    )
    service.learn_history(other, other_power, one_kwh_readings(), False, NOW)
    [row] = service.document()["devices"]
    assert row["entity_id"] == other and row["profile"]["samples"] == 1
    assert profile(row["profile"]).energy_kwh == pytest.approx(1)
    assert len(list(service.documents.iter_prefix("appliance-cycle-"))) == 1


def test_power_relink_excludes_old_automatic_pair_without_inventing_explicit_provenance(service):
    """The same energy meter retains its explicit 3-kWh completion, while obsolete automatic 1-kWh power evidence is withdrawn."""
    service.learn_history(ENTITY, POWER, one_kwh_readings(), False, NOW)
    explicit_three_kwh(service)
    new_power = "sensor.relinked_power"
    service.consumers.options = (
        {"entity_id": ENTITY, "name": "Washer", "power_entity": new_power},
    )
    service.sync_sources()
    with pytest.raises(ValueError, match="current Home Assistant"):
        service.learn_history(ENTITY, POWER, [], False, NOW)
    service.learn_history(ENTITY, new_power, [], False, NOW)
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(3)
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    [raw] = list(service.documents.iter_prefix("appliance-cycle-"))
    assert "power_entity" not in raw


def test_unreadable_explicit_points_do_not_replace_profile_or_poison_retry(service, monkeypatch):
    """A damaged explicit record rejects refinement and a repaired read can recover in the same service instance."""
    service.learn_history(ENTITY, POWER, one_kwh_readings(), False, NOW)
    explicit_three_kwh(service)
    restored = ApplianceService(service.documents, service.consumers)
    before = deepcopy(restored.state)
    original = restored.documents.iter_prefix

    def invalid_points(prefix):
        """Represent a decoded explicit archive with no usable original measurements."""
        for row in original(prefix):
            yield {**row, "points": []} if prefix == "appliance-cycle-" else row

    monkeypatch.setattr(restored.documents, "iter_prefix", invalid_points)
    with pytest.raises(ValueError, match="Fine cumulative energy history"):
        restored.learn_history(ENTITY, POWER, [], False, NOW)
    assert restored.state == before
    monkeypatch.setattr(restored.documents, "iter_prefix", original)
    restored.learn_history(ENTITY, POWER, [], False, NOW)
    assert profile(restored.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(2)


def test_explicit_model_write_failure_rolls_back_archive_cache_and_keeps_frozen_task(
    service, monkeypatch
):
    """A failure after inserting explicit evidence commits neither a 3-kWh contribution nor a changed task."""
    service.learn_history(ENTITY, POWER, one_kwh_readings(), False, NOW)
    service.configure(ENTITY, True)
    task = service.submit(ENTITY, "accepted-one", NOW, NOW + timedelta(hours=4), NOW)
    frozen = deepcopy(task["profile"])
    before = deepcopy(service.state)
    original = service.documents.write

    def fail(name, value):
        """Reject the state pointer after the transaction has attempted to archive the explicit run."""
        if name == "appliance-state.json":
            raise OSError("Synthetic explicit model outage")
        original(name, value)

    monkeypatch.setattr(service.documents, "write", fail)
    with pytest.raises(OSError):
        explicit_three_kwh(service)
    assert service.state == before
    assert not list(service.documents.iter_prefix("appliance-cycle-"))
    monkeypatch.setattr(service.documents, "write", original)
    service.learn_history(ENTITY, POWER, [], False, NOW)
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(1)
    explicit_three_kwh(service)
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(2)
    assert service.state["tasks"][ENTITY]["profile"] == frozen


def test_normally_confirmed_started_cycle_does_not_latch_as_intercepted(service):
    """Timely authenticated start feedback keeps normal completion and permits later readiness."""
    task_id, attempt = issued_task(service, expire=False)
    service.feedback(ENTITY, task_id, attempt["id"], "started", NOW - timedelta(minutes=45), NOW)
    service.learn_history(ENTITY, POWER, running_readings(), False, NOW)
    assert service.state["tasks"][ENTITY]["status"] == "running"
    service.learn_history(
        ENTITY, POWER, complete_running_readings(), False, NOW + timedelta(minutes=90)
    )
    later = NOW + timedelta(minutes=90)
    service.feedback(
        ENTITY, task_id, attempt["id"], "completed", NOW + timedelta(minutes=30), later
    )
    assert service.state["tasks"][ENTITY]["status"] == "completed"
    assert (
        service.submit(ENTITY, "next-normal-cycle", later, later + timedelta(hours=3), later)["id"]
        != task_id
    )


def test_explicit_profile_keeps_precedence_over_only_hourly_estimates(service):
    """A known explicit 3-kWh cycle supersedes coarse estimates, preserving the existing completed-cycle API contract."""
    from tests.app.appliance.test_history_learning import hourly_rows

    service.learn_history(
        ENTITY,
        POWER,
        hourly_rows(NOW - timedelta(days=10), [0, 0, 500, 0, 0, 1000, 0]),
        False,
        NOW,
        sample_seconds=3600,
    )
    explicit_three_kwh(service)
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(3)
    service.learn_history(ENTITY, POWER, [], False, NOW)
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(3)
    assert service.state["devices"][ENTITY]["profile_sources"] == {
        "hourly_cycles": 0,
        "fine_cycles": 1,
    }


def relink(service, power):
    """Change the selected source and run the real withdrawal without uploading history."""
    service.consumers.options = ({"entity_id": ENTITY, "name": "Washer", "power_entity": power},)
    service.sync_sources()


def test_explicit_reconstruction_rebinds_automatic_evidence_before_another_relink(service):
    """A->B->A->explicit reconstruction->B removes the old 1-kWh contribution without a history pass."""
    service.learn_history(ENTITY, POWER, one_kwh_readings(), False, NOW)
    service.configure(ENTITY, True)
    task = service.submit(ENTITY, "frozen-source-a", NOW, NOW + timedelta(hours=4), NOW)
    frozen = deepcopy(task["profile"])
    relink(service, "sensor.other_power")
    relink(service, POWER)
    explicit_three_kwh(service)
    settings = service.state["devices"][ENTITY]
    assert profile(settings["profile"]).energy_kwh == pytest.approx(2)
    relink(service, "sensor.other_power")
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(3)
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    assert "learning_source" not in service.state["devices"][ENTITY]
    assert service.state["tasks"][ENTITY]["profile"] == frozen
    assert len(list(service.documents.iter_prefix("appliance-detected-"))) == 1
    assert len(list(service.documents.iter_prefix("appliance-cycle-"))) == 1


def test_explicit_only_source_stays_unbound_and_unchanged_refreshes_reuse_model(
    service, monkeypatch
):
    """Energy-only 3-kWh evidence survives power relinking without fabricated provenance or repeat rebuilding."""
    from tests.app.appliance.test_history_resume import trace_refinement

    explicit_three_kwh(service)
    calls = trace_refinement(monkeypatch)
    for _ in range(2):
        service.learn_history(ENTITY, POWER, [], False, NOW)
    assert calls == []
    assert "learning_source" not in service.state["devices"][ENTITY]
    relink(service, "sensor.other_power")
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(3)
    [raw] = list(service.documents.iter_prefix("appliance-cycle-"))
    assert "power_entity" not in raw
    service = ApplianceService(service.documents, service.consumers)
    service.learn_history(ENTITY, "sensor.other_power", [], False, NOW)
    service.learn_history(ENTITY, "sensor.other_power", [], False, NOW)
    assert len(calls) == 1
    assert "learning_source" not in service.state["devices"][ENTITY]


def test_explicit_winning_overlap_does_not_inherit_discarded_automatic_power_owner(service):
    """The explicit view wins an equal-detail tie and remains a single energy-only sample after relinking."""
    from open_energy_planner.appliances.cycles.learning import detect_cycles

    rows = one_kwh_readings()
    service.learn_history(ENTITY, POWER, rows, False, NOW)
    [(start, end, points)] = detect_cycles(rows, NOW)
    service.record_cycle(ENTITY, "overlap-explicit", start, end, points)
    assert service.state["devices"][ENTITY]["profile"]["samples"] == 1
    assert "learning_source" not in service.state["devices"][ENTITY]
    relink(service, "sensor.other_power")
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(1)


def test_explicit_reconstruction_keeps_retained_coarse_summary_source_bound(service):
    """Restored hourly aggregates keep their actual source even when a known explicit profile takes precedence."""
    from tests.app.appliance.test_history_learning import hourly_rows

    service.learn_history(
        ENTITY,
        POWER,
        hourly_rows(NOW - timedelta(days=10), [0, 0, 1000, 0, 0]),
        False,
        NOW,
        sample_seconds=3600,
    )
    relink(service, "sensor.other_power")
    relink(service, POWER)
    explicit_three_kwh(service)
    settings = service.state["devices"][ENTITY]
    assert profile(settings["profile"]).energy_kwh == pytest.approx(3)
    assert profile(settings["hourly_profile"]).energy_kwh == pytest.approx(1)
    assert settings.get("learning_source") == POWER
    relink(service, "sensor.other_power")
    assert "hourly_profile" not in service.state["devices"][ENTITY]
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(3)
    assert "learning_source" not in service.state["devices"][ENTITY]
    assert "power_entity" not in next(service.documents.iter_prefix("appliance-cycle-"))


def test_failed_source_withdrawal_keeps_state_and_cache_until_explicit_only_rebuild_commits(
    service, monkeypatch
):
    """A failed relink cannot publish withdrawal or invalidate cached evidence. Retry retains only explicit 3 kWh."""
    service.learn_history(ENTITY, POWER, one_kwh_readings(), False, NOW)
    explicit_three_kwh(service)
    before, cache = deepcopy(service.state), deepcopy(service._cycle_records)
    original = service.documents.write

    def fail_state(name, value):
        """Reject the atomic withdrawal pointer before cache invalidation."""
        if name == "appliance-state.json":
            raise OSError("Synthetic source withdrawal outage")
        original(name, value)

    monkeypatch.setattr(service.documents, "write", fail_state)
    with pytest.raises(OSError):
        relink(service, "sensor.other_power")
    assert service.state == before
    assert service._cycle_records == cache
    monkeypatch.setattr(service.documents, "write", original)
    service.sync_sources()
    assert not service._cycle_records
    assert profile(service.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(3)
    restored = ApplianceService(service.documents, service.consumers)
    assert restored.state == json.loads(json.dumps(service.state))
