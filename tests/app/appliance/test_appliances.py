"""Independent dishwasher profile arithmetic, solver outcomes and durable execution safety."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.appliances.cycles.model import ApplianceWork, CycleProfile, learn_cycle
from open_energy_planner.appliances.cycles.service import ApplianceService
from open_energy_planner.appliances.forecasting import ConsumerForecasts, parse_selection
from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs.solver import HighsSolver
from open_energy_planner.optimization.domain import Interval, SolveOptions, Strategy
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

START = datetime(2026, 10, 24, tzinfo=UTC)
ENTITY = "sensor.dishwasher_energy"
POWERS = (1800, 80, 80, 0, 80, 1200, 80, 0)


def points(start=START):
    """Eight independently specified measured quarter phases total .83 kWh, including two pauses."""
    total = 10.0
    result = [{"at": start.isoformat(), "kwh": total}]
    for index, power in enumerate(POWERS, 1):
        total += power / 4000
        result.append({"at": (start + timedelta(minutes=15 * index)).isoformat(), "kwh": total})
    return result


def service(engine):
    """Use real shared storage with a selected Energy-owned dishwasher. No fake task persistence."""
    consumers = ConsumerForecasts(SqlDocuments(engine, "demand"))
    consumers.sync(
        {
            "entities": [{"entity_id": ENTITY, "name": "Dishwasher"}],
            "suggestions": {"consumers": [{"entity_id": ENTITY, "upstream": None}]},
        }
    )
    consumers.save(
        parse_selection([{"entity_id": ENTITY, "weather_sensitive": False}]), consumers.version
    )
    tasks = ApplianceService(SqlDocuments(engine, "planning"), consumers)
    tasks.configure(ENTITY, True)
    tasks.record_cycle(ENTITY, "manual-cycle", START, START + timedelta(hours=2), points())
    return tasks


@pytest.fixture
def tasks(tmp_path):
    """Close the real SQLite archive after each isolated application lifecycle scenario."""
    repository = SqlPlanRepository(tmp_path)
    yield service(repository.engine)
    repository.close()


def task(tasks, now=START + timedelta(days=1)):
    """Create one explicit ready task with six hours of flexibility, never inferred from usage history."""
    return tasks.submit(ENTITY, "ready-run", now, now + timedelta(hours=6), now)


def scheduled(tasks):
    """Publish a valid due schedule so the service can issue a bounded start attempt."""
    current = task(tasks)
    now = START + timedelta(days=1)
    plan = {
        "plan_id": "plan",
        "valid_until": (now + timedelta(minutes=20)).isoformat(),
        "appliance_schedule": [{"task_id": current["id"], "start": now.isoformat()}],
    }
    tasks.accept_plan(plan)
    tasks.tick(now, True, plan)
    return tasks.state["tasks"][ENTITY], plan, now


@pytest.mark.parametrize("waiting_at", ["lock", "worker"])
@pytest.mark.parametrize("expires", [False, True])
def test_claim_checks_expiry_after_lock_and_worker_waits(tasks, monkeypatch, waiting_at, expires):
    """A two-minute start request expires after a 121-second queue wait and cannot authorize execution."""
    import asyncio
    import threading
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.appliances.cycles import api as appliance_api

    async def scenario():
        """Hold a real request at the admission lock or write boundary using a deterministic clock."""
        current, plan, before = scheduled(tasks)
        clock = [before]

        class Clock:
            """Separate wall time from asyncio scheduling without sleeping for expiry."""

            @staticmethod
            def now(_zone):
                """Return the independently controlled current operation time."""
                return clock[0]

        monkeypatch.setattr(appliance_api, "datetime", Clock)
        entered = asyncio.Event()
        release = asyncio.Event()
        worker_release = threading.Event()
        loop = asyncio.get_running_loop()
        lock = asyncio.Lock()
        await lock.acquire()

        class ObservedLock:
            """Expose when HTTP parsing has reached a genuinely held app lock."""

            async def __aenter__(self):
                """Signal admission and wait for the real app lock to be released."""
                entered.set()
                await lock.acquire()

            async def __aexit__(self, *_arguments):
                """Release the lock on successful claims and rejected expired attempts."""
                lock.release()

        complete_write = appliance_api.complete_write

        async def delayed_write(operation):
            """Hold an already submitted worker before invoking the actual claim operation."""
            if waiting_at == "worker":

                def held_operation():
                    """Signal actual worker execution before the test advances wall time."""
                    loop.call_soon_threadsafe(entered.set)
                    worker_release.wait()
                    return operation()

                pending_write = asyncio.create_task(complete_write(held_operation))
                try:
                    await release.wait()
                finally:
                    worker_release.set()
                return await pending_write
            return await complete_write(operation)

        monkeypatch.setattr(appliance_api, "complete_write", delayed_write)
        app = web.Application()
        appliance_api.register(
            app,
            tasks,
            SimpleNamespace(state={"enabled": True, "plan": plan}),
            None,
            ObservedLock() if waiting_at == "lock" else asyncio.Lock(),
        )
        async with TestClient(TestServer(app)) as client:
            pending = asyncio.create_task(
                client.post(
                    "/v1/appliances/claim",
                    json={
                        "entity_id": ENTITY,
                        "task_id": current["id"],
                        "attempt_id": current["attempt"]["id"],
                    },
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                clock[0] = before + timedelta(seconds=121 if expires else 0)
            finally:
                lock.release()
                release.set()
            response = await pending
            if expires:
                assert response.status == 400
                assert "expired" in await response.text()
                assert tasks.state["tasks"][ENTITY]["attempt"]["status"] == "requested"
            else:
                assert response.status == 200
                assert (await response.json())["result"]["authorized"] is True
                assert tasks.state["tasks"][ENTITY]["attempt"]["status"] == "claimed"

    asyncio.run(scenario())


@pytest.mark.parametrize(("plan_seconds", "expected_seconds"), [(30, 30), (300, 120)])
def test_claim_returns_effective_deadline_even_after_delayed_persistence(
    tasks, monkeypatch, plan_seconds, expected_seconds
):
    """A late durable response retains the earlier of the plan's expiry and the two-minute attempt expiry."""
    current, plan, before = scheduled(tasks)
    plan["valid_until"] = (before + timedelta(seconds=plan_seconds)).isoformat()
    clock = [before]
    commit = tasks.commit

    def delayed_commit(data):
        """Finish the actual SQLite write before simulating response delivery ten minutes later."""
        commit(data)
        clock[0] = before + timedelta(minutes=10)

    monkeypatch.setattr(tasks, "commit", delayed_commit)
    result = tasks.claim(ENTITY, current["id"], current["attempt"]["id"], before, True, plan)
    assert result == {
        "authorized": True,
        "task_id": current["id"],
        "attempt_id": current["attempt"]["id"],
        "expires_at": (before + timedelta(seconds=expected_seconds)).isoformat(),
    }
    assert datetime.fromisoformat(result["expires_at"]) < clock[0]
    assert tasks.state["tasks"][ENTITY]["attempt"]["status"] == "claimed"


