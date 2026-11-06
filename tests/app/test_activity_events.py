"""Business events retain source ownership, restart truth and race-free subscriptions."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.activities.api import EVENTS
from open_energy_planner.activities.events import Events, Health, Outcome, public_context
from open_energy_planner.app import create_app
from open_energy_planner.storage.adapters.activities import ActivityStore
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
TOKEN = "activity-test-only-0123456789abcdef"


@pytest.mark.parametrize(
    "value",
    [None, "private", [], {"private_url": "private"}, {"label": " \n\t"}],
)
def test_unusable_activity_context_is_omitted(value):
    """Optional diagnostic fields cannot require domain work to fail."""
    assert public_context(value) == {}


@pytest.mark.parametrize(
    "identity",
    [
        None,
        "",
        "x" * 129,
        "https://example.invalid",
        "../other",
        "bad\nidentity",
        "x?token=private",
    ],
)
def test_activity_plan_identity_rejects_unsafe_navigation_inputs(identity):
    """Only bounded application plan identities can become read-only navigation parameters."""
    assert public_context({"plan_id": identity}) == {}


@pytest.mark.parametrize(
    "start,end",
    [
        (None, None),
        ("2026-10-25T12:00:00+00:00", None),
        ("2026-10-25T12:00:00", "2026-10-25T13:00:00"),
        ("2026-10-25T12:00:00+00:00", "2026-10-25T12:00:00+00:00"),
        ("2026-10-25T13:00:00+00:00", "2026-10-25T12:00:00+00:00"),
        ("invalid", "2026-10-25T13:00:00+00:00"),
        ("x" * 41, "2026-10-25T13:00:00+00:00"),
    ],
)
def test_invalid_activity_range_preserves_only_the_safe_label(start, end):
    """Never publish a partial, reversed, naive or malformed range."""
    assert public_context({"label": "Household", "start": start, "end": end}) == {
        "label": "Household"
    }


@pytest.mark.parametrize("generated", [None, "invalid", "2026-10-25T12:00:00", "x" * 41])
def test_invalid_plan_context_keeps_subject_without_unknown_strategy(generated):
    """An unusable creation time cannot make a plan caption ambiguous or expose arbitrary archive strings."""
    assert public_context(
        {
            "label": "Archived plan",
            "plan_generated_at": generated,
            "plan_strategy": "PRIVATE_STRATEGY",
        }
    ) == {"label": "Archived plan"}


def test_plan_context_never_publishes_unknown_strategy_values():
    """A valid creation time does not admit arbitrary retained strategy text."""
    assert public_context(
        {"plan_generated_at": "2026-10-25T03:15:17+01:00", "plan_strategy": "PRIVATE_STRATEGY"}
    ) == {"plan_generated_at": "2026-10-25T02:15:17+00:00"}


def test_activity_context_is_detached_bounded_and_preserved_across_restart(tmp_path):
    """Capture safe producer context once and retain it through live progress and restart."""

    async def scenario():
        """Exercise actual publication and SQL history with an independently specified UTC range."""
        repository = SqlPlanRepository(tmp_path)
        events = Events(ActivityStore(repository.engine), lambda: NOW)
        supplied = {
            "label": "  Household\nconsumption\t" + "x" * 200,
            "start": "2026-10-25T01:00:00+02:00",
            "end": "2026-10-25T03:00:00+01:00",
            "private_url": "PRIVATE_CONTEXT_SENTINEL",
            "plan_generated_at": "2026-10-25T03:15:17+01:00",
            "plan_strategy": "minimize cost",
            "plan_id": "retained-plan",
        }
        expected = {
            "label": ("Household consumption " + "x" * 200)[:160],
            "start": "2026-10-24T23:00:00+00:00",
            "end": "2026-10-25T02:00:00+00:00",
            "plan_generated_at": "2026-10-25T02:15:17+00:00",
            "plan_strategy": "minimize cost",
            "plan_id": "retained-plan",
        }
        try:
            identity = await events.start(
                "settlement", "settlement", "Evaluate savings", context=supplied
            )
            supplied["label"] = "Changed after publication"
            assert events.running[identity]["context"] == expected
            cursor = events.cursor
            await events.update(identity, "Waiting for complete measurements.")
            update = events.changes(cursor)["events"][-1]["value"]
            assert update["id"] == identity and update["context"] == expected
            update["context"]["label"] = "Changed detached replay"
            assert events.running[identity]["context"] == expected
            await events.flush()
            events.store.restart(NOW + timedelta(minutes=1))
            retained = events.store.read(NOW - timedelta(days=1), NOW + timedelta(days=1))
            assert retained[0]["id"] == identity and retained[0]["state"] == "interrupted"
            assert retained[0]["context"] == expected
            assert "PRIVATE_CONTEXT_SENTINEL" not in str(retained)
            scheduled = {"label": "Solar"}
            events.scheduled("solar", "forecast", "Update forecast", NOW, context=scheduled)
            scheduled["label"] = "Caller mutation"
            assert events.snapshot()["scheduled"][0]["context"] == {"label": "Solar"}
        finally:
            await events.close()
            repository.close()

    asyncio.run(scenario())


def test_replay_atomic_subscription_reset_and_detached_snapshots(tmp_path):
    """Events emitted after a snapshot and before waiting are replayed without a lost wakeup."""

    async def scenario():
        """Exercise a waiting subscriber, cursor overflow and a different process epoch."""
        repository = SqlPlanRepository(tmp_path)
        try:
            events = Events(ActivityStore(repository.engine), lambda: NOW)
            initial = await events.subscribe()
            assert initial["reset"] and initial["snapshot"]["health"] == []
            events.availability("prices", Health.DEGRADED, "Retained prices remain usable.")
            update = await events.subscribe(initial["cursor"])
            assert [row["topic"] for row in update["events"]] == ["health"]
            update["events"][0]["value"]["state"] = "unavailable"
            assert events.snapshot()["health"][0]["state"] == "degraded"
            waiting = asyncio.create_task(events.subscribe(update["cursor"], timeout=1))
            await asyncio.sleep(0)
            events.availability("prices", Health.AVAILABLE, "Fresh prices available.")
            assert (await waiting)["events"][0]["value"]["state"] == "available"
            cursor = events.cursor
            for index in range(513):
                events.availability("prices", Health.AVAILABLE, str(index))
            assert events.changes(cursor)["reset"]
            assert events.changes("another-process:1")["reset"]
            assert (await events.subscribe(events.cursor, timeout=0.001))["events"] == []
        finally:
            repository.close()

    asyncio.run(scenario())


def test_failure_recovery_notifications_and_six_hour_schedule(tmp_path):
    """An empty success is not recovery. Availability and notices require explicit domain publication."""

    async def scenario():
        """Retain real repeated attempts while keeping the independent notification contract."""
        repository = SqlPlanRepository(tmp_path)
        try:
            events = Events(ActivityStore(repository.engine), lambda: NOW)
            first = await events.start("weather", "weather", "Fetch weather")
            await events.finish(first, Outcome.FAILED, "Retained forecast still usable.")
            second = await events.start("weather", "weather", "Fetch weather")
            with pytest.raises(ValueError, match="usable result"):
                await events.finish(second, Outcome.COMPLETED, "Empty response.")
            await events.finish(second, Outcome.FAILED, "No usable weather returned.")
            third = await events.start("weather", "weather", "Fetch weather")
            await events.finish(third, Outcome.COMPLETED, "Usable forecast received.", usable=True)
            await events.flush()
            rows = events.store.read(NOW - timedelta(days=1), NOW + timedelta(days=1))
            assert len(rows) == 3
            by_id = {row["id"]: row for row in rows}
            assert not by_id[second]["recovered"] and by_id[third]["recovered"]
            assert events.snapshot()["notifications"] == {}
            assert events.snapshot()["health"][0]["state"] == "unknown"
            events.scheduled("weather", "weather", "Fetch weather", NOW + timedelta(hours=6))
            events.scheduled("models", "training", "Train model", NOW + timedelta(hours=7))
            assert [row["owner"] for row in events.snapshot()["scheduled"]] == ["weather", "models"]
            events.scheduled("weather", "weather", "Fetch weather")
            assert [row["owner"] for row in events.snapshot()["scheduled"]] == ["models"]
        finally:
            repository.close()

    asyncio.run(scenario())


def test_restart_interruption_retention_and_stable_paging(tmp_path):
    """Seven-day retention survives restart. Equal start timestamps never skip or duplicate executions."""

    async def scenario():
        """Create unfinished and completed real records in the shared database."""
        repository = SqlPlanRepository(tmp_path)
        try:
            store = ActivityStore(repository.engine)
            events = Events(store, lambda: NOW)
            first = await events.start("planning", "planning", "Calculate plan")
            second = await events.start("solar", "training", "Update solar forecast")
            await events.finish(second, Outcome.COMPLETED, "Forecast installed.", usable=True)
            await events.flush()
            store.restart(NOW + timedelta(hours=1))
            rows = store.read(NOW - timedelta(days=1), NOW + timedelta(days=1), limit=1)
            next_rows = store.read(
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                (rows[0]["started_at"], rows[0]["id"]),
            )
            assert {row["id"] for row in rows + next_rows} == {first, second}
            states = {row["id"]: row["state"] for row in rows + next_rows}
            assert states == {first: "interrupted", second: "completed"}
            store.restart(NOW + timedelta(days=8))
            assert store.read(NOW - timedelta(days=1), NOW + timedelta(days=9)) == []
        finally:
            repository.close()

    asyncio.run(scenario())


def test_api_authentication_day_validation_and_live_cursor(tmp_path):
    """Both APIs reuse existing authorization. History defaults to the installation day."""

    async def scenario():
        """Drive the actual app with no HA connection or real user data."""
        app = create_app(tmp_path, TOKEN)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/v1/events")).status == 401
            assert (await client.get("/v1/activities")).status == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            response = await client.get("/v1/events", headers=headers)
            initial = await response.json()
            assert initial["reset"]
            identity = await app[EVENTS].start(
                "prices:nord_pool",
                "prices",
                "Fetch published prices",
                context={"label": "Nord Pool", "private_url": "PRIVATE_CONTEXT_SENTINEL"},
            )
            await app[EVENTS].finish(
                identity, Outcome.COMPLETED, "Market: NL. Usable prices received.", usable=True
            )
            changes = await (
                await client.get(
                    "/v1/events", params={"cursor": initial["cursor"]}, headers=headers
                )
            ).json()
            assert any(
                row["topic"] == "activity"
                and row["value"]["id"] == identity
                and row["value"]["context"] == {"label": "Nord Pool"}
                for row in changes["events"]
            )
            await app[EVENTS].flush()
            history = await (await client.get("/v1/activities", headers=headers)).json()
            assert history["timezone"] == "UTC"
            assert any(
                row["id"] == identity and row["context"] == {"label": "Nord Pool"}
                for row in history["activities"]
            )
            assert "PRIVATE_CONTEXT_SENTINEL" not in str(changes) + str(history)
            assert (await client.get("/v1/activities?day=invalid", headers=headers)).status == 400
            assert (
                await client.get("/v1/activities?before=invalid", headers=headers)
            ).status == 400

    asyncio.run(scenario())


def test_history_page_limits_tied_cursors_and_exact_last_page(tmp_path):
    """Bound filtered pages and avoid an empty extra page, including equal start times."""
    from types import SimpleNamespace

    from aiohttp import web

    from open_energy_planner.activities.api import register

    async def scenario():
        """Read real stored executions through the isolated public history route."""
        repository = SqlPlanRepository(tmp_path)
        app = web.Application()
        events = register(app, repository.engine, SimpleNamespace(document=dict), lambda: "UTC")
        events.clock = lambda: NOW
        try:
            async with TestClient(TestServer(app)) as client:
                expected = set()
                for activity_index in range(57):
                    kind = "weather" if activity_index < 50 else "prices"
                    identity = await events.start(kind, kind, "Synthetic activity")
                    await events.finish(
                        identity, Outcome.COMPLETED, "Accepted synthetic data.", usable=True
                    )
                    if kind == "weather":
                        expected.add(identity)
                params = {"day": "2026-10-25", "kind": "weather", "limit": "25"}
                first = await (await client.get("/v1/activities", params=params)).json()
                second = await (
                    await client.get("/v1/activities", params={**params, "before": first["next"]})
                ).json()
                assert len(first["activities"]) == len(second["activities"]) == 25
                assert second["next"] is None
                identities = [row["id"] for row in first["activities"] + second["activities"]]
                assert len(set(identities)) == 50 and set(identities) == expected
                default = await (await client.get("/v1/activities")).json()
                assert len(default["activities"]) == 57 and default["next"] is None
                for invalid in ("0", "-1", "101", "1.5", "invalid"):
                    assert (
                        await client.get("/v1/activities", params={"limit": invalid})
                    ).status == 400
        finally:
            repository.close()

    asyncio.run(scenario())


def test_optional_writer_is_nonblocking_and_discloses_lost_updates(tmp_path):
    """A full queue cannot delay or reject domain work, and a new subscriber sees incomplete evidence."""

    async def scenario():
        """Keep the writer unscheduled until a bounded queue has deliberately filled."""
        repository = SqlPlanRepository(tmp_path)
        events = Events(ActivityStore(repository.engine), lambda: NOW)
        events.pending = asyncio.Queue(maxsize=1)
        try:
            identity = await events.start("planning", "planning", "Calculate plan")
            await events.finish(identity, Outcome.COMPLETED, "Plan published.", usable=True)
            assert events.running == {}
            assert events.snapshot()["lost_updates"] == 1
            assert not events.snapshot()["history_available"]
            await events.close()
            assert events.changes()["snapshot"]["lost_updates"] == 1
        finally:
            repository.close()

    asyncio.run(scenario())


def test_threaded_producer_drains_and_restores_recovery_after_restart(tmp_path):
    """Threaded model work publishes on the app loop, with deterministic same-instant outcome order."""

    async def scenario():
        """Use actual SQL persistence and restart reconstruction, without a broker or mock store."""
        repository = SqlPlanRepository(tmp_path)
        store = ActivityStore(repository.engine)
        events = Events(store, lambda: NOW)
        events.loop = asyncio.get_running_loop()
        try:

            def calculate():
                """Publish the actual worker-owned failure before propagating its domain exception."""
                with events.thread_attempt(
                    "models",
                    "training",
                    "Compare models",
                    failure="Baseline models remain active.",
                    context={"label": "Household"},
                ):
                    raise ValueError("Synthetic numerical failure")

            with pytest.raises(ValueError, match="Synthetic numerical"):
                await asyncio.to_thread(calculate)
            await events.close()
            restored = Events(store, lambda: NOW)
            restored.failed, restored.sequence = store.restart(NOW)
            assert restored.failed == {("models", "training")}
            identity = await restored.start("models", "training", "Compare models")
            await restored.finish(identity, Outcome.COMPLETED, "Usable evidence.", usable=True)
            await restored.close()
            assert store.restart(NOW)[0] == set()
            rows = store.read(NOW - timedelta(days=1), NOW + timedelta(days=1))
            assert next(row for row in rows if row["id"] == identity)["recovered"]
            failed = next(row for row in rows if row["state"] == "failed")
            assert failed["context"] == {"label": "Household"}
        finally:
            repository.close()

    asyncio.run(scenario())


def test_automatic_ha_price_delivery_has_one_activity_and_actual_transport(tmp_path):
    """Pending bridge delivery is not a failed request or a new activity on every refresh poll."""
    from open_energy_planner.prices.adapters.home_assistant import AutomaticPublishedPrices
    from open_energy_planner.prices.service import PriceService
    from open_energy_planner.prices.storage.adapters.store import PriceStore
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from tests.app.price.test_ha_prices import report, response

    async def scenario():
        """Request and accept a complete DST delivery day through the actual HA normalization path."""
        repository = SqlPlanRepository(tmp_path)
        now = NOW.replace(hour=0)
        events = Events(ActivityStore(repository.engine), lambda: now)
        try:
            source = AutomaticPublishedPrices("NL", None)
            service = PriceService(
                "NL", source, None, PriceStore(SqlDocuments(repository.engine, "prices"))
            )
            service.events = events
            await service.refresh(now)
            identity = next(iter(events.running))
            await service.refresh(now + timedelta(minutes=1))
            assert list(events.running) == [identity]
            assert (
                "Waiting for the requested Home Assistant price delivery"
                in events.running[identity]["detail"]
            )
            body = report(source, now)
            body["responses"] = [response(source, now.date(), now)]
            source.receive(body, now)
            service.attempts.clear()
            await service.refresh(now)
            assert not events.running
            await events.close()
            rows = events.store.read(now - timedelta(days=1), now + timedelta(days=1))
            assert len(rows) == 1
            assert rows[0]["id"] == identity and rows[0]["state"] == "completed"
            assert rows[0]["owner"] == "prices:nord_pool:NL"
            assert "Transport: Home Assistant" in rows[0]["detail"]
            assert not rows[0]["recovered"]
        finally:
            repository.close()

    asyncio.run(scenario())


def test_managed_gateway_shutdown_releases_active_subscription(tmp_path, monkeypatch, caplog):
    """The outer maintenance gateway cannot wait on a stream that only inner shutdown can release."""
    from open_energy_planner.runtime.maintenance import MAINTENANCE, create_managed_app

    async def scenario():
        """Observe actual request admission without replacing the underlying subscription behavior."""
        app = create_managed_app(tmp_path, TOKEN, factory=create_app, source="home_assistant")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            initial = await (await client.get("/v1/events", headers=headers)).json()
            events = app[MAINTENANCE].runner.app[EVENTS]
            entered = asyncio.Event()
            original = events.subscribe

            async def observe(cursor=None, timeout=25):
                """Signal that the live request reached the real event collector."""
                entered.set()
                return await original(cursor, timeout)

            monkeypatch.setattr(events, "subscribe", observe)
            pending = asyncio.create_task(
                client.get("/v1/events", params={"cursor": initial["cursor"]}, headers=headers)
            )
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(client.server.close(), 2)
            response = await pending
            assert response.status == 200
        finally:
            await client.close()

    with caplog.at_level(logging.INFO, logger="open_energy_planner.runtime.diagnostics.logging"):
        asyncio.run(scenario())
    assert [
        record.message.split(" (release=", 1)[0]
        for record in caplog.records
        if record.name == "open_energy_planner.runtime.diagnostics.logging"
    ] == [
        "Open Energy Planner starting",
        "Open Energy Planner started",
        "Open Energy Planner stopping",
        "Open Energy Planner stopped",
    ]


@pytest.mark.parametrize("failure", [TypeError, KeyError, RuntimeError])
def test_history_writer_survives_unexpected_row_failure(tmp_path, monkeypatch, failure, caplog):
    """One rejected row discloses evidence loss without stranding later writes or shutdown."""

    async def scenario():
        """Fail the first persistence attempt and accept the terminal record afterward."""
        repository = SqlPlanRepository(tmp_path)
        store = ActivityStore(repository.engine)
        events = Events(store, lambda: NOW)
        write = store.write
        attempts = []

        def fail_once(row, now):
            """Inject a non-backend failure before the real store accepts subsequent rows."""
            attempts.append(row["state"])
            if len(attempts) == 1:
                raise failure("PRIVATE_ACTIVITY_VALUE_SQL_AND_PARAMETER")
            write(row, now)

        monkeypatch.setattr(store, "write", fail_once)
        try:
            identity = await events.start("weather", "weather", "Fetch weather")
            await events.finish(identity, Outcome.COMPLETED, "Accepted weather.", usable=True)
            await asyncio.wait_for(events.close(), 3)
            assert attempts == ["running", "completed"]
            assert events.history_available and events.lost_updates == 1
            assert store.read(NOW, NOW + timedelta(days=1))[0]["state"] == "completed"
        finally:
            repository.close()

    asyncio.run(scenario())
    assert "PRIVATE_ACTIVITY_VALUE_SQL_AND_PARAMETER" not in caplog.text
    diagnostics = [row for row in caplog.records if row.name.endswith("activities.events")]
    assert diagnostics and all(not row.exc_info for row in diagnostics)
    assert failure.__name__ in diagnostics[0].getMessage()


@pytest.mark.parametrize(
    ("operation", "result_status", "elapsed", "expected_outcome"),
    [
        (operation, status, timedelta(), outcome)
        for operation in ("capture", "evaluate")
        for status, outcome in (
            ("pending", "skipped"),
            ("failed", "failed"),
            ("commit_failed", "failed"),
            ("ok", "completed"),
        )
    ]
    + [
        ("evaluate", "pending", timedelta(hours=24) - timedelta(microseconds=1), "skipped"),
        ("evaluate", "pending", timedelta(hours=24), "failed"),
        ("evaluate", "pending", timedelta(hours=24) + timedelta(microseconds=1), "failed"),
    ],
)
def test_model_activity_waits_for_accepted_evidence(
    tmp_path, monkeypatch, operation, result_status, elapsed, expected_outcome
):
    """Only retryable evidence is pending. Abandonment fails and accepted writes alone recover."""
    from copy import deepcopy
    from types import SimpleNamespace

    from open_energy_planner.forecast_selection.runtime import ModelSelection
    from open_energy_planner.forecast_selection.storage import SelectionStore
    from open_energy_planner.solar.calculation.model import version
    from open_energy_planner.storage.adapters.documents import SqlDocuments

    async def scenario():
        """Run actual model advancement in its worker thread against the real SQL evidence store."""
        repository = SqlPlanRepository(tmp_path)
        store = SelectionStore(SqlDocuments(repository.engine, "planning"))
        events = Events(ActivityStore(repository.engine), lambda: NOW)
        events.loop = asyncio.get_running_loop()
        capture = {
            "start": (NOW - timedelta(days=2)).isoformat(),
            "end": NOW.isoformat(),
            "original_plan_id": "synthetic-plan",
            "contract_version": "synthetic-contract",
        }
        original = {
            "valid_until": (NOW + timedelta(hours=1)).isoformat(),
            "market_area": "NL",
            "currency": "EUR",
            "input_snapshot": {
                "problem": {
                    "intervals": [
                        {
                            "start": (NOW + timedelta(hours=48)).isoformat(),
                            "duration_h": 1,
                        }
                    ]
                }
            },
        }
        family = {"windows": [], "pending": None}
        if operation == "evaluate":
            family["pending"] = version(capture)
            store.commit("family", family, capture=capture)
        runtime = ModelSelection(
            store,
            SimpleNamespace(get_plan=lambda _identity: original),
            SimpleNamespace(frames=lambda *args, **kwargs: [], published=lambda *args: []),
            SimpleNamespace(load=lambda _identity: None),
            None,
        )
        runtime.events = events
        status = "ok" if result_status in ("ok", "commit_failed") else result_status
        result = {
            "status": status,
            "value": capture if operation == "capture" else {"score": 1},
            "issue": "Settlement evidence is pending."
            if status == "pending"
            else "Calculation failed.",
        }
        monkeypatch.setattr(runtime, "_execute", lambda _payload: result)
        prior_state = deepcopy(store.state)
        commit = store.commit

        def accept(*args, **kwargs):
            """Verify no usable completion is published before the owning store commits."""
            assert events.running
            if result_status == "commit_failed":
                raise OSError("synthetic commit failure")
            commit(*args, **kwargs)

        monkeypatch.setattr(store, "commit", accept)
        try:
            if result_status == "pending":
                # A pending first attempt must not create a failure episode.
                assert events.failed == set()
            else:
                failed = await events.start("models", "training", "Earlier comparison")
                await events.finish(failed, Outcome.FAILED, "Earlier failure.")
            cursor = events.cursor
            if result_status == "commit_failed":
                with pytest.raises(OSError, match="synthetic commit"):
                    await asyncio.to_thread(
                        runtime._advance, original, {"family_id": "family"}, family, NOW + elapsed
                    )
                assert store.state == prior_state
            else:
                assert await asyncio.to_thread(
                    runtime._advance, original, {"family_id": "family"}, family, NOW + elapsed
                )
            await events.flush()
            terminal = [
                row["value"]
                for row in events.changes(cursor)["events"]
                if row["topic"] == "activity" and row["value"]["state"] != "running"
            ]
            assert len(terminal) == 1
            row = terminal[0]
            assert row["state"] == expected_outcome
            assert row["recovered"] is (result_status == "ok")
            assert row["usable"] is (result_status == "ok")
            assert (("models", "training") in events.failed) is (expected_outcome == "failed")
            health = events.health["models"]
            assert health["state"] == (
                "available"
                if result_status == "ok"
                else "unknown"
                if expected_outcome == "skipped"
                else "degraded"
            )
            if result_status == "pending" and operation == "evaluate":
                retained = store.state["families"]["family"]
                if expected_outcome == "skipped":
                    assert retained["pending"] == version(capture)
                    assert retained["retry_after"] is not None
                else:
                    assert retained["pending"] is None and retained["retry_after"] is None
                    assert (
                        store.read("evaluation", retained["last_failure"])["issue"]
                        == "Settlement evidence is pending."
                    )
                    assert "pending" not in row["detail"].lower()
                assert store.read("capture", version(capture)) == capture
        finally:
            await events.close()
            repository.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_kind", ["persistence", "superseded"])
def test_planning_activity_distinguishes_publication_failure_and_supersession(
    tmp_path, failure_kind
):
    """A publication error starts a failure episode. Deliberate supersession does not."""
    from open_energy_planner.planning import PlanningCoordinator, PublicationSuperseded
    from tests.app.planning.test_planning_pipeline import NOW as PLANNING_NOW
    from tests.app.planning.test_planning_pipeline import capture, service

    async def scenario():
        """Run two real planning generations with only the first publication rejected."""
        repository = SqlPlanRepository(tmp_path)
        events = Events(ActivityStore(repository.engine), lambda: PLANNING_NOW)
        attempts = []

        async def publish(document):
            """Distinguish failed persistence from an explicit stale-generation rejection."""
            attempts.append(document)
            if len(attempts) == 1:
                if failure_kind == "superseded":
                    raise PublicationSuperseded()
                raise OSError("Synthetic persistence failure")

        coordinator = PlanningCoordinator(service(), publish)
        coordinator.events = events
        try:
            coordinator.submit(capture(PLANNING_NOW))
            await coordinator.wait()
            rows = events.changes()["snapshot"]
            assert rows["running"] == []
            await events.flush()
            history = events.store.read(PLANNING_NOW, PLANNING_NOW + timedelta(days=1))
            assert len(history) == 1
            assert history[0]["state"] == ("failed" if failure_kind == "persistence" else "skipped")
            coordinator.submit(capture(PLANNING_NOW, 0.5))
            await coordinator.wait()
            await events.flush()
            history = events.store.read(PLANNING_NOW, PLANNING_NOW + timedelta(days=1))
            completed = next(row for row in history if row["state"] == "completed")
            assert completed["recovered"] is (failure_kind == "persistence")
        finally:
            await events.close()
            repository.close()

    asyncio.run(scenario())


def test_recalculation_preserves_the_last_established_planning_health(tmp_path, monkeypatch):
    """A background round does not revoke availability of the already published validated plan."""
    from open_energy_planner.planning import PlanningCoordinator
    from tests.app.planning.test_planning_pipeline import NOW as PLANNING_NOW
    from tests.app.planning.test_planning_pipeline import capture, service

    async def scenario():
        """Pause a real second calculation and inspect the public health events before completion."""
        repository = SqlPlanRepository(tmp_path)
        events = Events(ActivityStore(repository.engine), lambda: PLANNING_NOW)
        published = []

        async def publish(document):
            """Accept the validated document without altering the independent solver outcome."""
            published.append(document)

        planner = service()
        coordinator = PlanningCoordinator(planner, publish)
        coordinator.events = events
        entered, release = asyncio.Event(), asyncio.Event()
        calculate = planner.calculate

        async def pause(snapshot):
            """Hold the second numerical operation after its actual calculating transition."""
            entered.set()
            await release.wait()
            return await calculate(snapshot)

        try:
            coordinator.submit(capture(PLANNING_NOW))
            await coordinator.wait()
            assert events.health["planning"]["state"] == "available"
            cursor = events.cursor
            monkeypatch.setattr(planner, "calculate", pause)
            coordinator.submit(capture(PLANNING_NOW, 0.5))
            await asyncio.wait_for(entered.wait(), 3)
            assert coordinator.status == "calculating"
            assert events.health["planning"]["state"] == "available"
            assert not [row for row in events.changes(cursor)["events"] if row["topic"] == "health"]
            release.set()
            await coordinator.wait()
            assert len(published) == 2
            assert events.health["planning"]["state"] == "available"
        finally:
            release.set()
            await coordinator.wait()
            await events.close()
            repository.close()

    asyncio.run(scenario())
