"""Exercise blocker logging, recovery and authenticated read-only notification projection."""

import asyncio
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.build_info import BUILD_INFO
from open_energy_planner.contracts.api import ContractAPI
from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.demand.storage import DemandStore
from open_energy_planner.planning import PlanningCoordinator
from open_energy_planner.synthetic import capture
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_app import running
from tests.app.demand.test_demand_app import definition
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.app.history.test_history_app import prepare
from tests.app.notifications.support import notification_document
from tests.app.planning.test_live_app import wait_ready
from tests.app.planning.test_planning_pipeline import service

TOKEN = "notification-test-only-0123456789abcdef"


def test_negative_export_difference_is_advisory_and_clears_on_recovery():
    """A usable zero volume warns without claiming the source is wrong or recovery is certain."""
    document = notification_document(
        "ready", "validated", None, negative_export_meters=("sensor.export",)
    )
    notice = document["notifications"][0]
    assert notice["code"] == "bonus_statistics_negative_difference"
    assert notice["detail"] == "sensor.export"
    assert notice["action_path"] == "/contract"
    assert "intentional statistics correction or incorrect meter data" in notice["message"]
    assert "Further export can" in notice["message"]
    assert "Planning can continue" in notice["message"]
    assert "No new energy plan" not in notice["message"]
    assert notification_document("ready", "validated", None)["notifications"] == []


@pytest.mark.parametrize("retired_rule", [False, True])
def test_negative_export_notification_uses_real_accepted_endpoint_evidence(
    tmp_path, monkeypatch, retired_rule
):
    """An unrelated uninitialized old rule cannot hide the current rule's zero-volume advisory."""

    owners = []
    read_meters = ContractAPI.negative_export_meters

    def capture_owner(owner, now):
        """Expose the endpoint's owner for temporary source loss without replacing its evidence reader."""
        owners.append(owner)
        return read_meters(owner, now)

    monkeypatch.setattr(ContractAPI, "negative_export_meters", capture_owner)

    def invalid_evidence(*_args, **_kwargs):
        """Simulate a statistics validation failure without fabricating replacement measurements."""
        raise ValueError("bonus_counter_baseline_required")

    async def scenario():
        async with running(tmp_path, with_export=True) as (session, url):
            _request, body = await prepare(session, url)
            if retired_rule:
                contract = settings()
                contract["bonuses"] = [
                    {
                        "rule_id": identity,
                        "effective_from": effective,
                        "rate_formula": "0.04",
                        "cap_kwh": 100000,
                        "timezone": "UTC",
                    }
                    for identity, effective in (
                        ("retired", "2026-01-01T00:00:00+00:00"),
                        ("solar", "2026-02-01T00:00:00+00:00"),
                    )
                ]
                async with session.get(url + "/v1/contracts") as response:
                    contract["expected_version"] = (await response.json())["version"]
                async with session.put(url + "/v1/contracts", json=contract) as response:
                    assert response.status == 200
                async with session.get(url + "/v1/snapshot") as response:
                    request = (await response.json())["bonus_history"]["request"]
                body["request_version"] = request["version"]
            rows = body["statistics"][0]["hours"]
            rows[:] = [rows[0], rows[-1]]
            reference = rows[0][1]
            rows[-1][1] = reference - 1
            async with session.post(url + "/v1/history", json=body) as response:
                assert response.status == 200
            async with session.get(url + "/v1/notifications") as response:
                notices = (await response.json())["notifications"]
            assert any(
                notice["code"] == "bonus_statistics_negative_difference" for notice in notices
            )
            owner = owners[-1]
            for target, attribute, unavailable_value in (
                (owner, "live", None),
                (owner.live, "configuration", None),
                (owner.contracts, "issue", "contract_cache_unavailable"),
                (owner.bonus_counters, "issue", "counter_cache_unavailable"),
                (owner.bonus_counters, "current", None),
                (BonusCompiler, "current_evidence", invalid_evidence),
            ):
                with monkeypatch.context() as unavailable_source:
                    unavailable_source.setattr(target, attribute, unavailable_value)
                    async with session.get(url + "/v1/notifications") as response:
                        unavailable = await response.json()
                    assert "contract-statistics" in unavailable["unavailable_sources"]
                    retained = next(
                        notice
                        for notice in unavailable["notifications"]
                        if notice["code"] == "bonus_statistics_negative_difference"
                    )
                    assert retained["stale"] is True
                async with session.get(url + "/v1/notifications") as response:
                    restored = await response.json()
                assert "contract-statistics" not in restored["unavailable_sources"]
                assert (
                    next(
                        notice
                        for notice in restored["notifications"]
                        if notice["code"] == "bonus_statistics_negative_difference"
                    )["stale"]
                    is False
                )
            async with session.get(url + "/v1/contracts/budgets") as response:
                budgets = (await response.json())["budgets"]
                budget = next(item for item in budgets if item["rule_id"] == "solar")
            assert budget["used_kwh"] == 0
            if retired_rule:
                assert (
                    next(item for item in budgets if item["rule_id"] == "retired")["used_kwh"]
                    is None
                )
            async with session.get(url + "/v1/contracts") as response:
                version = (await response.json())["version"]
            contract = settings()
            contract["expected_version"] = version
            contract["bonuses"] = []
            async with session.put(url + "/v1/contracts", json=contract) as response:
                assert response.status == 200
            async with session.get(url + "/v1/notifications") as response:
                recovered = await response.json()
            assert "contract-statistics" not in recovered["unavailable_sources"]
            assert all(
                notice["code"] != "bonus_statistics_negative_difference"
                for notice in recovered["notifications"]
            )

    asyncio.run(scenario())


