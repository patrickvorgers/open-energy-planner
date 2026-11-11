"""Validate control acknowledgements independently of full installation snapshots."""

import asyncio

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from custom_components.open_energy_planner.client import LocalClient, ReleaseMismatchError
from open_energy_planner.build_info import BUILD_INFO, HEADER_FIELDS


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"accepted": False, "enabled": False, "request_id": "pause-current"},
        {"accepted": 1, "enabled": False, "request_id": "pause-current"},
        {"accepted": "true", "enabled": False, "request_id": "pause-current"},
        {"accepted": True, "enabled": 0, "request_id": "pause-current"},
        {"accepted": True, "enabled": True, "request_id": "pause-current"},
        {"accepted": True, "enabled": False, "request_id": "old-or-foreign"},
        {"accepted": True, "enabled": False, "request_id": None},
        {"accepted": True, "enabled": False, "request_id": "pause-current", "plan": {}},
    ],
)
def test_client_rejects_unconfirmed_or_foreign_control_receipt(receipt):
    """HTTP success alone cannot confirm a different, false, malformed or snapshot-shaped acknowledgement."""

    async def scenario():
        """Return real HTTP JSON with valid release headers so only receipt validation is under test."""

        async def respond(request):
            """Observe the original boolean/id command before returning independently specified invalid evidence."""
            assert await request.json() == {"enabled": False, "request_id": "pause-current"}
            return web.json_response(
                receipt,
                headers={
                    header: BUILD_INFO[field] or "" for field, header in HEADER_FIELDS.items()
                },
            )

        application = web.Application()
        application.router.add_put("/v1/enabled", respond)
        async with TestServer(application) as server, ClientSession() as session:
            client = LocalClient(
                session, str(server.make_url("")), "synthetic-control-token-0123456789"
            )
            with pytest.raises(ValueError, match="Invalid enable acknowledgement"):
                await client.enabled(False, "pause-current")

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_client_accepts_only_matching_control_intent(enabled):
    """A correctly typed receipt confirms exactly its command without requiring a financial snapshot."""

    async def scenario():
        """Use the native client over actual HTTP for both pause and resume."""

        async def respond(request):
            """Echo only the application control receipt contract, never plan or forecast data."""
            assert await request.json() == {"enabled": enabled, "request_id": "current-control"}
            return web.json_response(
                {"accepted": True, "enabled": enabled, "request_id": "current-control"},
                headers={
                    header: BUILD_INFO[field] or "" for field, header in HEADER_FIELDS.items()
                },
            )

        application = web.Application()
        application.router.add_put("/v1/enabled", respond)
        async with TestServer(application) as server, ClientSession() as session:
            client = LocalClient(
                session, str(server.make_url("")), "synthetic-control-token-0123456789"
            )
            assert await client.enabled(enabled, "current-control") == {
                "accepted": True,
                "enabled": enabled,
                "request_id": "current-control",
            }

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["version", "build"])
def test_control_receipt_retains_release_and_build_identity_checks(field):
    """A matching intent/id cannot bypass the existing paired-build response identity boundary."""

    async def scenario():
        """Corrupt only one producer identity header, leaving the receipt itself correct."""

        async def respond(_request):
            """Return a valid receipt from a deliberately incompatible producer identity."""
            headers = {
                header: ("incompatible" if name == field else BUILD_INFO[name] or "")
                for name, header in HEADER_FIELDS.items()
            }
            return web.json_response(
                {"accepted": True, "enabled": False, "request_id": "pause-current"}, headers=headers
            )

        application = web.Application()
        application.router.add_put("/v1/enabled", respond)
        async with TestServer(application) as server, ClientSession() as session:
            client = LocalClient(
                session, str(server.make_url("")), "synthetic-control-token-0123456789"
            )
            with pytest.raises(ReleaseMismatchError):
                await client.enabled(False, "pause-current")

    asyncio.run(scenario())