def test_complete_fine_cycle_learns_energy_and_preserves_pauses():
    """Measured .45+.02+.02+0+.02+.30+.02+0 equals .83 kWh. A pause is a real phase."""
    learned = learn_cycle(points(), START, START + timedelta(hours=2))
    assert learned.power_w == pytest.approx(POWERS)
    assert learned.energy_kwh == pytest.approx(0.83)
    assert learned.duration_seconds == 7200
    assert learned.energy(
        START, START + timedelta(minutes=50), START + timedelta(minutes=70)
    ) == pytest.approx(80 * (10 / 60) / 1000)
    coarse = [points()[0], points()[4], points()[8]]
    with pytest.raises(ValueError, match="coarse"):
        learn_cycle(coarse, START, START + timedelta(hours=2))
    reset = points()
    reset[3]["kwh"] = 0
    with pytest.raises(ValueError, match="resets"):
        learn_cycle(reset, START, START + timedelta(hours=2))


@pytest.mark.parametrize("strategy", [Strategy.COST, Strategy.CONSUMPTION])
def test_solver_selects_independently_known_contiguous_cycle(strategy):
    """Cheap .1 rates at 02:00 and 03:15 align the two heater peaks. .075+.08 = .155 cost."""
    intervals = tuple(
        Interval(START + timedelta(minutes=15 * index), 0.25, 0.1 if index in (8, 13) else 1, 0)
        for index in range(32)
    )
    work = ApplianceWork("task", ENTITY, START, START + timedelta(hours=8), CycleProfile(POWERS))
    problem = InstallationProblem(intervals, (), (), (), 3000, 3000, appliances=(work,))
    result = HighsSolver().solve(problem, SolveOptions(strategy=strategy))
    assert result.plan is not None, result.stop_reason
    validate(problem, result.plan)
    assert dict(result.plan.appliance_starts)["task"] == START + timedelta(hours=2)
    assert result.plan.costs.cash_cost == pytest.approx(0.155)
    assert sum(flow.grid_import for flow in result.plan.flows) == pytest.approx(0.83)
    corrupt = replace(result.plan, appliance_starts=(("task", START),))
    with pytest.raises(ValueError, match="conservation"):
        validate(problem, corrupt)


