"""Keep source-choice payload reads outside shared app mutation coordination."""

import asyncio
from urllib.parse import urlsplit

from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from tests.ha_contract.support.connection import server


def test_incomplete_chooser_post_does_not_hold_the_shared_app_lock(tmp_path):
    """A stalled authenticated body cannot block snapshots or other source operations."""

    async def scenario():
        """Hold a real socket halfway through its body while independently reading app state."""
        token = "synthetic-pending-body-admin-0123456789"
        entered = asyncio.Event()
        application = create_app(tmp_path, token, source="home_assistant")

        @web.middleware
        async def observe(request, handler):
            """Observe the owned request before it waits for remaining payload bytes."""
            if request.path == "/v1/ha/source-choices/ev" and request.method == "POST":
                entered.set()
            return await handler(request)

        application.middlewares.append(observe)
        async with (
            server(application) as url,
            ClientSession(headers={"Authorization": "Bearer " + token}) as client,
        ):
            address = urlsplit(url)
            reader, writer = await asyncio.open_connection(address.hostname, address.port)
            try:
                writer.write(
                    (
                        "POST /v1/ha/source-choices/ev HTTP/1.1\r\nHost: localhost\r\n"
                        f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
                        "Content-Length: 2\r\n\r\n{"
                    ).encode()
                )
                await writer.drain()
                await asyncio.wait_for(entered.wait(), 2)
                async with asyncio.timeout(2):
                    async with client.get(url + "/v1/snapshot") as response:
                        assert response.status == 200
                    async with client.get(url + "/v1/ha/catalogue/refresh") as response:
                        assert response.status == 200
                writer.write(b"}")
                await writer.drain()
                reply = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                assert reply.startswith(b"HTTP/1.1 200")
                async with client.post(
                    url + "/v1/ha/source-choices/ev", json={"extra": True}
                ) as response:
                    assert response.status == 400
                async with client.post(url + "/v1/ha/source-choices/unknown", json={}) as response:
                    assert response.status == 400
                async with client.post(
                    url + "/v1/ha/source-choices/ev",
                    json={},
                    headers={"Authorization": "Bearer invalid-synthetic-token"},
                ) as response:
                    assert response.status == 401
                async with client.post(
                    url + "/v1/ha/source-choices/ev", data=b" " * (1024 * 1024 + 1)
                ) as response:
                    assert response.status == 413
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())
