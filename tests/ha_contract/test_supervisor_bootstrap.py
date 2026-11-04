"""Distinguish integration admission from Supervisor transport failures over real HTTP/WebSocket exchanges."""

import asyncio
import logging

import pytest
from aiohttp import ClientSession, web

from open_energy_planner import supervisor
from open_energy_planner.pairing import Pairing
from tests.ha_contract.support.connection import server


@pytest.mark.parametrize(
    ("failure", "expected", "recovery"),
    [
        ("unknown_command", "integration is not loaded", False),
        ("link_failed", "integration is loaded", False),
        ("unauthorized", "Home Assistant rejected", False),
        ("transport", "Supervisor connection unavailable", False),
        ("malformed", "Supervisor connection unavailable", False),
        ("auth_invalid", "Supervisor authentication rejected", False),
        ("handshake", "invalid Supervisor response", False),
        ("auth_malformed", "invalid Supervisor response", False),
        (None, "automatic connection request was accepted", False),
        ("unknown_command", "automatic connection request was accepted", True),
        ("auth_invalid", "automatic connection request was accepted", True),
    ],
)
def test_supervisor_diagnostics_identify_the_failed_boundary_and_retry(
    tmp_path, monkeypatch, caplog, failure, expected, recovery
):
    """Unknown commands cannot prove files absent. Recognized failures, transport and accepted requests have distinct messages.

    Recovery cases reject an integration command or authentication once, then
    accept the next actual WebSocket request. No response creates a peer without the separate TLS
    pairing exchange. Provider error text contains a private sentinel that must
    never reach diagnostics.
    """
    caplog.set_level(logging.INFO, logger=supervisor.__name__)

    async def scenario():
        """Exercise real protocol messages, substituting only the Supervisor hostname for a local fixture."""
        token = "private-supervisor-token"
        private_error = "private-provider-error"
        requests = []
        connections = []
        pairing = Pairing(tmp_path, "test-installation")
        real_session = ClientSession

        async def info(request):
            """Require the normal app identity request to retain its authenticated transport."""
            assert request.headers["Authorization"] == "Bearer " + token
            return web.json_response({"data": {"slug": "local_open_energy_planner"}})

        async def websocket(request):
            """Act as the Supervisor proxy and return independently specified HA result codes."""
            if failure == "transport":
                raise web.HTTPUnauthorized()
            connection = web.WebSocketResponse()
            await connection.prepare(request)
            connections.append(connection)
            active_failure = None if recovery and len(connections) > 1 else failure
            if active_failure == "handshake":
                await connection.send_json({"type": "unexpected", "message": private_error})
                await connection.close()
                return connection
            await connection.send_json({"type": "auth_required"})
            assert await connection.receive_json() == {"type": "auth", "access_token": token}
            if active_failure in {"auth_invalid", "auth_malformed"}:
                await connection.send_json(
                    {
                        "type": "auth_invalid"
                        if active_failure == "auth_invalid"
                        else "unexpected",
                        "message": private_error,
                    }
                )
                await connection.close()
                return connection
            await connection.send_json({"type": "auth_ok"})
            command = await connection.receive_json()
            assert command["type"] == "open_energy_planner/supervisor_link"
            assert command["secret"] == pairing.supervisor_secret
            assert command["instance_id"] == "test-installation"
            requests.append(command)
            code = active_failure
            response = {"id": command["id"], "type": "result", "success": code is None}
            response.update(
                {"error": {"code": code, "message": private_error}}
                if code
                else {"result": {"status": "create_entry"}}
            )
            await connection.send_json([] if failure == "malformed" else response)
            await connection.close()
            return connection

        app = web.Application()
        app.router.add_get("/addons/self/info", info)
        app.router.add_get("/core/websocket", websocket)
        async with server(app) as url:

            class LocalSupervisorSession:
                """Keep real HTTP and WebSocket behavior while routing the fixed hostname locally."""

                def __init__(self, **options):
                    """Preserve the production session timeout and own its actual socket resources."""
                    self.session = real_session(**options)

                async def __aenter__(self):
                    """Open the real client before bootstrap starts."""
                    await self.session.__aenter__()
                    return self

                async def __aexit__(self, *errors):
                    """Close actual sockets on normal completion, failure or cancellation."""
                    return await self.session.__aexit__(*errors)

                def get(self, address, **options):
                    """Redirect the identity request without changing headers or response decoding."""
                    return self.session.get(address.replace("http://supervisor", url), **options)

                def ws_connect(self, address, **options):
                    """Redirect only the hostname, retaining the real protocol and heartbeat settings."""
                    return self.session.ws_connect(
                        address.replace("http://supervisor", url), **options
                    )

            monkeypatch.setattr(supervisor, "ClientSession", LocalSupervisorSession)
            if recovery:
                monkeypatch.setattr(supervisor, "PAIRING_RETRY_SECONDS", 0)
            task = asyncio.create_task(
                supervisor.connect(pairing, token, "127.0.0.1", 8099, "a" * 64)
            )
            try:
                async with asyncio.timeout(3):
                    while not any(expected in record.message for record in caplog.records):
                        await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        messages = [
            record.message for record in caplog.records if record.name == supervisor.__name__
        ]
        assert token not in caplog.text and private_error not in caplog.text
        assert pairing.supervisor_secret not in caplog.text
        assert pairing.peer is None
        if failure == "unknown_command":
            assert any("installation cannot be verified" in message for message in messages)
            assert any("restart Home Assistant Core" in message for message in messages)
        if failure == "auth_invalid":
            assert any("Supervisor token and permissions" in message for message in messages)
            assert any("restart the app" in message for message in messages)
        if failure in {"handshake", "auth_malformed"}:
            assert not any("Supervisor token and permissions" in message for message in messages)
            assert not any("authentication rejected" in message for message in messages)
            assert not requests
        if failure not in ("transport", "malformed", "handshake", "auth_malformed"):
            assert not any("Supervisor connection unavailable" in message for message in messages)
        if recovery:
            assert len(connections) == 2
            assert len(requests) == (1 if failure == "auth_invalid" else 2)
        elif failure in {"auth_invalid", "handshake", "auth_malformed"}:
            assert not requests
            assert any("Retrying in 10 seconds" in message for message in messages)
        assert pairing.ui_url == "/app/local_open_energy_planner"

    asyncio.run(scenario())


