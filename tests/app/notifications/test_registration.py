"""Verify a future module can join the real authenticated endpoint without app-factory changes."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.notifications.api import NOTIFICATIONS
from open_energy_planner.notifications.model import NoticeText, Notification

TOKEN = "notification-test-only-0123456789abcdef"


def test_module_registration_reuses_authentication_and_preserves_unknown_source_evidence(tmp_path):
    """An arbitrary startup provider is private, readable and recoverable through the actual HTTP route."""

    async def scenario():
        """Change diagnostic input only, keeping product configuration and all mutation endpoints untouched."""
        state = [
            (
                Notification(
                    "future:device-one",
                    "input_missing",
                    NoticeText("Future problem", "Check the input"),
                ),
            )
        ]
        reads = []

        def current():
            """Read the imaginary module's current evidence without changing application data."""
            reads.append(True)
            return state[0]

        app = create_app(tmp_path, TOKEN, source="home_assistant")
        app[NOTIFICATIONS].register("future", current)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/v1/notifications")).status == 401
            assert reads == []
            headers = {"Authorization": "Bearer " + TOKEN}
            response = await client.get("/v1/notifications", headers=headers)
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            document = await response.json()
            notice = next(item for item in document["notifications"] if item["owner"] == "future")
            assert notice["key"] == "future:device-one"
            assert notice["stale"] is False
            assert TOKEN not in str(document)
            state[0] = None
            uncertain = await (await client.get("/v1/notifications", headers=headers)).json()
            assert "future" in uncertain["unavailable_sources"]
            assert (
                next(item for item in uncertain["notifications"] if item["owner"] == "future")[
                    "stale"
                ]
                is True
            )
            state[0] = ()
            recovered = await (await client.get("/v1/notifications", headers=headers)).json()
            assert all(item["owner"] != "future" for item in recovered["notifications"])
            assert "future" not in recovered["unavailable_sources"]

    asyncio.run(scenario())