def test_infeasible_appliance_does_not_block_other_installation_planning():
    """A 1.8-kW heater cannot fit a 1.6-kW grid. The task is omitted while .1 kWh/h household remains served."""
    intervals = tuple(
        Interval(START + timedelta(minutes=15 * index), 0.25, 1, 0, 0.025) for index in range(16)
    )
    work = ApplianceWork("task", ENTITY, START, START + timedelta(hours=4), CycleProfile(POWERS))
    problem = InstallationProblem(intervals, (), (), (), 1600, 1600, appliances=(work,))
    result = HighsSolver().solve(problem)
    assert result.plan is not None
    assert result.plan.appliance_starts == ()
    assert sum(flow.grid_import for flow in result.plan.flows) == pytest.approx(0.4)
    validate(problem, result.plan)
    from open_energy_planner.planning import CommandMapper, InputSnapshot
    from open_energy_planner.savings.context import physical_context
    from open_energy_planner.savings.evidence import evidence
    from open_energy_planner.savings.forecast import comparison

    snapshot = InputSnapshot(
        "snapshot", "configuration", START, "measurement", "forecast", "prices", problem
    )
    document = CommandMapper().build(snapshot, result, result.plan.costs, START)
    physical = physical_context(document)
    assert sum(step.load_kwh for step in physical.intervals) == pytest.approx(0.4)
    assert evidence(document).get("issue") is None
    with pytest.raises(ValueError, match="not a comparable"):
        comparison(document)


def test_profile_and_readiness_are_idempotent_and_overlapping_runs_rejected(tasks):
    """Retrying the same measured cycle/task does not create extra samples or duplicate runnable work."""
    tasks.record_cycle(ENTITY, "manual-cycle", START, START + timedelta(hours=2), points())
    assert tasks.state["devices"][ENTITY]["profile"]["samples"] == 1
    first = task(tasks)
    assert task(tasks)["id"] == first["id"]
    with pytest.raises(ValueError, match="unresolved"):
        tasks.submit(
            ENTITY,
            "second",
            START + timedelta(days=1),
            START + timedelta(days=1, hours=6),
            START + timedelta(days=1),
        )