def test_restarting_app_reannounces_without_replacing_installation_identity(tmp_path, monkeypatch):
    """HA restarting while the app is stopped must not suppress its next discovery as an identical announcement.

    Supervisor 2026.10.5 ignores identical app/service/config publications and
    excludes stopped apps from HA's startup discovery list. A fresh public
    announcement must therefore change only delivery metadata, never the app's
    certificate, installation identity or stored HA peer.
    """
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from uuid import UUID

    from open_energy_planner import discovery

    async def scenario():
        """Run both actual advertisement lifecycles with retained TLS files and captured transport calls."""
        client = MagicMock()
        client.__aenter__.return_value = client
        client.post.return_value.__aenter__.return_value = SimpleNamespace(
            raise_for_status=MagicMock()
        )
        zeroconf = MagicMock()
        for method in ("async_register_service", "async_unregister_service", "async_close"):
            setattr(zeroconf, method, AsyncMock())
        site = MagicMock()
        site.start = AsyncMock()
        monkeypatch.setenv("SUPERVISOR_TOKEN", "private-supervisor-token")
        monkeypatch.setattr(discovery, "ClientSession", lambda **_options: client)
        monkeypatch.setattr(discovery, "AsyncZeroconf", lambda: zeroconf)
        monkeypatch.setattr(discovery.socket, "gethostbyname", lambda _host: "192.0.2.10")
        monkeypatch.setattr(discovery.web, "TCPSite", lambda *_args, **_options: site)
        monkeypatch.setattr(discovery, "connect_supervisor", AsyncMock())
        peer = {"peer_id": "retained-ha-owner", "name": "HA", "token": "private-peer-token"}
        peer_file = tmp_path / "pairing.json"
        peer_file.write_text(json.dumps(peer))
        original_peer = peer_file.read_bytes()
        fingerprints = []
        for _ in range(2):
            pairing = Pairing(tmp_path, "retained-installation")
            pairing.supervisor_secret = "private-bootstrap-proof"
            app = web.Application()
            discovery.advertise(app, pairing, tmp_path, "private-admin-token")
            lifetime = app.cleanup_ctx[0](app)
            try:
                await anext(lifetime)
                fingerprints.append(pairing.fingerprint)
            finally:
                await lifetime.aclose()
            assert pairing.peer == peer and peer_file.read_bytes() == original_peer
        assert client.post.call_count == 2
        first, second = [call.kwargs["json"] for call in client.post.call_args_list]
        assert first != second, (
            "Supervisor would suppress an identical publication after HA missed the first"
        )
        first_id = first["config"]["announcement_id"]
        second_id = second["config"]["announcement_id"]
        assert UUID(first_id).version == UUID(second_id).version == 4
        assert first_id != second_id
        assert {**first["config"], "announcement_id": None} == {
            **second["config"],
            "announcement_id": None,
        }
        assert first["service"] == second["service"] == "open_energy_planner"
        assert first["config"]["instance_id"] == "retained-installation"
        assert fingerprints[0] == fingerprints[1] == first["config"]["fingerprint"]
        assert not any(
            secret in str([first, second])
            for secret in (
                "private-supervisor-token",
                "private-peer-token",
                "private-bootstrap-proof",
                "private-admin-token",
            )
        )

    asyncio.run(scenario())