def test_notifications_remain_readable_while_configuration_persistence_waits(tmp_path, monkeypatch):
    """Read a consistent status without queuing behind a paused configuration write."""
    started, release = Event(), Event()
    save = DemandStore.save

    def paused_save(store, configuration):
        """Hold the existing mutation lock during an actual off-loop persistence call."""
        started.set()
        assert release.wait(10)
        return save(store, configuration)

    async def scenario():
        async with running_forecasts(tmp_path) as (session, url):
            await configure_forecasts(session, url)
            monkeypatch.setattr(DemandStore, "save", paused_save)
            write = asyncio.ensure_future(session.put(url + "/v1/demand", json=definition()))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                async with asyncio.timeout(2):
                    async with session.get(url + "/v1/notifications") as response:
                        assert response.status == 200
                        assert isinstance((await response.json())["notifications"], list)
                assert not write.done()
            finally:
                release.set()
                response = await write
                await response.read()
                response.release()

    asyncio.run(scenario())


def test_blocker_transitions_log_once_and_recovery_requires_publication(caplog):
    """Reason changes retain the original start and only a published plan closes the incident."""

    async def scenario():
        now = [datetime(2026, 10, 30, tzinfo=UTC)]
        published = []

        async def publish(document):
            """Observe actual successful publication independently of calculation readiness."""
            published.append(document)

        coordinator = PlanningCoordinator(
            service(clock=lambda: now[0]), publish, clock=lambda: now[0]
        )
        coordinator.invalidate("bonus_counter_baseline_required")
        first_start = coordinator.blocked_since
        coordinator.invalidate("bonus_counter_baseline_required")
        now[0] += timedelta(seconds=10)
        coordinator.invalidate("required_measurement_unavailable")
        assert coordinator.block_reason == "required_measurement_unavailable"
        assert coordinator.blocked_since == first_start
        assert len([message for message in caplog.messages if "Planning blocked:" in message]) == 2
        now[0] += timedelta(seconds=15)
        coordinator.submit(capture(now[0]))
        assert not any("Planning resumed" in message for message in caplog.messages)
        await coordinator.wait()
        assert len(published) == 1
        assert coordinator.blocked_since is None
        assert coordinator.block_reason is None
        resumed = [message for message in caplog.messages if "Planning resumed" in message]
        assert len(resumed) == 1
        assert "25.0 seconds" in resumed[0]
        assert "required_measurement_unavailable" in resumed[0]

    with caplog.at_level(logging.INFO, logger="open_energy_planner"):
        asyncio.run(scenario())


def test_notifications_require_authentication_and_do_not_refresh_or_write(tmp_path):
    """Public setup metadata reveals no blockers and authorized reads preserve the current state."""

    async def scenario():
        app = create_app(tmp_path, TOKEN, source="home_assistant")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/v1/notifications")).status == 401
            assert "notifications" not in await (await client.get("/setup/session")).json()
            headers = {"Authorization": "Bearer " + TOKEN}
            first = await (await client.get("/v1/notifications", headers=headers)).json()
            second = await client.get("/v1/notifications", headers=headers)
            assert second.headers["Cache-Control"] == "no-store"
            assert await second.json() == first
            assert first["notifications"][0]["code"] == "sources_not_configured"
            assert first["notifications"][0]["action_path"] == "/setup"
            snapshot = await (await client.get("/v1/snapshot", headers=headers)).json()
            assert snapshot["plan"] is None
            assert snapshot["enabled"] is True

    asyncio.run(scenario())


def test_bonus_rejection_is_actionable_deduplicated_and_clears_after_recovery(tmp_path, caplog):
    """Reject malformed annual statistics twice, expose one blocker, then recover through real HTTP."""

    async def scenario():
        async with running(tmp_path, with_export=True) as (session, url):
            _request, valid = await prepare(session, url)
            broken = deepcopy(valid)
            broken["statistics"][0]["hours"][0][1] = True
            for _ in range(2):
                async with session.post(url + "/v1/history", json=broken) as response:
                    assert response.status == 400
            errors = [
                message
                for message in caplog.messages
                if "Export bonus history unavailable:" in message
            ]
            assert len(errors) == 1
            assert "Invalid statistics sum" in errors[0]
            async with session.get(url + "/v1/notifications") as response:
                notification = (await response.json())["notifications"][0]
            assert notification["code"] == "bonus_counter_baseline_required"
            assert notification["detail"] == "incomplete_or_invalid_history"
            assert "Recorder" in notification["message"]
            assert notification["action_path"] == "/contract"
            assert "zero" not in notification["message"].lower()
            async with session.post(url + "/v1/history", json=valid) as response:
                assert response.status == 200
            await wait_ready(session, url)
            async with session.get(url + "/v1/notifications") as response:
                assert (await response.json())["notifications"] == []
            assert "Export bonus history is usable again" in caplog.messages

    with caplog.at_level(logging.INFO, logger="open_energy_planner"):
        asyncio.run(scenario())


