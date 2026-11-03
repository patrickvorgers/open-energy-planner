"""Verify the authenticated read-only forecast model-status API without manual activation or stored side effects."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from tests.database.support.archive import list_documents


def test_model_status_is_authenticated_read_only_and_does_not_write_selection(tmp_path):
    """Reading cold-start model status cannot create evidence or provide a manual activation route."""

    async def scenario():
        """Use real middleware and routes to verify the absent-config state and read-only contract."""
        async with TestClient(
            TestServer(create_app(tmp_path, "setup-test-token-0123456789", source="home_assistant"))
        ) as client:
            headers = {"Authorization": "Bearer setup-test-token-0123456789"}
            response = await client.get("/v1/models", headers=headers)
            assert response.status == 200
            document = await response.json()
            assert document["models"] == [] and document["issue"] is None
            assert "not measured EMS savings" in document["explanation"]
            assert (
                await client.put("/v1/models", headers=headers, json={"preferred": "learned"})
            ).status == 405
            assert list_documents(tmp_path, "planning") == []

    asyncio.run(scenario())
