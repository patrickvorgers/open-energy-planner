"""Independent one-off, retry, override and HA-local repeating-request contracts."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from open_energy_planner.appliances.ev.requests import (
    active_request,
    advance_requests,
    cancel_request,
    departure,
    request_document,
    save_request,
)
from tests.app.ev.support.devices import CAR, NOW, POINT, SESSION, settings


def initial():
    """Saved vehicle defaults alone never imply a charging request."""
    return {
        **settings(),
        "assignments": {POINT: {"vehicle_id": CAR, "session_id": SESSION}},
        "requests": {},
        "active_requests": {},
    }


def create(state=None, *, repeat=(), deadline=None):
    """Specify an independent 65% override for a car whose default remains 80%."""
    body = {
        "request_id": str(uuid4()),
        "vehicle_id": CAR,
        "target_soc": 65,
        "departure": (deadline or NOW + timedelta(hours=2)).isoformat(),
        "repeat_weekdays": list(repeat),
    }
    return save_request(state or initial(), body, NOW, "Europe/Amsterdam"), body


def report(now, soc=70):
    """A current, identified physical connection proves target completion, not a proposed plan."""
    return {
        "at": now.isoformat(),
        "vehicles": [{"id": CAR, "soc": soc}],
        "points": [{"id": POINT, "connected": True, "session_id": SESSION}],
    }


def test_defaults_do_not_create_a_request_and_one_override_is_atomic():
    """A request is explicit. Its saved defaults survive creation and editing unchanged."""
    original = initial()
    assert active_request(original, CAR) is None
    state, body = create(original)
    request = active_request(state, CAR)
    assert request["target_soc"] == 65
    assert state["vehicles"] == original["vehicles"]
    edited = save_request(
        state,
        {**body, "target_soc": 75, "expected_version": request_document(request)["version"]},
        NOW,
        "Europe/Amsterdam",
    )
    assert active_request(edited, CAR)["id"] == body["request_id"]
    assert active_request(edited, CAR)["target_soc"] == 75
    assert edited["vehicles"][0]["target_soc"] == 80


def test_cancel_and_repeated_create_never_revive_a_request():
    """The same retried submission retains its original outcome after cancellation and restart."""
    state, body = create(repeat=range(7))
    request = active_request(state, CAR)
    cancelled = cancel_request(state, request["id"], request_document(request)["version"], NOW)
    assert active_request(cancelled, CAR) is None
    assert cancelled["requests"][request["id"]]["status"] == "cancelled"
    assert (
        save_request(deepcopy(cancelled), body, NOW + timedelta(days=1), "Europe/Amsterdam")
        == cancelled
    )
    assert cancel_request(cancelled, request["id"], "old-version", NOW) == cancelled
    assert (
        advance_requests(cancelled, report(NOW + timedelta(days=1)), NOW + timedelta(days=1))
        == cancelled
    )


def test_completion_preserves_history_and_does_not_repeat_by_default():
    """One measured completed charge has no implicit next-day obligation."""
    state, body = create()
    finished = advance_requests(
        state, report(NOW + timedelta(minutes=1)), NOW + timedelta(minutes=1)
    )
    assert active_request(finished, CAR) is None
    assert finished["requests"][body["request_id"]]["status"] == "completed"
    assert len(finished["requests"]) == 1


def test_explicit_repeat_creates_one_next_occurrence_without_reusing_completed_charge():
    """The next occurrence cannot be completed by the same early reading from the previous period."""
    state, body = create(repeat=range(7))
    now = NOW + timedelta(minutes=1)
    finished = advance_requests(state, report(now), now)
    next_request = active_request(finished, CAR)
    assert next_request["id"] != body["request_id"]
    assert next_request["ready_at"] == body["departure"]
    assert finished["requests"][body["request_id"]]["status"] == "completed"
    assert (
        advance_requests(finished, report(now + timedelta(seconds=1)), now + timedelta(seconds=1))
        == finished
    )
    assert len(finished["requests"]) == 2


@pytest.mark.parametrize(
    "day, expected",
    [
        (datetime(2026, 3, 28, 12, tzinfo=UTC), datetime(2026, 4, 5, 0, 30, tzinfo=UTC)),
        (datetime(2026, 10, 24, 12, tzinfo=UTC), datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
    ],
)
def test_weekday_repetition_uses_ha_timezone_and_one_fall_back_occurrence(day, expected):
    """Sunday 02:30 skips the spring gap and uses only the first fall-back occurrence."""
    actual = departure({"departure_time": "02:30"}, day, "Europe/Amsterdam", (6,))
    assert actual == expected
    following = departure(
        {"departure_time": "02:30"}, actual + timedelta(minutes=1), "Europe/Amsterdam", (6,)
    )
    assert following > actual + timedelta(days=6)


def test_stale_editor_and_second_active_request_cannot_replace_saved_intent():
    """Concurrent edits reject stale versions while exact retries preserve identity and values."""
    state, body = create()
    first = request_document(active_request(state, CAR))
    edited = save_request(
        state,
        {**body, "target_soc": 75, "expected_version": first["version"]},
        NOW,
        "Europe/Amsterdam",
    )
    with pytest.raises(ValueError, match="changed"):
        save_request(
            edited,
            {**body, "target_soc": 90, "expected_version": first["version"]},
            NOW,
            "Europe/Amsterdam",
        )
    with pytest.raises(ValueError, match="already has"):
        create(edited)
    assert len(edited["requests"]) == 1


def test_expired_repeating_request_skips_missed_days_and_cancel_stops_the_chain():
    """An offline week yields one future occurrence rather than replaying missed daily charges."""
    state, body = create(repeat=range(7))
    now = NOW + timedelta(days=8)
    advanced = advance_requests(state, report(now, soc=None), now)
    assert advanced["requests"][body["request_id"]]["status"] == "expired"
    following = active_request(advanced, CAR)
    assert datetime.fromisoformat(following["departure"]) > now
    assert len(advanced["requests"]) == 2
    stopped = cancel_request(advanced, following["id"], request_document(following)["version"], now)
    assert active_request(stopped, CAR) is None
    assert (
        advance_requests(stopped, report(now + timedelta(days=1)), now + timedelta(days=1))
        == stopped
    )


@pytest.mark.parametrize("days", [[True], [7], [-1], [0, 0], "Monday", [1.0]])
def test_invalid_repeat_days_cannot_change_saved_state(days):
    """Malformed weekday schedules fail before any request or default can be changed."""
    state = initial()
    before = deepcopy(state)
    with pytest.raises(ValueError, match="repeat weekdays"):
        save_request(
            state,
            {"request_id": str(uuid4()), "vehicle_id": CAR, "repeat_weekdays": days},
            NOW,
            "Europe/Amsterdam",
        )
    assert state == before


def test_stale_noop_edit_must_still_validate_the_saved_version():
    """Equal desired fields cannot turn an unrelated stale version into successful optimistic editing."""
    state, body = create()
    before = deepcopy(state)
    with pytest.raises(ValueError, match="changed"):
        save_request(state, {**body, "expected_version": "stale-editor"}, NOW, "Europe/Amsterdam")
    assert state == before
    assert save_request(state, body, NOW, "Europe/Amsterdam") == state


def test_editing_a_future_repeat_preserves_its_readiness_boundary():
    """Changing a future occurrence must not reuse the already completed period's charge."""
    state, body = create(repeat=range(7))
    now = NOW + timedelta(minutes=1)
    state = advance_requests(state, report(now), now)
    following = active_request(state, CAR)
    original_ready = following["ready_at"]
    edited = save_request(
        state,
        {
            "request_id": following["id"],
            "vehicle_id": CAR,
            "target_soc": 80,
            "departure": following["departure"],
            "repeat_weekdays": following["repeat_weekdays"],
            "expected_version": request_document(following)["version"],
        },
        now,
        "Europe/Amsterdam",
    )
    assert active_request(edited, CAR)["ready_at"] == original_ready == body["departure"]
    assert (
        advance_requests(
            edited, report(now + timedelta(seconds=1), soc=90), now + timedelta(seconds=1)
        )
        == edited
    )
    with pytest.raises(ValueError, match="readiness"):
        save_request(
            edited,
            {
                "request_id": following["id"],
                "vehicle_id": CAR,
                "departure": (now + timedelta(minutes=10)).isoformat(),
                "expected_version": request_document(active_request(edited, CAR))["version"],
            },
            now,
            "Europe/Amsterdam",
        )