def test_current_failure_and_recovery_projection_keep_unknowns_explicit():
    """Version mismatch and calculation failures remain visible until actual ready state."""
    since = datetime(2026, 10, 30, 12, 45, tzinfo=UTC)
    failed = notification_document("failed", "planning_error", since, version_status="mismatch")
    assert [notice["code"] for notice in failed["notifications"]] == [
        "release_mismatch",
        "planning_error",
    ]
    assert failed["notifications"][1]["since"] == "2026-10-30T12:45:00+00:00"
    recovering = notification_document(
        "calculating", None, since, block_reason="bonus_counter_baseline_required"
    )["notifications"][0]
    assert recovering["code"] == "planning_resuming"
    assert recovering["title"] == "Calculating a new energy plan"
    assert "This can take a few minutes" in recovering["message"]
    assert "not yet completed" in recovering["message"]
    assert recovering["action_path"] == "/contract"
    assert notification_document("ready", "validated", None) == {
        "notifications": [],
        "unavailable_sources": [],
    }
    unknown = notification_document("not_ready", "future_unknown_condition", since)[
        "notifications"
    ][0]
    assert unknown["title"] == "Planning is blocked"
    assert unknown["code"] == "future_unknown_condition"
    prefix = notification_document("not_ready", "bonus_statistics_prefix_required", since)[
        "notifications"
    ][0]
    assert "valid export statistics reference" in prefix["message"]
    assert prefix["action_path"] == "/contract"
    window = notification_document("not_ready", "bonus_window_unavailable", since)["notifications"][
        0
    ]
    assert "time window" in window["message"]
    assert "earlier export" not in window["message"]
    for reason in ("publication_inputs_expired", "stale_input", "input_expired_during_solve"):
        expired = notification_document("not_ready", reason, since)["notifications"][0]
        assert "measurements became too old" in expired["message"]
        assert "first transfer interval elapsed" in expired["message"]
        assert expired["action_path"] == "/overview"
        assert expired["level"] == "info"
    for reason in ("bonus_export_measurement_required", "bonus_export_measurement_expired"):
        measurement = notification_document("not_ready", reason, since)["notifications"][0]
        assert measurement["action_path"] == "/setup"
        assert "current readings" in measurement["message"]


def test_version_blocker_is_logged_once_and_recovers_on_an_identified_match(
    tmp_path, caplog, monkeypatch
):
    """Keep retained mismatch private and log it once until an actual matching peer clears it."""

    async def scenario():
        contact_time = [0.0]
        monkeypatch.setattr("open_energy_planner.build_info.monotonic", lambda: contact_time[0])
        async with TestClient(
            TestServer(create_app(tmp_path, TOKEN, source="home_assistant"))
        ) as client:
            headers = {"Authorization": "Bearer " + TOKEN, "X-OEP-Release": "wrong-release"}
            for _ in range(2):
                assert (await client.get("/v1/version", headers=headers)).status == 409
            reader = {"Authorization": "Bearer " + TOKEN}
            notice = await (await client.get("/v1/notifications", headers=reader)).json()
            assert notice["notifications"][0]["code"] == "release_mismatch"
            public = await (await client.get("/setup/session")).json()
            assert "blocking_mismatch" not in public["build_info"]
            version = await (await client.get("/v1/version", headers=reader)).json()
            assert version["blocking_mismatch"] is True
            contact_time[0] = 60.0
            version = await (await client.get("/v1/version", headers=reader)).json()
            assert version["status"] == "unavailable"
            assert version["blocking_mismatch"] is True
            public = await (await client.get("/setup/session")).json()
            assert "blocking_mismatch" not in public["build_info"]
            notice = await (await client.get("/v1/notifications", headers=reader)).json()
            assert notice["notifications"][0]["code"] == "release_mismatch"
            headers.update(
                {"X-OEP-Release": BUILD_INFO["version"], "X-OEP-Build": BUILD_INFO["build"] or ""}
            )
            for _ in range(2):
                assert (await client.get("/v1/version", headers=headers)).status == 200
            notice = await (await client.get("/v1/notifications", headers=reader)).json()
            assert all(item["code"] != "release_mismatch" for item in notice["notifications"])
            version = await (await client.get("/v1/version", headers=reader)).json()
            assert version["blocking_mismatch"] is False

    with caplog.at_level(logging.INFO, logger="open_energy_planner"):
        asyncio.run(scenario())
    assert (
        len([message for message in caplog.messages if "Integration data blocked:" in message]) == 1
    )
    assert (
        len(
            [message for message in caplog.messages if "Integration data accepted again" in message]
        )
        == 1
    )