def test_claim_expiry_restart_and_late_feedback_never_duplicate_start(tasks):
    """A claimed start with missing feedback becomes unknown across restart. True late start reconciles without a new command."""
    current, plan, now = scheduled(tasks)
    attempt_id = current["attempt"]["id"]
    tasks.claim(ENTITY, current["id"], attempt_id, now, True, plan)
    with pytest.raises(ValueError, match="claimed"):
        tasks.claim(ENTITY, current["id"], attempt_id, now, True, plan)
    tasks.tick(now + timedelta(minutes=3), True, plan)
    restored = ApplianceService(tasks.documents, tasks.consumers)
    assert restored.state["tasks"][ENTITY]["status"] == "start_not_confirmed"
    assert (
        restored.document(now + timedelta(minutes=3), True, plan)["devices"][0]["request"] is None
    )
    with pytest.raises(ValueError, match="cannot execute"):
        restored.feedback(
            ENTITY,
            current["id"],
            attempt_id,
            "not_started",
            now + timedelta(minutes=3),
            now + timedelta(minutes=3),
        )
    restored.feedback(
        ENTITY,
        current["id"],
        attempt_id,
        "started",
        now + timedelta(seconds=10),
        now + timedelta(minutes=3),
    )
    assert restored.state["tasks"][ENTITY]["status"] == "running"
    restored.feedback(
        ENTITY,
        current["id"],
        attempt_id,
        "started",
        now + timedelta(seconds=10),
        now + timedelta(minutes=3),
    )
    assert len(restored.state["tasks"][ENTITY]["events"]) == 1
    restored.feedback(
        ENTITY,
        current["id"],
        attempt_id,
        "completed",
        now + timedelta(hours=2),
        now + timedelta(hours=2),
    )
    with pytest.raises(ValueError, match="contradicts"):
        restored.feedback(
            ENTITY,
            current["id"],
            attempt_id,
            "started",
            now + timedelta(hours=2),
            now + timedelta(hours=2),
        )


def test_confirmed_cancel_allows_new_attempt_but_stale_feedback_is_rejected(tasks):
    """Only explicit nonexecution plus cancellation closes an attempt. Retry retains the task and changes attempt identity."""
    current, plan, now = scheduled(tasks)
    old_id = current["attempt"]["id"]
    tasks.feedback(ENTITY, current["id"], old_id, "not_started", now, now, cancelled=True)
    tasks.accept_plan(plan)
    tasks.tick(now + timedelta(seconds=1), True, plan)
    assert tasks.state["tasks"][ENTITY]["id"] == current["id"]
    assert tasks.state["tasks"][ENTITY]["attempt"]["id"] != old_id
    with pytest.raises(ValueError, match="stale"):
        tasks.feedback(ENTITY, current["id"], old_id, "started", now, now)


