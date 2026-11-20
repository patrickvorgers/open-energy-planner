"""Keep an HA connection bound to its configured installation across address reuse."""

import asyncio

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from custom_components.open_energy_planner.client import InstallationMismatchError, LocalClient
from open_energy_planner.build_info import BUILD_INFO, HEADER_FIELDS


@pytest.mark.parametrize("replacement", [None, "22222222-2222-4222-8222-222222222222"])
def test_changed_identity_blocks_exchanges_until_original_installation_returns(replacement):
    """Missing or foreign identity blocks writes without silently retargeting the saved connection."""

    async def scenario():
        """Exercise real HTTP and prove rejected writes never reach the server."""
        original = "11111111-1111-4111-8111-111111111111"
        snapshot = {"instance_id": original}
        requests = []

        async def respond(request):
            """Retain only request paths and return a separately mutable installation identity."""
            requests.append(request.path)
            return web.json_response(
                snapshot if request.path == "/v1/snapshot" else {"accepted": True},
                headers={
                    header: BUILD_INFO[field] or "" for field, header in HEADER_FIELDS.items()
                },
            )

        application = web.Application()
        application.router.add_get("/v1/snapshot", respond)
        application.router.add_post("/v1/ha/source-choices", respond)
        async with TestServer(application) as server, ClientSession() as session:
            client = LocalClient(
                session, str(server.make_url("")), "synthetic-token", expected_instance_id=original
            )
            unconfirmed = LocalClient(
                session,
                str(server.make_url("")),
                "synthetic-token",
                expected_instance_id="foreign-installation",
            )
            with pytest.raises(InstallationMismatchError):
                await unconfirmed.request("POST", "/v1/ha/source-choices", {"failed": True})
            assert requests == ["/v1/snapshot"]
            requests.clear()
            assert (await client.snapshot())["instance_id"] == original
            snapshot.clear()
            if replacement is not None:
                snapshot["instance_id"] = replacement
            with pytest.raises(InstallationMismatchError):
                await client.snapshot()
            with pytest.raises(InstallationMismatchError):
                await client.request("POST", "/v1/ha/source-choices", {"failed": True})
            assert requests == ["/v1/snapshot", "/v1/snapshot"]
            snapshot["instance_id"] = original
            await client.snapshot()
            assert (await client.request("POST", "/v1/ha/source-choices", {"failed": True}))[
                "accepted"
            ]
            assert requests[-2:] == ["/v1/snapshot", "/v1/ha/source-choices"]

    asyncio.run(scenario())


@pytest.mark.parametrize("late_exchange", ["initial_snapshot", "identified_reply", "receipt"])
def test_reply_started_before_a_mismatch_cannot_confirm_identity_or_success(late_exchange):
    """Late matching snapshots or receipts cannot undo a newer installation mismatch."""

    async def scenario():
        """Use events to hold a real HTTP response until a competing request records the mismatch."""
        original = "11111111-1111-4111-8111-111111111111"
        foreign = "22222222-2222-4222-8222-222222222222"
        started = asyncio.Event()
        release = asyncio.Event()
        snapshots = 0
        writes = 0

        async def respond(request):
            """Return independently ordered identities and count only source publications."""
            nonlocal snapshots, writes
            if request.path == "/v1/snapshot":
                snapshots += 1
                order = snapshots
                if late_exchange == "initial_snapshot" and order == 1:
                    started.set()
                    await release.wait()
                document = {"instance_id": foreign if order == 2 else original}
            elif request.path == "/v1/history":
                started.set()
                await release.wait()
                document = (
                    {"instance_id": original}
                    if late_exchange == "identified_reply"
                    else {"accepted": True}
                )
            else:
                writes += 1
                document = {"accepted": True}
            return web.json_response(
                document,
                headers={
                    header: BUILD_INFO[field] or "" for field, header in HEADER_FIELDS.items()
                },
            )

        application = web.Application()
        application.router.add_get("/v1/snapshot", respond)
        application.router.add_post("/v1/history", respond)
        application.router.add_post("/v1/ha/source-choices", respond)
        async with TestServer(application) as server, ClientSession() as session:
            client = LocalClient(
                session, str(server.make_url("")), "synthetic-token", expected_instance_id=original
            )
            if late_exchange != "initial_snapshot":
                await client.snapshot()
            late = asyncio.create_task(
                client.request(
                    "POST",
                    "/v1/ha/source-choices"
                    if late_exchange == "initial_snapshot"
                    else "/v1/history",
                    {},
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 2)
                with pytest.raises(InstallationMismatchError):
                    await client.snapshot()
            finally:
                release.set()
            with pytest.raises(InstallationMismatchError):
                await late
            with pytest.raises(InstallationMismatchError):
                await client.request("POST", "/v1/ha/source-choices", {})
            assert writes == 0
            await client.snapshot()
            await client.request("POST", "/v1/ha/source-choices", {})
            assert writes == 1

    asyncio.run(scenario())
