"""Prove shared problem lifecycle and ownership without any OEP-specific cause codes."""

import logging
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from open_energy_planner.notifications.collection import NotificationCenter
from open_energy_planner.notifications.model import Lifecycle, NoticeText, Notification


def test_temporary_lifetime_is_optional_source_owned_presentation():
    """Expose the declared lifetime without timing out notices or altering permanent warnings."""
    cause = Notification("example", "updated", NoticeText("Updated", "Settings accepted"))
    assert "auto_clear_minutes" not in cause.document("example")
    temporary = replace(cause, auto_clear_minutes=5)
    center = NotificationCenter()
    center.register("example", lambda: (temporary,))
    assert center.document()["notifications"][0]["auto_clear_minutes"] == 5
    assert center.document()["notifications"][0]["key"] == "example"


@pytest.mark.parametrize("minutes", [0, -1, True, "5", 1.5])
def test_temporary_lifetime_rejects_invalid_minutes(minutes):
    """Malformed duration hints cannot enter the trusted notification projection."""
    with pytest.raises(ValueError, match="positive whole-minute"):
        Notification(
            "example",
            "updated",
            NoticeText("Updated", "Settings accepted"),
            auto_clear_minutes=minutes,
        )


def test_source_confirms_recovery_without_dismissal_or_domain_rules():
    """One stable cause remains while recovering and disappears only after a fresh empty result."""
    since = datetime(2026, 10, 31, 12, tzinfo=UTC)
    cause = Notification(
        "example:resource:input",
        "input_missing",
        NoticeText("Missing input", "Restore the input"),
        blocks=("example_operation",),
        since=since,
    )
    state = [(cause,)]
    center = NotificationCenter()
    center.register("example", lambda: state[0])
    active = center.document()["notifications"][0]
    assert active["key"] == "example:resource:input"
    assert active["owner"] == "example"
    assert active["state"] == "active"
    assert active["blocks"] == ["example_operation"]
    assert active["since"] == "2026-10-31T12:00:00+00:00"
    state[0] = (
        Notification(
            cause.key,
            "recovering",
            NoticeText("Recovering", "Checking the result"),
            state=Lifecycle.RECOVERING,
            blocks=cause.blocks,
            since=since,
        ),
    )
    recovering = center.document()["notifications"][0]
    assert recovering["key"] == active["key"]
    assert recovering["state"] == "recovering"
    assert recovering["since"] == active["since"]
    state[0] = None
    assert center.document()["notifications"][0]["stale"] is True
    state[0] = ()
    assert center.document() == {"notifications": [], "unavailable_sources": []}


@pytest.mark.parametrize("fallback_first", [False, True])
def test_owner_wins_over_dependent_symptom_without_matching_messages(fallback_first):
    """A propagated cause appears once while unrelated resources with identical wording remain distinct."""
    text = NoticeText("Needs attention", "Review this resource")
    owner = Notification("resource:one", "source_fault", text)
    dependent = Notification(
        "resource:one", "operation_blocked", NoticeText("Blocked", "A dependency failed")
    )
    other = Notification("resource:two", "source_fault", text)
    center = NotificationCenter()
    sources = [("owner", lambda: (owner, other), False), ("dependent", lambda: (dependent,), True)]
    for name, read, fallback in reversed(sources) if fallback_first else sources:
        center.register(name, read, fallback=fallback)
    result = center.document()["notifications"]
    assert [notice["key"] for notice in result] == ["resource:one", "resource:two"]
    assert result[0]["owner"] == "owner"
    assert result[0]["fallback"] is False
    assert result[0]["code"] == "source_fault"