@pytest.mark.parametrize("with_bonus", [False, True])
def test_task_projection_replaces_device_forecast_once_and_retains_exact_inputs(tasks, with_bonus):
    """Six hours of .3-kWh residual plus the .83-kWh task totals 2.63, not household+task=3.23."""
    from types import SimpleNamespace

    from open_energy_planner.planning import InputSnapshot

    now = START + timedelta(days=1)
    task(tasks)
    steps = [
        {"start": (now + timedelta(hours=index)).isoformat(), "duration_h": 1, "kwh": 0.3}
        for index in range(6)
    ]
    residual = {"issued_at": now.isoformat(), "steps": steps}
    tasks.consumers.result = {
        "devices": [
            {
                "entity_id": ENTITY,
                "forecast": {
                    "issued_at": now.isoformat(),
                    "steps": [{**step, "kwh": 0.1} for step in steps],
                },
                "baseline_forecast": {
                    "issued_at": now.isoformat(),
                    "steps": [{**step, "kwh": 0.1} for step in steps],
                },
            }
        ],
        "residual_baseline": residual,
    }
    original = InstallationProblem(
        tuple(Interval(now + timedelta(hours=index), 1, 0.2, 0, 0.4) for index in range(6)),
        (),
        (),
        (),
        3000,
        3000,
    )
    if with_bonus:
        from open_energy_planner.optimization.export_bonus import ExportBonus

        original = replace(
            original,
            export_bonuses=(
                ExportBonus(
                    "bonus",
                    (0.2,) * 6,
                    (0.5,) * 3 + (0.7,) * 3,
                    ("first",) * 3 + ("second",) * 3,
                    0.4,
                ),
            ),
        )
    snapshot = InputSnapshot(
        "snapshot", "configuration", now, "measurement", "forecast", "prices", original
    )
    prepared = tasks.prepare(snapshot, SimpleNamespace(), now)
    assert sum(step.load_kwh for step in prepared.problem.intervals) == pytest.approx(1.8)
    assert sum(step.load_kwh for step in original.intervals) == pytest.approx(2.4)
    assert prepared.appliance_evidence == (residual,)
    assert all(step.duration_h <= 0.25 for step in prepared.problem.intervals)
    if with_bonus:
        from open_energy_planner.optimization.export_bonus import settle

        rule = prepared.problem.export_bonuses[0]
        assert (
            len(rule.rates)
            == len(rule.caps_kwh)
            == len(rule.periods)
            == len(prepared.problem.intervals)
            == 24
        )
        assert rule.periods == ("first",) * 12 + ("second",) * 12
        settled = settle(rule, (0.1,) * 24)
        assert sum(settled.quota_kwh) == pytest.approx(0.8), (
            "Only .1 old-period quota and .7 reset-period quota remain"
        )
        assert sum(
            rate * credit for rate, credit in zip(rule.rates, settled.quota_kwh, strict=True)
        ) == pytest.approx(0.16)
    result = HighsSolver().solve(prepared.problem)
    validate(prepared.problem, result.plan)
    assert sum(flow.grid_import for flow in result.plan.flows) == pytest.approx(2.63)
    before = tasks.revision
    starts = dict(result.plan.appliance_starts)
    tasks.accept_plan(
        {
            "plan_id": "plan",
            "appliance_schedule": [
                {
                    "task_id": prepared.problem.appliances[0].task_id,
                    "start": next(iter(starts.values())).isoformat(),
                }
            ],
        }
    )
    assert tasks.revision == before, "Plan publication must not itself request another solver round"


def test_running_cycle_keeps_actual_start_and_remaining_energy_after_replanning(tasks):
    """After the first .45-kWh heater phase, remaining .38 kWh stays committed even across storage reload."""
    current, _, now = scheduled(tasks)
    tasks.feedback(ENTITY, current["id"], current["attempt"]["id"], "started", now, now)
    restored = ApplianceService(tasks.documents, tasks.consumers)
    learned = CycleProfile(POWERS)
    later = now + timedelta(minutes=15)
    work = ApplianceWork(current["id"], ENTITY, now, now + timedelta(hours=6), learned, now)
    problem = InstallationProblem(
        tuple(Interval(later + timedelta(minutes=15 * index), 0.25, 0.1, 0) for index in range(23)),
        (),
        (),
        (),
        3000,
        3000,
        appliances=(work,),
    )
    result = HighsSolver().solve(problem)
    validate(problem, result.plan)
    assert dict(result.plan.appliance_starts)[current["id"]] == now
    assert sum(flow.grid_import for flow in result.plan.flows) == pytest.approx(0.38)
    assert restored.state["tasks"][ENTITY]["status"] == "running"


@pytest.mark.parametrize("execution", ["requested", "claimed", "started", "unknown"])
def test_task_cancellation_withdraws_planning_without_inventing_physical_stop(tasks, execution):
    """Cancellation always releases planning. Retained attempt evidence never reopens it or authorizes a queued start."""
    current, plan, now = scheduled(tasks)
    attempt_id = current["attempt"]["id"]
    if execution in ("claimed", "started"):
        tasks.claim(ENTITY, current["id"], attempt_id, now, True, plan)
    if execution == "started":
        tasks.feedback(ENTITY, current["id"], attempt_id, "started", now, now)
    if execution == "unknown":
        tasks.tick(now + timedelta(minutes=3), True, plan)
    before_attempt = dict(tasks.state["tasks"][ENTITY]["attempt"])
    cancelled = tasks.cancel_task(ENTITY, current["id"])
    assert cancelled["status"] == "cancelled" and cancelled["plan_id"] is None
    assert cancelled["attempt"] == before_attempt
    tasks.tick(now + timedelta(minutes=4), True, plan)
    assert tasks.state["tasks"][ENTITY]["status"] == "cancelled"
    assert tasks.document(now, True, plan)["devices"][0]["request"] is None
    with pytest.raises(ValueError):
        tasks.claim(ENTITY, current["id"], attempt_id, now, True, plan)
    event = "completed" if execution == "started" else "started"
    tasks.feedback(
        ENTITY,
        current["id"],
        attempt_id,
        event,
        now + timedelta(minutes=4),
        now + timedelta(minutes=4),
    )
    assert tasks.state["tasks"][ENTITY]["status"] == "cancelled"
    tasks.submit(ENTITY, "new-explicit-readiness", now, now + timedelta(hours=6), now)
    assert tasks.state["tasks"][ENTITY]["id"] != current["id"]


