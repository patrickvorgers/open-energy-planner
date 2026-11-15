"""Only newly accepted, currently fresh physical observations may intercept readiness."""

from copy import deepcopy
from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles.model import instant
from tests.app.appliance.raw_support import raw_trace
from tests.app.appliance.support import ENTITY, NOW, POWER, readings, running_readings


@pytest.mark.parametrize("age,accepted", [(0, True), (600, True), (601, False)])
@pytest.mark.parametrize("raw", [False, True])
def test_shared_observer_controls_task_interception_at_actual_age(service, age, accepted, raw):
    """Both caller forms use one actual observation age, including the exact ten-minute boundary."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    service.configure(ENTITY, True)
    service.submit(ENTITY, "ready", NOW, NOW + timedelta(hours=4), NOW)
    data = deepcopy(service.state)
    original = deepcopy(data["tasks"][ENTITY])
    running = {
        "started_at": (NOW - timedelta(minutes=15)).isoformat(),
        "observed_until": NOW.isoformat(),
        "energy_kwh": 0.25,
        "status": "observed",
    }
    service.observe_history(
        data,
        ENTITY,
        [] if raw else [{"at": (NOW - timedelta(minutes=5)).isoformat()}],
        [],
        {"running": running},
        POWER,
        NOW + timedelta(seconds=age),
        **({"observed_until": NOW} if raw else {}),
    )
    assert (data["tasks"][ENTITY]["status"] == "execution_observed") is accepted
    if not accepted:
        assert data["tasks"][ENTITY] == original
        assert "observed_run" not in data["devices"][ENTITY]


@pytest.mark.parametrize("raw", [False, True])
def test_closing_watermark_prevents_old_run_from_intercepting_new_readiness(service, raw):
    """A 12:00 run replay cannot intercept readiness submitted after its 12:05 quiet update."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    data = deepcopy(service.state)
    running = {
        "started_at": (NOW - timedelta(minutes=15)).isoformat(),
        "observed_until": NOW.isoformat(),
        "energy_kwh": 0.25,
        "status": "observed",
    }
    arguments = (
        [] if raw else [{"at": (NOW - timedelta(minutes=5)).isoformat()}],
        [],
        {"running": running},
        POWER,
        NOW,
    )
    keywords = {"observed_until": NOW} if raw else {}
    service.observe_history(data, ENTITY, *arguments, **keywords)
    closed = NOW + timedelta(minutes=5)
    service.observe_history(
        data, ENTITY, [], [], {"quiet": True}, POWER, closed, observed_until=closed
    )
    service.commit(data)
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "idle"
    service.configure(ENTITY, True)
    service.submit(ENTITY, "after-quiet", closed, closed + timedelta(hours=4), closed)
    original = deepcopy(service.state["tasks"][ENTITY])
    data = deepcopy(service.state)
    replay = (*arguments[:-1], closed)
    service.observe_history(data, ENTITY, *replay, **keywords)
    assert data["tasks"][ENTITY] == original
    assert data["devices"][ENTITY]["observed_run"]["status"] == "idle"


def test_combined_raw_counter_and_upload_age_cannot_mutate_started_work(service):
    """A 180-second counter lag plus a 421-second upload delay retains the curve without touching a started task."""
    body = raw_trace(completed=False)
    start, end = instant(body["power_states"][1]["at"]), instant(body["end"])
    before = start - timedelta(hours=1)
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, before)
    service.configure(ENTITY, True)
    task = service.submit(ENTITY, "started", before, start + timedelta(hours=4), before)
    plan = {
        "plan_id": "started-plan",
        "valid_until": (start + timedelta(hours=4)).isoformat(),
        "appliance_schedule": [{"task_id": task["id"], "start": before.isoformat()}],
    }
    service.accept_plan(plan)
    service.tick(before, True, plan)
    attempt = service.state["tasks"][ENTITY]["attempt"]
    service.feedback(ENTITY, task["id"], attempt["id"], "started", before, before)
    original = deepcopy(service.state["tasks"][ENTITY])
    body["energy_states"][-1].update(
        at=(end - timedelta(seconds=180)).isoformat(), state=str(10 + 0.2 + 1 / 90)
    )
    service.learn_raw_history(body, end + timedelta(seconds=421))
    assert service.state["tasks"][ENTITY] == original
    assert "observed_run" not in service.state["devices"][ENTITY]
    assert service.state["devices"][ENTITY]["raw_history"]["power_curve"]


def test_stale_statistical_run_cannot_intercept_pending_readiness(service):
    """An otherwise real statistical run ending eleven minutes ago leaves current readiness untouched."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    service.configure(ENTITY, True)
    service.submit(ENTITY, "pending", NOW, NOW + timedelta(hours=4), NOW)
    original = deepcopy(service.state["tasks"][ENTITY])
    service.learn_history(ENTITY, POWER, running_readings(), False, NOW + timedelta(minutes=11))
    assert service.state["tasks"][ENTITY] == original


@pytest.mark.parametrize("raw", [False, True])
def test_public_history_callers_reject_running_replay_after_quiet(service, raw):
    """Both real learning entry points retain new readiness after a newer quiet upload and an old running replay."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    body = raw_trace(completed=False)
    observed = instant(body["end"]) if raw else NOW
    closed = observed + timedelta(minutes=5)
    if raw:
        service.learn_raw_history(body, observed)
        quiet_start = observed - timedelta(minutes=30)
        quiet = {
            **body,
            "start": quiet_start.isoformat(),
            "end": closed.isoformat(),
            "power_reported_at": closed.isoformat(),
            "energy_reported_at": closed.isoformat(),
            "power_states": [{"at": quiet_start.isoformat(), "state": "0", "unit": "W"}],
            "energy_states": [
                {"at": at.isoformat(), "state": "10", "unit": "kWh"} for at in (quiet_start, closed)
            ],
        }
        service.learn_raw_history(quiet, closed)
    else:
        service.learn_history(ENTITY, POWER, running_readings(), False, observed)
        quiet = [
            {"at": (observed - timedelta(minutes=5 * index)).isoformat(), "kwh": 10, "power_w": 0}
            for index in reversed(range(12))
        ]
        service.learn_history(ENTITY, POWER, quiet, False, closed)
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "idle"
    service.configure(ENTITY, True)
    service.submit(ENTITY, "new-public-readiness", closed, closed + timedelta(hours=4), closed)
    original = deepcopy(service.state["tasks"][ENTITY])
    if raw:
        service.learn_raw_history(body, closed)
    else:
        service.learn_history(ENTITY, POWER, running_readings(), False, closed)
    assert service.state["tasks"][ENTITY] == original
    assert service.state["devices"][ENTITY]["observed_run"]["status"] == "idle"
