"""Verify blueprint bindings and one-time sidebar setup through HA's public API boundaries."""

import asyncio
import traceback
from copy import deepcopy
from types import SimpleNamespace

import pytest
from aiohttp import WSServerHandshakeError

from script.home_assistant.bootstrap import provision_ha_examples as ha_examples
from script.provisioning import device_examples as examples
from tests.supervisor.support.examples import HomeAssistantBoundary, run


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "authentication",
        "command",
        "unexpected_id",
        "connection",
        "handshake",
        "send",
        "unauthorized",
        "invalid_format",
        "unknown_command",
        "malformed",
    ],
)
def test_native_supervisor_command_uses_authenticated_bounded_websocket(failure):
    """Native panel reads use HA authentication and bounded receive/close without accepting API errors."""
    response = {
        "id": 2 if failure == "unexpected_id" else 1,
        "success": failure not in {"command", "unauthorized", "invalid_format", "unknown_command"},
        "error": {
            "code": failure
            if failure in {"unauthorized", "invalid_format", "unknown_command"}
            else "unknown_error"
        },
        "result": {"ingress_panel": True},
    }
    if failure == "malformed":
        del response["success"]
        response["error"] = {"code": "unauthorized"}
    messages = iter(
        [
            {"type": "auth_required"},
            {"type": "auth_invalid" if failure == "authentication" else "auth_ok"},
            response,
        ]
    )
    sent = []

    class Socket:
        """Provide protocol messages without opening an actual network connection."""

        async def __aenter__(self):
            """Open the isolated native channel."""
            return self

        async def __aexit__(self, *_exception):
            """Close this channel without suppressing authentication or command failures."""

        async def receive_json(self):
            """Return the next independently specified HA protocol message."""
            return next(messages)

        async def send_json(self, message):
            """Retain protocol output to check private authentication and the actual request."""
            sent.append(message)
            if failure == "send" and message["type"] == "supervisor/api":
                raise ConnectionError("Connection lost while sending the command")

    class Session:
        """Require the native path and independently specified timeout semantics."""

        def __init__(self):
            """Keep private test authorization on this transport instance."""
            self.headers = {"Authorization": "Bearer private-test-token"}

        def ws_connect(self, url, *, timeout):
            """Reject deprecated or unbounded transport settings."""
            assert url == "http://homeassistant/api/websocket"
            assert timeout.ws_receive == 15 and timeout.ws_close == 10
            if failure == "connection":
                raise ConnectionError("No WebSocket was opened")
            if failure == "handshake":
                raise WSServerHandshakeError(
                    SimpleNamespace(real_url=url, headers=self.headers),
                    (),
                    status=401,
                    message="Invalid response status",
                )
            return Socket()

    command = {
        "type": "supervisor/api",
        "endpoint": "/addons/local_open_energy_planner/info",
        "method": "get",
    }
    if failure:
        with pytest.raises(RuntimeError if failure != "send" else ConnectionError) as captured:
            asyncio.run(ha_examples.native_command(Session(), "http://homeassistant", command))
        safe_retry = failure in {
            "authentication",
            "connection",
            "handshake",
            "unauthorized",
            "invalid_format",
            "unknown_command",
        }
        assert isinstance(captured.value, ha_examples.CommandNotApplied) is safe_retry
        if failure == "authentication":
            assert str(captured.value) == "Home Assistant WebSocket authentication failed"
        if failure in {"connection", "handshake"}:
            expected_error = ConnectionError if failure == "connection" else WSServerHandshakeError
            assert isinstance(captured.value.__cause__, expected_error)
            diagnostics = "".join(traceback.format_exception(captured.value))
            expected_detail = (
                "No WebSocket was opened" if failure == "connection" else "Invalid response status"
            )
            assert expected_detail in diagnostics
            assert "private-test-token" not in diagnostics
    else:
        assert asyncio.run(
            ha_examples.native_command(Session(), "http://homeassistant", command)
        ) == {"ingress_panel": True}
        assert sent == [
            {"type": "auth", "access_token": "private-test-token"},
            {"id": 1, **command},
        ]