def test_appliance_api_authentication_schema_and_durable_cycle_learning(tmp_path):
    """Real HTTP routes reject unauthenticated/coarse data and retain a complete cycle and idempotent task across restart."""
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.app import create_app

    async def scenario():
        """Exercise actual middleware and SQL-backed service, without claiming an unavailable live forecast is runnable."""
        token = "appliance-test-token-0123456789"
        headers = {"Authorization": "Bearer " + token}
        repository = SqlPlanRepository(tmp_path)
        prepared = service(repository.engine)
        repository.close()
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            assert (await client.get("/v1/appliances")).status == 401
            response = await client.get("/v1/appliances", headers=headers)
            state = await response.json()
            assert state["devices"][0]["profile"]["samples"] == 1
            assert state["devices"][0]["profile"]["power_w"] == pytest.approx(POWERS)
            assert (
                await client.post("/v1/appliances/feedback", headers=headers, json={})
            ).status == 400
            assert (
                await client.post(
                    "/v1/appliances/claim",
                    headers=headers,
                    json={"entity_id": ENTITY, "task_id": "old", "attempt_id": "old"},
                )
            ).status == 400
            assert prepared.state["tasks"] == {}

    asyncio.run(scenario())


def test_dashboard_deadline_is_durable_draft_and_never_changes_open_work(tasks):
    """Saving a future draft neither submits work nor triggers optimization. Readiness freezes it and blocks edits."""
    now = START + timedelta(days=1)
    before = tasks.revision
    tasks.set_deadline(ENTITY, now + timedelta(hours=6), now)
    assert tasks.revision == before and not tasks.state["tasks"]
    restored = ApplianceService(tasks.documents, tasks.consumers)
    assert (
        restored.state["devices"][ENTITY]["finish_before"] == (now + timedelta(hours=6)).isoformat()
    )
    for deadline in (now, now + timedelta(days=8), now.replace(tzinfo=None)):
        with pytest.raises(ValueError, match="next seven days"):
            restored.set_deadline(ENTITY, deadline, now)
    submitted = restored.submit(ENTITY, "dashboard-ready", now, now + timedelta(hours=6), now)
    with pytest.raises(ValueError, match="open task"):
        restored.set_deadline(ENTITY, now + timedelta(hours=7), now)
    assert restored.state["tasks"][ENTITY]["deadline"] == submitted["deadline"]


def test_cancelled_task_feedback_is_archived_after_a_new_explicit_submission(tasks):
    """Old physical completion updates only its cancelled archive. New planning and its revision remain unchanged."""
    from copy import deepcopy

    old, plan, now = scheduled(tasks)
    attempt_id = old["attempt"]["id"]
    tasks.feedback(ENTITY, old["id"], attempt_id, "started", now, now)
    tasks.cancel_task(ENTITY, old["id"])
    newer = tasks.submit(ENTITY, "replacement", now, now + timedelta(hours=6), now)
    before, revision = deepcopy(tasks.state), tasks.revision
    observed = now + timedelta(hours=2)
    result = tasks.feedback(ENTITY, old["id"], attempt_id, "completed", observed, observed)
    assert result["status"] == "cancelled" and result["attempt"]["status"] == "completed"
    assert tasks.state == before and tasks.revision == revision
    assert tasks.state["tasks"][ENTITY]["id"] == newer["id"]
    restored = ApplianceService(tasks.documents, tasks.consumers)
    assert (
        restored.documents.read("appliance-task-" + old["id"] + ".json")["attempt"]["status"]
        == "completed"
    )
    assert (
        restored.feedback(ENTITY, old["id"], attempt_id, "completed", observed, observed) == result
    )
    with pytest.raises(ValueError):
        restored.claim(ENTITY, old["id"], attempt_id, observed, True, plan)
    with pytest.raises(ValueError):
        restored.feedback("sensor.other", old["id"], attempt_id, "completed", observed, observed)


