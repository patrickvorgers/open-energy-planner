"""Retain request history and defaults across real backend restart, retries and configuration replacement."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

from open_energy_planner.appliances.ev.service import EVService
from tests.app.ev.support.devices import CAR, NOW, POINT, SESSION, catalogue, observations, settings
from tests.database.support import ev as ev_support

ev_service = ev_support.ev_service


def test_no_request_means_no_work_even_when_connected_with_a_soc_deficit(ev_service):
    """Configuration defaults, refresh and restart never authorize a charging request."""
    ev_service.receive(observations(ev_service), NOW)
    ev_service.assign(POINT, CAR, SESSION, NOW)
    assert ev_service.work(NOW, "Europe/Amsterdam") == ()
    assert ev_service.document(NOW, "Europe/Amsterdam")["points"][0]["status"] == "request_missing"
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    restored.receive(observations(restored, NOW + timedelta(seconds=5)), NOW + timedelta(seconds=5))
    assert restored.work(NOW + timedelta(seconds=5), "Europe/Amsterdam") == ()
    assert restored.state["requests"] == {}


def test_request_override_cancellation_retry_and_history_survive_restart(ev_service):
    """A cancelled request remains cancelled after a repeated creation command on a new process."""
    body = {
        "request_id": str(uuid4()),
        "vehicle_id": CAR,
        "target_soc": 65,
        "departure": (NOW + timedelta(hours=2)).isoformat(),
    }
    defaults = deepcopy(ev_service.state["vehicles"])
    ev_service.save_request(body, NOW, "Europe/Amsterdam")
    current = ev_service.document(NOW, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    ev_service.cancel_request(current["id"], current["version"], NOW, CAR)
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    restored.save_request(body, NOW + timedelta(days=1), "Europe/Amsterdam")
    assert restored.document(NOW, "Europe/Amsterdam")["vehicles"][0]["charging_request"] is None
    assert restored.state["vehicles"] == defaults
    assert restored.request_history(catalogue())["requests"][0]["status"] == "cancelled"
    assert len(restored.state["requests"]) == 1


@pytest.mark.parametrize("change", ["replace_point", "remove_points"])
def test_replacing_configuration_clears_forecast_issue_and_preserves_request_history(
    ev_service, change
):
    """A cached issue for old point membership cannot survive configuration replacement."""
    body = {"request_id": str(uuid4()), "vehicle_id": CAR}
    ev_service.save_request(body, NOW, "Europe/Amsterdam")
    ev_service.issue = "Charger component forecast unavailable. EV history cannot be removed safely"
    replacement = settings()
    if change == "replace_point":
        replacement["points"][0]["id"] = str(uuid4())
    else:
        replacement["points"] = []
    ev_service.configure(replacement, catalogue(), NOW)
    assert ev_service.issue is None
    assert ev_service.document(NOW, "Europe/Amsterdam")["issue"] is None
    assert ev_service.request_history(catalogue())["requests"][0]["id"] == body["request_id"]


@pytest.mark.parametrize("invalid", ["no_plan", "expired", "changed_revision", "cancelled_request"])
def test_old_confirmed_execution_advice_requires_a_current_usable_plan(ev_service, invalid):
    """Retained confirmed power evidence cannot show stale advice for an obsolete request or plan."""
    ev_service.save_request(
        {"request_id": str(uuid4()), "vehicle_id": CAR}, NOW, "Europe/Amsterdam"
    )
    plan = {
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
        "steps": [
            {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "devices": {POINT: {"power_w": 2300}},
            }
        ],
    }
    ev_service.receive(observations(ev_service), NOW)
    ev_service.assign(POINT, CAR, SESSION, NOW)
    plan["ev_context"] = {"revision": ev_service.hard_revision(NOW, "Europe/Amsterdam")}
    assert not ev_service.execution_deviated(plan, NOW)
    now = NOW + timedelta(seconds=60)
    ev_service.receive(observations(ev_service, now), now)
    assert ev_service.execution_deviated(plan, now)
    assert ev_service.document(now, "Europe/Amsterdam", plan=plan)["points"][0]["execution_issue"]
    if invalid == "no_plan":
        plan = None
    elif invalid == "expired":
        plan["valid_until"] = now.isoformat()
    elif invalid == "changed_revision":
        plan["ev_context"]["revision"] = "old-request-revision"
    else:
        request = ev_service.document(now, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
        ev_service.cancel_request(request["id"], request["version"], now, CAR)
    assert (
        ev_service.document(now, "Europe/Amsterdam", plan=plan)["points"][0]["execution_issue"]
        is None
    )


def test_history_pages_keep_all_cancelled_requests_without_loading_them_into_live_projection(
    ev_service,
):
    """An independent 28 requests exceed the 25-row page and remain traversable exactly once."""
    expected = set()
    for index in range(28):
        now = NOW + timedelta(seconds=index)
        request_id = str(uuid4())
        expected.add(request_id)
        ev_service.save_request(
            {"request_id": request_id, "vehicle_id": CAR}, now, "Europe/Amsterdam"
        )
        request = ev_service.document(now, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
        ev_service.cancel_request(request_id, request["version"], now, CAR)
    first = ev_service.request_history(catalogue())
    second = ev_service.request_history(catalogue(), first["next_cursor"])
    assert len(first["requests"]) == 25
    assert len(second["requests"]) == 3
    assert second["next_cursor"] is None
    assert {row["id"] for row in first["requests"] + second["requests"]} == expected


def test_removed_vehicle_cancels_repeat_and_retains_history_after_restart(ev_service):
    """Removing configuration stops its future work without erasing the request outcome."""
    body = {"request_id": str(uuid4()), "vehicle_id": CAR, "repeat_weekdays": [0, 2, 4]}
    ev_service.save_request(body, NOW, "Europe/Amsterdam")
    replacement = settings()
    replacement["vehicles"] = []
    replacement["points"] = []
    ev_service.configure(replacement, catalogue(), NOW)
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    assert restored.state["active_requests"] == {}
    history = restored.request_history(catalogue())["requests"]
    assert len(history) == 1
    assert history[0]["status"] == "cancelled"
    assert history[0]["vehicle_name"] == "Removed vehicle"


@pytest.mark.parametrize("timestamp_kind", ["ha_report", "device_sample"])
@pytest.mark.parametrize("capture", ["current", "expired", "unknown_soc", "different_session"])
def test_completion_uses_available_capture_without_retiming_old_sample_provenance(
    ev_service, timestamp_kind, capture
):
    """Current available SoC can complete despite old provenance. Expired, unknown or unassigned evidence cannot."""
    configuration = settings()
    if timestamp_kind == "device_sample":
        configuration["vehicles"][0]["soc_timestamp_attribute"] = "sample_time"
    ev_service.configure(configuration, catalogue(), NOW)
    old_sample = (NOW - timedelta(days=3)).isoformat()
    initial = observations(ev_service, NOW, soc=85)
    initial["vehicles"][0].update(
        observed_at=old_sample, reported_at=old_sample, timestamp_kind=timestamp_kind
    )
    ev_service.receive(initial, NOW)
    ev_service.assign(POINT, CAR, SESSION, NOW)
    request_id = str(uuid4())
    ev_service.save_request({"request_id": request_id, "vehicle_id": CAR}, NOW, "Europe/Amsterdam")
    now = NOW + timedelta(seconds=5)
    report = observations(ev_service, now, soc=85)
    report["vehicles"][0].update(
        observed_at=old_sample, reported_at=old_sample, timestamp_kind=timestamp_kind
    )
    if capture == "unknown_soc":
        report["vehicles"][0].update(
            soc=None, observed_at=None, reported_at=None, timestamp_kind=None
        )
    elif capture == "different_session":
        report["points"][0]["session_id"] = str(uuid4())
    if capture == "expired":
        with pytest.raises(ValueError, match="stale"):
            ev_service.receive(report, now + timedelta(seconds=120), "Europe/Amsterdam")
    else:
        ev_service.receive(report, now, "Europe/Amsterdam")
    assert ev_service.state["requests"][request_id]["status"] == (
        "completed" if capture == "current" else "planned"
    )
    if capture in ("current", "different_session"):
        assert ev_service.report["vehicles"][0]["observed_at"] == old_sample
        assert ev_service.report["vehicles"][0]["reported_at"] == old_sample


def test_request_archive_payload_growth_is_linear_and_history_survives_restart(
    ev_service, record_testsuite_property
):
    """Eight then sixteen independently specified occurrences retain edits/outcomes without rearchiving all predecessors."""
    counts = []
    expected = {}
    checkpoints = []
    for index in range(16):
        now = NOW + timedelta(minutes=index * 5)
        ev_service.receive(observations(ev_service, now, soc=50), now)
        ev_service.assign(POINT, CAR, SESSION, now)
        request_id = str(uuid4())
        ev_service.save_request(
            {"request_id": request_id, "vehicle_id": CAR, "target_soc": 65}, now, "Europe/Amsterdam"
        )
        current = ev_service.document(now, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
        if index == 0:
            checkpoints.append(
                (ev_service.documents.read("ev-state.json"), {request_id: ("planned", 65)})
            )
        ev_service.save_request(
            {
                "request_id": request_id,
                "vehicle_id": CAR,
                "target_soc": 75,
                "departure": current["departure"],
                "expected_version": current["version"],
            },
            now + timedelta(seconds=1),
            "Europe/Amsterdam",
        )
        current = ev_service.document(now, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
        if index % 2 == 0:
            ev_service.cancel_request(
                request_id, current["version"], now + timedelta(seconds=2), CAR
            )
            expected[request_id] = "cancelled"
        else:
            ev_service.receive(
                observations(ev_service, now + timedelta(seconds=2), soc=85),
                now + timedelta(seconds=2),
            )
            expected[request_id] = "completed"
        if index in (7, 15):
            documents = list(ev_service.documents.iter_prefix("ev-"))
            counts.append(
                sum(
                    len(document.get("requests", {}))
                    + int("vehicle_id" in document and "status" in document)
                    for document in documents
                )
            )
            checkpoints.append(
                (
                    ev_service.documents.read("ev-state.json"),
                    {request_id: (status, 75) for request_id, status in expected.items()},
                )
            )
    record_testsuite_property(
        "ev_request_records_8_16_" + ev_service.documents.engine.dialect.name, str(counts)
    )
    assert counts[1] <= counts[0] * 2.5, counts
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    assert {
        row["id"]: row["status"] for row in restored.request_history(catalogue())["requests"]
    } == expected
    assert all(request["target_soc"] == 75 for request in restored.state["requests"].values())
    for archived, outcomes in checkpoints:
        retained = ev_support.reconstruct_requests(ev_service.documents, archived)
        assert {
            request_id: (request["status"], request["target_soc"])
            for request_id, request in retained.items()
        } == outcomes


def test_request_versions_and_compact_state_roll_back_together(ev_service, monkeypatch):
    """A failed current-state write must leave neither a new request nor a partial immutable history."""
    before = sorted(ev_service.documents.iter_prefix("ev-"), key=lambda row: str(row))
    original_write = ev_service.documents.write

    def fail_pointer(name, document):
        """Fail after request/version persistence but before the global pointer can commit."""
        if name == "ev-state.json":
            raise OSError("Synthetic write interruption")
        original_write(name, document)

    monkeypatch.setattr(ev_service.documents, "write", fail_pointer)
    with pytest.raises(OSError):
        ev_service.save_request(
            {"request_id": str(uuid4()), "vehicle_id": CAR}, NOW, "Europe/Amsterdam"
        )
    assert sorted(ev_service.documents.iter_prefix("ev-"), key=lambda row: str(row)) == before
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    assert restored.state["requests"] == {}


def test_saved_vehicle_defaults_without_request_fields_remain_compatible(ev_service):
    """A released configuration document gains no implicit work and remains the parent of the first explicit request."""
    from open_energy_planner.demand.calculation.model import digest

    previous = {**settings(), "assignments": {}}
    previous_revision = digest(previous)
    ev_service.documents.write("ev-state-" + previous_revision + ".json", previous)
    ev_service.documents.write("ev-state.json", previous)
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    assert restored.state["requests"] == {}
    assert restored.state["active_requests"] == {}
    assert restored.state["vehicles"][0]["target_soc"] == 80
    restored.save_request({"request_id": str(uuid4()), "vehicle_id": CAR}, NOW, "Europe/Amsterdam")
    current = restored.documents.read("ev-state.json")
    assert current["previous_revision"] == previous_revision
    assert restored.documents.read("ev-state-" + previous_revision + ".json") == previous
    assert len(ev_support.reconstruct_requests(restored.documents, current)) == 1


def test_exact_creation_retry_after_vehicle_removal_preserves_cancelled_history(ev_service):
    """The original UUID/fingerprint remains a no-op after removal, while new intent still requires a configured vehicle."""
    body = {"request_id": str(uuid4()), "vehicle_id": CAR, "target_soc": 65}
    ev_service.save_request(body, NOW, "Europe/Amsterdam")
    current = ev_service.document(NOW, "Europe/Amsterdam")["vehicles"][0]["charging_request"]
    ev_service.configure({"vehicles": [], "points": []}, catalogue(), NOW)
    restored = EVService(ev_service.documents, ev_service.consumers, ev_service.appliances)
    before = deepcopy(restored.state)
    restored.save_request(body, NOW + timedelta(days=1), "Europe/Amsterdam")
    assert restored.state == before
    assert restored.state["requests"][body["request_id"]]["status"] == "cancelled"
    with pytest.raises(ValueError):
        restored.save_request({**body, "target_soc": 75}, NOW, "Europe/Amsterdam")
    with pytest.raises(ValueError, match="Unknown vehicle"):
        restored.save_request({**body, "request_id": str(uuid4())}, NOW, "Europe/Amsterdam")
    with pytest.raises(ValueError, match="Unknown vehicle"):
        restored.save_request(
            {**body, "expected_version": current["version"]}, NOW, "Europe/Amsterdam"
        )