def test_first_start_adds_six_simulator_blueprints_and_sidebar(tmp_path, monkeypatch):
    """Three cycle, two charging and one boiler automation bind solely to synthetic equipment."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    run(boundary, journal, monkeypatch)
    assert boundary.sidebar and len(boundary.definitions) == 6
    dishwasher = boundary.definitions["oep_lab_dishwasher"]["use_blueprint"]
    assert dishwasher["path"] == "open_energy_planner/appliance.yaml"
    assert dishwasher["input"]["appliance"] == "virtual-dishwasher"
    assert dishwasher["input"]["start_action"] == [{"action": "oep_test_fixture.dishwasher_start"}]
    assert dishwasher["input"]["cancelled_trigger"] == [
        {"trigger": "state", "entity_id": "sensor.oep_test_dishwasher_status", "to": "cancelled"}
    ]
    point = boundary.definitions["oep_lab_wallbox_a"]["use_blueprint"]
    assert point["path"] == "open_energy_planner/ev.yaml"
    assert point["input"]["permission"] == "binary_sensor.renamed_wallbox_a_charging"
    assert point["input"]["power"] == "sensor.renamed_wallbox_a_power"
    assert point["input"]["charge_action"] == [
        {
            "action": "oep_test_fixture.ev_power",
            "data": {"point": 0, "power_w": "{{ requested_power_w }}"},
        }
    ]
    boiler = boundary.definitions["oep_lab_boiler"]["use_blueprint"]
    assert boiler["input"]["stop_action"] == [
        {"action": "oep_test_fixture.boiler_heater", "data": {"enabled": False}}
    ]
    assert all(
        not any(field in definition for field in ("initial_state", "triggers", "actions"))
        for definition in boundary.definitions.values()
    )


def test_existing_and_deleted_automations_and_hidden_sidebar_are_retained(tmp_path, monkeypatch):
    """A user-edited or disabled object is never overwritten, and warm starts do not recreate deleted examples."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    custom = {"alias": "Keep this automation", "initial_state": False, "actions": []}
    boundary.definitions["oep_lab_dishwasher"] = deepcopy(custom)
    run(boundary, journal, monkeypatch)
    assert boundary.definitions["oep_lab_dishwasher"] == custom
    boundary.definitions.pop("oep_lab_dryer")
    boundary.sidebar = False
    before = deepcopy(boundary.definitions)
    boundary.calls.clear()
    run(boundary, journal, monkeypatch)
    assert boundary.definitions == before and not boundary.sidebar
    assert boundary.calls == []


def test_sidebar_failure_is_not_recorded_as_success(tmp_path, monkeypatch):
    """A successful HTTP status cannot hide a Supervisor option that failed to become active."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")

    async def unchanged(_session, _url, _command):
        """Keep reporting a hidden panel despite accepting the setter's native request."""
        return {"ingress_panel": False}

    monkeypatch.setattr(ha_examples, "native_command", unchanged)
    with pytest.raises(RuntimeError, match="not enabled"):
        asyncio.run(
            ha_examples.provision(
                None, "http://homeassistant", None, journal.path, app_request=None
            )
        )
    assert "sidebar" not in examples.ExampleJournal(journal.path).state["completed"]


@pytest.mark.parametrize("outcome", ["rejected", "unexpected_id"])
def test_sidebar_only_retries_a_known_unapplied_command(tmp_path, monkeypatch, outcome):
    """Explicit pre-dispatch rejection can retry. An unexpected post-send response retains its receipt."""
    journal = examples.ExampleJournal(tmp_path / "examples.json")
    boundary = HomeAssistantBoundary(journal)
    original_request = boundary.request
    failed = False

    async def request(method, path, **kwargs):
        """Fail the first options call with independently specified transport certainty."""
        nonlocal failed
        if not failed and method == "POST" and path.endswith("/options"):
            failed = True
            boundary.calls.append((method, path, deepcopy(kwargs)))
            error = ha_examples.CommandNotApplied if outcome == "rejected" else RuntimeError
            raise error("Native command rejection or unexpected response identity")
        return await original_request(method, path, **kwargs)

    boundary.request = request
    with pytest.raises(RuntimeError):
        run(boundary, journal, monkeypatch)
    receipts = examples.ExampleJournal(journal.path).state["completed"]
    assert ("attempted:sidebar" in receipts) is (outcome == "unexpected_id")
    run(boundary, journal, monkeypatch)
    assert boundary.sidebar is (outcome == "rejected")
    writes = sum(
        method == "POST" and path.endswith("/options") for method, path, _ in boundary.calls
    )
    assert writes == (2 if outcome == "rejected" else 1)