def test_failed_source_cannot_clear_its_problem_or_hide_another_owner(caplog):
    """Retain failure evidence, update healthy sources and redact private exception payloads."""
    failure = [False]
    private_payload = "private-token-must-not-be-logged"
    cause = Notification("source:resource", "missing", NoticeText("Missing", "Restore input"))
    other = [
        Notification("other:resource", "other_fault", NoticeText("Other", "Check other input"))
    ]

    def read():
        """Simulate a source becoming unreadable after its first successful report."""
        if failure[0]:
            raise OSError(private_payload)
        return (cause,)

    center = NotificationCenter()
    center.register("source", read)
    center.register("other", lambda: tuple(other))
    assert len(center.document()["notifications"]) == 2
    failure[0] = True
    other.clear()
    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            document = center.document()
    assert document["unavailable_sources"] == ["source"]
    assert [notice["key"] for notice in document["notifications"]] == ["source:resource"]
    assert document["notifications"][0]["stale"] is True
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None
    assert "private-token" not in caplog.text
    failure[0] = False
    assert center.document()["notifications"][0]["stale"] is False


def test_first_unknown_or_malformed_source_never_claims_successful_resolution():
    """An unreadable initial source has explicit uncertainty even without a runtime cache."""
    state = [None]
    center = NotificationCenter()
    center.register("new_source", lambda: state[0])
    assert center.document() == {"notifications": [], "unavailable_sources": ["new_source"]}
    state[0] = ({"invalid": "payload"},)
    assert center.document()["unavailable_sources"] == ["new_source"]
    state[0] = ()
    assert center.document() == {"notifications": [], "unavailable_sources": []}


def test_healthy_source_reads_do_not_reset_a_failing_explanation_transition(caplog):
    """Read and explanation failures recover independently without logging the same ongoing error again."""
    failure = [True]
    private_payload = "private explanation input"

    def explain(_reason, _detail):
        """Fail cause explanation independently of the module's healthy current-status reader."""
        if failure[0]:
            raise ValueError(private_payload)

    center = NotificationCenter()
    center.register("source", lambda: (), explain=explain)
    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            assert center.document()["unavailable_sources"] == []
            assert center.explain("cause") is None
        assert len(caplog.records) == 1
        failure[0] = False
        assert center.explain("cause") is None
        failure[0] = True
        assert center.explain("cause") is None
    assert len(caplog.records) == 2
    assert "private explanation input" not in caplog.text


def test_resolved_input_cause_keeps_dependent_operation_recovery_visible():
    """A healthy owner cannot clear the same cause while its dependent operation still confirms recovery."""
    key = "resource:one"
    owner = [(Notification(key, "input_missing", NoticeText("Missing input", "Restore input")),)]
    dependent = [
        (
            Notification(
                key,
                "checking",
                NoticeText("Checking operation", "Wait for the result"),
                state=Lifecycle.RECOVERING,
                blocks=("example_operation",),
            ),
        )
    ]
    center = NotificationCenter()
    center.register("owner", lambda: owner[0])
    center.register("dependent", lambda: dependent[0], fallback=True)
    assert center.document()["notifications"][0]["owner"] == "owner"
    owner[0] = ()
    recovering = center.document()["notifications"]
    assert len(recovering) == 1
    assert recovering[0]["key"] == key
    assert recovering[0]["state"] == "recovering"
    assert recovering[0]["fallback"] is True
    dependent[0] = ()
    assert center.document()["notifications"] == []


def test_explanation_and_registration_are_owned_by_sources():
    """Future sources can explain arbitrary codes without editing collection or frontend code."""
    center = NotificationCenter()
    text = NoticeText("Resource needs attention", "Review its current input", level="info")
    center.register(
        "future", lambda: (), explain=lambda reason, _detail: text if reason == "new_code" else None
    )
    assert center.explain("new_code") == text
    assert center.explain("unclaimed") is None
    with pytest.raises(ValueError, match="unique"):
        center.register("future", lambda: ())
    center.document()
    with pytest.raises(RuntimeError, match="before reads"):
        center.register("later", lambda: ())


def test_notification_boundary_rejects_unsafe_actions_and_naive_time():
    """Invalid provider metadata cannot become trusted recovery links or fabricated timestamps."""
    with pytest.raises(ValueError, match="current installation"):
        NoticeText("Problem", "Explanation", "Open", "https://example.invalid")
    with pytest.raises(ValueError, match="timezone"):
        Notification(
            "one", "cause", NoticeText("Problem", "Explanation"), since=datetime(2026, 10, 31)
        )
    with pytest.raises(ValueError):
        NoticeText("Problem", "Explanation", level="unexpected")
