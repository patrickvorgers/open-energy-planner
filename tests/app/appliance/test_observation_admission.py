"""Only newly accepted, currently fresh physical observations may intercept readiness."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.cycles.model import instant
from tests.app.appliance.raw_support import raw_trace, start_observing
from tests.app.appliance.support import ENTITY, NOW, POWER, readings, running_readings


@pytest.mark.parametrize("age,accepted", [(-1, False), (0, True), (600, True), (601, False)])
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


def test_same_observed_cycle_progress_keeps_the_planning_revision(service):
    """A newer meter sample leaves the same frozen run usable. A real run transition changes it."""
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, NOW)
    initial = service.planning_revision
    data = deepcopy(service.state)
    running = {
        "started_at": (NOW - timedelta(minutes=15)).isoformat(),
        "observed_until": NOW.isoformat(),
        "energy_kwh": 0.25,
        "status": "observed",
    }
    service.observe_history(
        data, ENTITY, [], [], {"running": running}, POWER, NOW, observed_until=NOW
    )
    service.commit(data)
    running_revision = service.planning_revision
    public_revision = service.revision
    assert running_revision != initial

    later = NOW + timedelta(seconds=5)
    data = deepcopy(service.state)
    running.update(observed_until=later.isoformat(), energy_kwh=0.26)
    service.observe_history(
        data, ENTITY, [], [], {"running": running}, POWER, later, observed_until=later
    )
    service.commit(data)
    assert service.state["devices"][ENTITY]["observed_run"]["energy_kwh"] == 0.26
    assert service.revision != public_revision
    assert service.planning_revision == running_revision

    closed = NOW + timedelta(minutes=5)
    data = deepcopy(service.state)
    service.observe_history(
        data, ENTITY, [], [], {"quiet": True}, POWER, closed, observed_until=closed
    )
    service.commit(data)
    assert service.planning_revision != running_revision


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
        start_observing(service, body)
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


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("processing_seconds,accepted", [(301, True), (302, False)])
def test_processing_time_counts_before_running_evidence_intercepts_readiness(
    service, monkeypatch, raw, processing_seconds, accepted
):
    """Evidence aged 299 seconds at entry remains admissible at 600 seconds, but not at 601 after archive work."""
    from open_energy_planner.appliances.cycles import service as service_module

    body = raw_trace(completed=False)
    observed = instant(body["end"]) if raw else NOW
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, observed)
    service.configure(ENTITY, True)
    service.submit(ENTITY, "processing-age", observed, observed + timedelta(hours=4), observed)
    original = deepcopy(service.state["tasks"][ENTITY])
    if raw:
        start_observing(service, body)
    service._cycle_records.clear()
    elapsed = [0]
    monkeypatch.setattr(service_module, "monotonic", lambda: elapsed[0], raising=False)
    collect = service.collect_cycles

    def slow_archive(*arguments, **keywords):
        """Perform the real cold archive restoration, then advance processing time without sleeping."""
        result = collect(*arguments, **keywords)
        elapsed[0] = processing_seconds
        return result

    monkeypatch.setattr(service, "collect_cycles", slow_archive)
    if raw:
        service.learn_raw_history(body, observed + timedelta(seconds=299))
        assert service.state["devices"][ENTITY]["raw_history"]["power_curve"]
    else:
        service.learn_history(
            ENTITY, POWER, running_readings(), False, observed + timedelta(seconds=299)
        )
    assert (service.state["tasks"][ENTITY]["status"] == "execution_observed") is accepted
    assert service.state["tasks"][ENTITY]["profile"] == original["profile"]
    if not accepted:
        assert service.state["tasks"][ENTITY] == original
        assert "observed_run" not in service.state["devices"][ENTITY]


@pytest.mark.parametrize("raw", [False, True])
def test_http_observation_age_includes_executor_queue_delay(service, monkeypatch, raw):
    """The HTTP owner starts elapsed time before executor dispatch, so queued evidence cannot latch readiness."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.appliances.cycles import api as api_module
    from open_energy_planner.appliances.cycles import service as service_module

    body = raw_trace(completed=False)
    observed = instant(body["end"]) if raw else NOW
    service.learn_history(ENTITY, POWER, readings(cycles=1), False, observed)
    service.configure(ENTITY, True)
    service.submit(ENTITY, "queued", observed, observed + timedelta(hours=4), observed)
    original = deepcopy(service.state["tasks"][ENTITY])
    elapsed = [0]

    class EntryTime(datetime):
        """Keep the independent UTC operation timestamp stable while processing time advances."""

        @classmethod
        def now(cls, tz=None):
            """Capture once inside the acquired route lock, independently of the executor's start."""
            return observed + timedelta(seconds=299)

    complete = api_module.complete_write

    async def queued_write(save, *arguments):
        """Advance a controlled monotonic clock before the real executor invokes the domain writer."""
        elapsed[0] = 302
        return await complete(save, *arguments)

    monkeypatch.setattr(api_module, "datetime", EntryTime)
    monkeypatch.setattr(api_module, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(service_module, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(api_module, "complete_write", queued_write)

    async def scenario():
        """Dispatch the real HTTP operation through its lock and off-thread write owner."""
        application = web.Application()
        api_module.register(
            application,
            service,
            SimpleNamespace(state={"enabled": False, "plan": None}),
            None,
            asyncio.Lock(),
            SimpleNamespace(state={"points": []}),
            SimpleNamespace(state={"stores": []}),
        )
        payload = (
            body
            if raw
            else {
                "entity_id": ENTITY,
                "power_entity": POWER,
                "rows": running_readings(),
                "synthetic": False,
            }
        )
        async with TestClient(TestServer(application)) as client:
            response = await client.post(
                "/v1/appliances/" + ("raw_history" if raw else "history"), json=payload
            )
            assert response.status == 200
        assert service.state["tasks"][ENTITY] == original
        assert "observed_run" not in service.state["devices"][ENTITY]
        if raw:
            assert service.state["devices"][ENTITY]["raw_history"]["power_curve"]

    asyncio.run(scenario())


def test_processing_age_does_not_discard_conserved_raw_training_or_original_frame(
    service, monkeypatch
):
    """Capture admission stays at UTC entry even when archive work later makes live running evidence stale."""
    from open_energy_planner.appliances.cycles import service as service_module
    from open_energy_planner.appliances.cycles.service import ApplianceService, profile

    body = raw_trace()
    start_observing(service, body)
    elapsed = [0]
    collect = service.collect_cycles

    def slow_archive(*arguments, **keywords):
        """Read actual stored evidence and then simulate fifteen minutes of elapsed processing."""
        result = collect(*arguments, **keywords)
        elapsed[0] = 901
        return result

    monkeypatch.setattr(service_module, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(service, "collect_cycles", slow_archive)
    service.learn_raw_history(body, instant(body["end"]))
    restored = ApplianceService(service.documents, service.consumers)
    assert profile(restored.state["devices"][ENTITY]["profile"]).energy_kwh == pytest.approx(
        23 / 45
    )
    assert restored.state["devices"][ENTITY]["profile"]["samples"] == 1
    assert restored.state["devices"][ENTITY]["raw_history"]["power_curve"]
    [record] = list(restored.documents.iter_prefix("appliance-raw-"))
    assert restored.documents.read(record["history_ref"]) == body
