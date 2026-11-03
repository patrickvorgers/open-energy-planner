"""Deterministic execution tests: fixed input documents and a controlled clock."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.open_energy_planner.protocol import Plan, RevokedPlan, Schedule
from tests.ha_contract.support.commands import command_plan

NOW = datetime(2026, 10, 19, tzinfo=UTC)


def test_invalidation_rejects_same_and_older_generations_until_a_new_plan_arrives():
    """Revoke a plan repeatedly. Reject equal/older generations under any ID and activate only a newer valid plan."""
    schedule = Schedule()
    original = command_plan(NOW)
    schedule.accept(original, NOW)
    schedule.invalidate()
    schedule.invalidate()
    for document in (original, command_plan(NOW), command_plan(NOW - timedelta(seconds=1))):
        with pytest.raises(ValueError, match="cannot be reactivated"):
            schedule.accept(document, NOW)
        assert schedule.project(NOW).command is None
    later = NOW + timedelta(seconds=1)
    assert schedule.accept(command_plan(later), later)
    assert schedule.project(later).status == "active"


def test_exact_boundaries_and_distinct_zero_unknown():
    """Advance a fixed clock across steps. Require half-open boundaries and distinct fixed-zero/grid-following values."""
    schedule_item = Schedule()
    schedule_item.accept(command_plan(NOW), NOW)
    assert schedule_item.project(NOW).command.devices[0].power_w == -1000
    follow = schedule_item.project(NOW + timedelta(seconds=30)).command
    assert follow.devices[0].mode == "follow_grid_target" and follow.devices[0].power_w is None
    assert schedule_item.project(NOW + timedelta(seconds=60)).command.devices[0].power_w == 0
    assert schedule_item.project(NOW + timedelta(seconds=90)).status == "expired"


def test_replacement_removes_old_future_transitions():
    """Accept a replacement before an old boundary. Project only the new schedule after that boundary."""
    schedule_item = Schedule()
    schedule_item.accept(command_plan(NOW), NOW)
    observed_at = NOW + timedelta(seconds=10)
    schedule_item.accept(command_plan(observed_at, powers=(250, 250, 250), seconds=50), observed_at)
    assert schedule_item.project(NOW + timedelta(seconds=31)).command.devices[0].power_w == 250


def test_identical_command_is_equal_despite_new_plan():
    """Replace a plan with identical current values. Require equal projection so plan identity alone causes no command change."""
    schedule_item = Schedule()
    schedule_item.accept(command_plan(NOW), NOW)
    old = schedule_item.project(NOW)
    observed_at = NOW + timedelta(seconds=1)
    schedule_item.accept(command_plan(observed_at), observed_at)
    assert old == schedule_item.project(observed_at)


def test_pause_insight_and_fresh_resume():
    """Pause while accepting insight updates. Keep commands disabled until explicit resume receives a newer plan."""
    schedule_item = Schedule()
    schedule_item.accept(command_plan(NOW), NOW)
    schedule_item.pause(NOW + timedelta(seconds=1))
    schedule_item.accept(command_plan(NOW + timedelta(seconds=2)), NOW + timedelta(seconds=2))
    assert schedule_item.project(NOW + timedelta(seconds=3)).status == "paused"
    schedule_item.resume(NOW + timedelta(seconds=4))
    assert schedule_item.project(NOW + timedelta(seconds=4)).status == "waiting_for_new_plan"
    schedule_item.accept(command_plan(NOW + timedelta(seconds=5)), NOW + timedelta(seconds=5))
    assert schedule_item.project(NOW + timedelta(seconds=5)).status == "active"


@pytest.mark.parametrize(
    "mutation",
    [
        "release",
        "missing_end",
        "gap",
        "overlap",
        "nan",
        "boolean_power",
        "conflicting_mode",
        "invalid_strategy",
        "bad_status",
        "naive",
        "fractional_watts",
        "boolean_schema",
        "old_schema",
        "missing_devices",
        "validity",
        "empty",
    ],
)
def test_bad_document_does_not_replace_good_plan(mutation):
    """Reject corrupted wire fields without replacing a valid plan or treating malformed data as normal revoked-plan recovery."""
    schedule_item = Schedule()
    good = command_plan(NOW)
    schedule_item.accept(good, NOW)
    bad = deepcopy(good)
    if mutation == "release":
        bad["release"] = "different"
    elif mutation == "missing_end":
        del bad["steps"][-1]["end"]
    elif mutation == "gap":
        bad["steps"][1]["start"] = (NOW + timedelta(seconds=31)).isoformat()
    elif mutation == "overlap":
        bad["steps"][1]["start"] = (NOW + timedelta(seconds=29)).isoformat()
    elif mutation == "nan":
        bad["steps"][0]["devices"]["battery"]["power_w"] = float("nan")
    elif mutation == "boolean_power":
        bad["steps"][0]["devices"]["battery"]["power_w"] = True
    elif mutation == "conflicting_mode":
        bad["steps"][1]["devices"]["battery"]["power_w"] = 5
    elif mutation == "invalid_strategy":
        bad["strategy"] = "automatic"
    elif mutation == "bad_status":
        bad["solution_status"] = "infeasible"
    elif mutation == "fractional_watts":
        bad["steps"][0]["devices"]["battery"]["power_w"] = 1.5
    elif mutation == "boolean_schema":
        bad["schema"] = True
    elif mutation == "old_schema":
        bad["schema"] = 1
    elif mutation == "missing_devices":
        del bad["steps"][0]["devices"]
    elif mutation == "naive":
        bad["generated_at"] = "2026-10-19T00:00:00"
    elif mutation == "validity":
        bad["valid_until"] = (NOW + timedelta(seconds=100)).isoformat()
    else:
        bad["steps"] = []
    with pytest.raises(ValueError):
        schedule_item.accept(bad, NOW)
    assert schedule_item.document == good and schedule_item.project(NOW).status == "active"
    schedule_item.invalidate()
    with pytest.raises(ValueError) as failure:
        schedule_item.accept(bad, NOW)
    assert not isinstance(failure.value, RevokedPlan)


def test_duplicate_id_is_idempotent_but_modified_id_is_rejected():
    """Replay an identical ID/document harmlessly. Reject the same ID with changed command contents."""
    schedule_item = Schedule()
    plan_document = command_plan(NOW)
    schedule_item.accept(plan_document, NOW)
    assert not schedule_item.accept(plan_document, NOW)
    bad = deepcopy(plan_document)
    bad["steps"][0]["devices"]["battery"]["power_w"] = 100
    with pytest.raises(ValueError):
        schedule_item.accept(bad, NOW)


def test_delayed_plan_cannot_restore_old_schedule():
    """Receive an older plan after a newer acceptance. Reject the delayed schedule rather than rolling back commands."""
    schedule_item = Schedule()
    old = command_plan(NOW)
    observed_at = NOW + timedelta(seconds=2)
    schedule_item.accept(command_plan(observed_at), observed_at)
    with pytest.raises(ValueError):
        schedule_item.accept(old, observed_at)


def test_age_expiry_separate_from_horizon():
    """Use a short freshness limit inside a longer plan. Become stale at the age deadline before horizon expiry."""
    schedule_item = Schedule(max_age_seconds=10)
    schedule_item.accept(command_plan(NOW), NOW)
    assert schedule_item.next_boundary(NOW) == NOW + timedelta(seconds=10)
    assert schedule_item.project(NOW + timedelta(seconds=10)).status == "stale"


def test_wrong_configuration_rejected():
    """Parse a plan for another configuration revision. Reject it before command projection."""
    with pytest.raises(ValueError):
        Plan.parse(command_plan(NOW), "other")


def test_initial_not_ready_and_future_plan_start():
    """Start without coverage or before the first step. Remain not ready instead of applying a future command early."""
    schedule_item = Schedule()
    assert schedule_item.project(NOW).status == "not_ready"
    plan_document = command_plan(NOW)
    plan_document["steps"][0]["start"] = (NOW + timedelta(seconds=1)).isoformat()
    schedule_item.accept(plan_document, NOW)
    assert schedule_item.project(NOW).status == "not_ready"
