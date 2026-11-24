"""Verify safe Home Assistant diagnostics for failed app exchanges."""

import asyncio
import logging

import pytest
from aiohttp import ClientResponseError, ClientSession, web
from aiohttp.test_utils import TestServer

from custom_components.open_energy_planner import client as client_module
from custom_components.open_energy_planner.client import LocalClient
from open_energy_planner.build_info import BUILD_INFO, HEADER_FIELDS


def test_request_logs_failures_and_recovery_without_private_values(caplog, monkeypatch):
    """Report failures, repeats, a changed HTTP status and recovery with safe timing."""
    ticks = iter(range(10))
    monkeypatch.setattr(client_module, "monotonic", lambda: next(ticks))
    caplog.set_level(logging.DEBUG, logger=client_module.__name__)

    async def scenario():
        """Exercise HTTP status failures and recovery through the same app route."""
        statuses = iter([503, 503, 409, 200, 503])

        async def respond(request):
            """Return controlled responses carrying synthetic values that must stay private."""
            assert request.headers["Authorization"] == "Bearer synthetic-private-token"
            assert request.query["key"] == "synthetic-private-query"
            status = next(statuses)
            if status != 200:
                return web.Response(status=status, reason="synthetic-private-response")
            headers = {header: BUILD_INFO[field] or "" for field, header in HEADER_FIELDS.items()}
            return web.json_response({"ok": True}, headers=headers)

        application = web.Application()
        application.router.add_get("/v1/snapshot", respond)
        async with TestServer(application) as server, ClientSession() as session:
            client = LocalClient(session, str(server.make_url("")), "synthetic-private-token")
            route = "/v1/snapshot?key=synthetic-private-query"
            for status in (503, 503, 409):
                with pytest.raises(ClientResponseError) as failure:
                    await client.request("GET", route)
                assert failure.value.status == status
            assert await client.request("GET", route) == {"ok": True}
            with pytest.raises(ClientResponseError):
                await client.request("GET", route)

    asyncio.run(scenario())
    records = [record for record in caplog.records if record.name == client_module.__name__]
    assert [(record.levelno, record.getMessage()) for record in records] == [
        (
            logging.WARNING,
            "App request GET /v1/snapshot failed after 1.000 s (ClientResponseError (HTTP 503)).",
        ),
        (
            logging.DEBUG,
            "App request GET /v1/snapshot failed after 1.000 s (ClientResponseError (HTTP 503)).",
        ),
        (
            logging.WARNING,
            "App request GET /v1/snapshot failed after 1.000 s (ClientResponseError (HTTP 409)).",
        ),
        (logging.INFO, "App request GET /v1/snapshot recovered in 1.000 s."),
        (
            logging.WARNING,
            "App request GET /v1/snapshot failed after 1.000 s (ClientResponseError (HTTP 503)).",
        ),
    ]
    assert "synthetic-private" not in caplog.text


def test_measurement_timeout_logs_the_actual_route(caplog, monkeypatch):
    """Identify a stalled measurement upload as a timeout on its own route."""
    monkeypatch.setattr(client_module, "HTTP_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        """Hold the app response until the integration's HTTP deadline expires."""
        release = asyncio.Event()

        async def respond(_request):
            """Keep the connection open without sending response headers."""
            await release.wait()
            return web.json_response({})

        application = web.Application()
        application.router.add_post("/v1/observations", respond)
        async with TestServer(application) as server, ClientSession() as session:
            client = LocalClient(session, str(server.make_url("")), "synthetic-private-token")
            try:
                with pytest.raises(TimeoutError):
                    await client.observations({"value": "synthetic-private-measurement"})
            finally:
                release.set()

    asyncio.run(scenario())
    assert "App request POST /v1/observations failed after " in caplog.text
    assert "(TimeoutError)" in caplog.text
    assert "synthetic-private" not in caplog.text
