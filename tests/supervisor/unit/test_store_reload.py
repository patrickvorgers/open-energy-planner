"""Keep the longer store request confined to credential-safe disposable acceptance."""

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest

from script.supervisor import provision_supervisor


@pytest.fixture
def store_boundary(monkeypatch):
    """Model private Docker metadata and HTTP transport without accessing an engine or credentials."""
    calls = []
    metadata = [
        {"Name": "/hassio_cli", "Config": {"Env": ["SUPERVISOR_TOKEN=fixture-private-token"]}},
        {
            "Name": "/hassio_supervisor",
            "NetworkSettings": {"Networks": {"hassio": {"IPAddress": "172.30.32.2"}}},
        },
    ]
    response = io.BytesIO(b'{"result":"ok","data":{"accepted":true}}')
    response.status = 200
    boundary = SimpleNamespace(
        calls=calls, metadata=metadata, response=response, failure=None, handlers=None
    )

    def inspect(*arguments):
        """Record that metadata is queried only after isolation has passed."""
        calls.append(arguments)
        return json.dumps(metadata)

    def open_request(request, *, timeout):
        """Capture the exact authenticated request without making an HTTP call."""
        calls.append((request, timeout))
        if boundary.failure is not None:
            raise boundary.failure
        return boundary.response

    def opener(*handlers):
        """Retain the explicitly configured proxy and redirect policies."""
        boundary.handlers = handlers
        return SimpleNamespace(open=open_request)

    monkeypatch.setenv("OEP_SUPERVISOR_SMOKE", "1")
    monkeypatch.setattr(
        provision_supervisor, "require_private_engine", lambda: calls.append("isolated")
    )
    monkeypatch.setattr(provision_supervisor, "run", inspect)
    monkeypatch.setattr(provision_supervisor, "build_opener", opener)
    return boundary


def test_acceptance_store_request_is_bounded_and_local(store_boundary):
    """Only the verified inner endpoint receives one POST with a 180-second timeout."""
    assert provision_supervisor.ha("store", "reload") == {"accepted": True}
    assert store_boundary.calls[0] == "isolated"
    assert store_boundary.calls[1][-3:] == ("inspect", "hassio_cli", "hassio_supervisor")
    request, timeout = store_boundary.calls[2]
    assert len(store_boundary.calls) == 3
    assert request.full_url == "http://172.30.32.2/store/reload"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer fixture-private-token"
    assert timeout == 180
    proxy, redirects = store_boundary.handlers
    assert isinstance(proxy, ProxyHandler) and proxy.proxies == {}
    assert isinstance(redirects, provision_supervisor.NoSupervisorRedirects)
    assert redirects.redirect_request(request, None, 302, "Found", {}, "http://elsewhere/") is None


def test_direct_store_reload_rejects_ordinary_environment(store_boundary, monkeypatch):
    """The direct helper cannot inspect credentials outside disposable acceptance."""
    monkeypatch.delenv("OEP_SUPERVISOR_SMOKE")
    with pytest.raises(provision_supervisor.IsolationError):
        provision_supervisor.reload_acceptance_store()
    assert store_boundary.calls == []


def test_store_reload_requires_engine_ownership_before_inspect(store_boundary, monkeypatch):
    """A foreign Docker engine is rejected before metadata or HTTP transport is accessed."""

    def reject_engine():
        """Represent an isolation failure without querying a real engine."""
        raise provision_supervisor.IsolationError("Foreign engine")

    monkeypatch.setattr(provision_supervisor, "require_private_engine", reject_engine)
    with pytest.raises(provision_supervisor.IsolationError):
        provision_supervisor.ha("store", "reload")
    assert store_boundary.calls == []


@pytest.mark.parametrize("invalid", ["address", "name", "missing", "duplicate", "header"])
def test_store_reload_rejects_untrusted_metadata(store_boundary, invalid):
    """Unexpected identities and malformed credentials never reach HTTP or error text."""
    cli, supervisor = store_boundary.metadata
    if invalid == "address":
        supervisor["NetworkSettings"]["Networks"]["hassio"]["IPAddress"] = "192.168.0.100"
    elif invalid == "name":
        cli["Name"] = "/another_cli"
    elif invalid == "missing":
        cli["Config"]["Env"] = []
    elif invalid == "duplicate":
        cli["Config"]["Env"] *= 2
    else:
        cli["Config"]["Env"] = ["SUPERVISOR_TOKEN=private\r\nInjected: header"]
    with pytest.raises(RuntimeError, match="Cannot verify") as caught:
        provision_supervisor.ha("store", "reload")
    assert "private" not in str(caught.value).replace("private Supervisor", "")
    assert len(store_boundary.calls) == 2


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"result":"error"}', b"x" * 65537])
def test_store_reload_requires_bounded_success_protocol(store_boundary, body):
    """Malformed, unsuccessful and oversized bodies fail without retry or exposing payloads."""
    store_boundary.response = io.BytesIO(body)
    store_boundary.response.status = 200
    with pytest.raises(RuntimeError, match="failed or timed out"):
        provision_supervisor.ha("store", "reload")
    assert len(store_boundary.calls) == 3


@pytest.mark.parametrize("failure", ["timeout", "redirect", "http"])
def test_store_reload_transport_errors_never_expose_credentials(store_boundary, failure):
    """Transport failures stay fatal and suppress response and exception credential text."""
    if failure == "timeout":
        store_boundary.failure = TimeoutError("fixture-private-token")
    else:
        store_boundary.failure = HTTPError(
            "http://172.30.32.2/store/reload",
            302 if failure == "redirect" else 500,
            "fixture-private-token",
            {},
            io.BytesIO(b"fixture-private-token"),
        )
    with pytest.raises(RuntimeError) as caught:
        provision_supervisor.ha("store", "reload")
    assert "fixture-private-token" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert len(store_boundary.calls) == 3


def test_ordinary_store_reload_retains_cli(store_boundary, monkeypatch):
    """Persistent environments use the normal CLI rather than the acceptance credential path."""
    monkeypatch.delenv("OEP_SUPERVISOR_SMOKE")
    monkeypatch.setattr(provision_supervisor, "run", lambda *args: '{"result":"ok","data":42}')
    assert provision_supervisor.ha("store", "reload") == 42
    assert store_boundary.calls == []