def test_split_deadline_fields_are_order_independent_and_resolve_european_dst(tasks):
    """Both controls create one future deadline. Autumn uses the first future occurrence and spring gaps cannot enable readiness."""
    now = START
    revision = tasks.revision
    tasks.set_deadline_field(ENTITY, "date", "2026-10-25", "Europe/Amsterdam", now)
    assert tasks.state["devices"][ENTITY]["finish_before"] is None
    tasks.set_deadline_field(ENTITY, "time", "02:30:00", "Europe/Amsterdam", now)
    assert tasks.state["devices"][ENTITY]["finish_before"] == "2026-10-25T00:30:00+00:00"
    assert tasks.revision == revision
    tasks.set_deadline_field(
        ENTITY, "time", "02:30:00", "Europe/Amsterdam", datetime(2026, 10, 25, 1, tzinfo=UTC)
    )
    assert tasks.state["devices"][ENTITY]["finish_before"] == "2026-10-25T01:30:00+00:00"
    spring = datetime(2026, 3, 28, tzinfo=UTC)
    tasks.set_deadline_field(ENTITY, "date", "2026-03-29", "Europe/Amsterdam", spring)
    assert tasks.state["devices"][ENTITY]["finish_before"] is None
    tasks.set_deadline_field(ENTITY, "time", "03:30:00", "Europe/Amsterdam", spring)
    assert tasks.state["devices"][ENTITY]["finish_before"] == "2026-03-29T01:30:00+00:00"
    for field, value, zone in (
        ("time", "03:30:00+01:00", "Europe/Amsterdam"),
        ("date", "invalid", "UTC"),
        ("date", "2026-03-29", "invalid/zone"),
    ):
        with pytest.raises(ValueError):
            tasks.set_deadline_field(ENTITY, field, value, zone, spring)


def test_learned_duration_keeps_longest_cycle_while_averaging_power(tasks):
    """120- and 150-minute .83/.855-kWh cycles average to .8425 kWh. A later 15-minute cycle must not shorten the retained horizon."""
    extended = points()
    extended.append(
        {"at": (START + timedelta(minutes=135)).isoformat(), "kwh": extended[-1]["kwh"] + 0.025}
    )
    extended.append(
        {"at": (START + timedelta(minutes=150)).isoformat(), "kwh": extended[-1]["kwh"]}
    )
    tasks.record_cycle(ENTITY, "longer-cycle", START, START + timedelta(minutes=150), extended)
    learned = tasks.state["devices"][ENTITY]["profile"]
    assert len(learned["power_w"]) * 15 == 150
    assert learned["samples"] == 2
    assert sum(learned["power_w"]) / 4000 == pytest.approx(0.8425)
    assert learned["power_w"][-2:] == pytest.approx([50, 0])
    tasks.record_cycle(
        ENTITY,
        "shorter-cycle",
        START,
        START + timedelta(minutes=15),
        [
            {"at": START.isoformat(), "kwh": 10},
            {"at": (START + timedelta(minutes=15)).isoformat(), "kwh": 10.5},
        ],
    )
    learned = tasks.state["devices"][ENTITY]["profile"]
    assert learned["samples"] == 3
    assert len(learned["power_w"]) * 15 == 150
    assert sum(learned["power_w"]) / 4000 == pytest.approx((0.83 + 0.855 + 0.5) / 3)
