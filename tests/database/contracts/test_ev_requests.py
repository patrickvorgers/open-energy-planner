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
