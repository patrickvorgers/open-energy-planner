"""Keep onboarding independent of expired commands left by earlier integration tests."""

import io
import json

import pytest

from script.home_assistant import provision_smoke


def snapshot(*, plan_id="old", input_id="previous", expires="2000-01-01T00:00:00+00:00"):
    """Describe old or replaced synthetic commands independently of the live wall clock."""
    return {
        "source": "synthetic",
        "enabled": True,
        "planning": {"status": "ready", "requested_snapshot_id": "new-input"},
        "plan": {"plan_id": plan_id, "input_snapshot_id": input_id, "valid_until": expires},
    }


def responses(monkeypatch, documents):
    """Record fixed-host requests while supplying deterministic API replies without a server."""
    pending = iter(documents)
    calls = []

    def respond(request, timeout):
        """Return the next JSON response and retain request method/body for boundary assertions."""
        calls.append((request.full_url, request.get_method(), request.data))
        return io.BytesIO(json.dumps(next(pending)).encode())

    monkeypatch.setattr(provision_smoke, "urlopen", respond)
    monkeypatch.setattr(provision_smoke, "sleep", lambda _seconds: None)
    return calls


def test_onboarding_replaces_expired_commands_and_waits_for_its_own_fresh_plan(monkeypatch):
    """Prior-test expiry or an older ready result cannot pass. Only the requested new valid plan can."""
    calls = responses(
        monkeypatch,
        [
            snapshot(),
            {"planning": {"requested_snapshot_id": "new-input"}},
            snapshot(expires="2099-01-01T00:00:00+00:00"),
            snapshot(plan_id="new", input_id="new-input"),
            snapshot(plan_id="new", input_id="new-input", expires="2099-01-01T00:00:00+00:00"),
        ],
    )
    provision_smoke.fresh_synthetic_plan()
    assert len(calls) == 5
    assert calls[1] == ("http://app:8099/v1/replan", "POST", b"{}")
    assert all(address.startswith("http://app:8099/") for address, _method, _body in calls)


def test_onboarding_does_not_replan_a_live_installation(monkeypatch):
    """A live source is never changed by the synthetic onboarding preparation."""
    calls = responses(monkeypatch, [{"source": "home_assistant"}])
    with pytest.raises(RuntimeError, match="isolated synthetic"):
        provision_smoke.fresh_synthetic_plan()
    assert calls == [("http://app:8099/v1/snapshot", "GET", None)]


def test_onboarding_rejects_failed_planning_instead_of_reusing_old_commands(monkeypatch):
    """An explicit solver failure cannot pass merely because a previous plan is retained."""
    failed = snapshot(expires="2099-01-01T00:00:00+00:00")
    failed["planning"]["status"] = "failed"
    responses(
        monkeypatch, [snapshot(), {"planning": {"requested_snapshot_id": "new-input"}}, failed]
    )
    with pytest.raises(RuntimeError, match="failed validation"):
        provision_smoke.fresh_synthetic_plan()


def test_onboarding_plan_preparation_has_a_bounded_wait(monkeypatch):
    """A solver that never publishes cannot hang the onboarding test indefinitely."""
    responses(monkeypatch, [snapshot(), {"planning": {"requested_snapshot_id": "new-input"}}])
    clock = iter((0, 31))
    monkeypatch.setattr(provision_smoke, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not become ready"):
        provision_smoke.fresh_synthetic_plan()
