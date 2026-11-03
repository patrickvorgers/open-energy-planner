"""Verify authenticated consumer choices, version conflicts, Energy-owned meters and persistence after reopening."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from tests.ha_contract.support.metadata import setup_metadata


def test_consumer_choices_are_authenticated_versioned_and_energy_owned(tmp_path):
    """Real API rejects arbitrary/overlapping meters and stale writes, and preserves weather choices after reopen."""
    from copy import deepcopy

    async def scenario():
        """Use normal discovery and middleware so browser-only filtering cannot bypass ownership."""
        token = "consumer-api-test-0123456789abcdef"
        headers = {"Authorization": "Bearer " + token}
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            assert (await client.get("/consumers")).status == 200
            assert (await client.get("/v1/consumers")).status == 401
            body = deepcopy(setup_metadata())
            body["suggestions"]["consumers"] = [{"entity_id": "sensor.heat", "upstream": None}]
            assert (await client.post("/v1/ha/catalogue", headers=headers, json=body)).status == 200
            state = await (await client.get("/v1/consumers", headers=headers)).json()
            assert state["options"][0]["entity_id"] == "sensor.heat"
            payload = {
                "selected": [{"entity_id": "sensor.heat", "weather_sensitive": True}],
                "expected_version": state["version"],
            }
            assert (await client.put("/v1/consumers", headers=headers, json=payload)).status == 200
            assert (await client.put("/v1/consumers", headers=headers, json=payload)).status == 409
            state = await (await client.get("/v1/consumers", headers=headers)).json()
            payload["expected_version"] = state["version"]
            payload["selected"][0]["entity_id"] = "sensor.other"
            assert (await client.put("/v1/consumers", headers=headers, json=payload)).status == 400
            assert not state["active_in_planning"]
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            state = await (await client.get("/v1/consumers", headers=headers)).json()
            assert state["selected"] == [{"entity_id": "sensor.heat", "weather_sensitive": True}]
            assert state["result"] is None

    asyncio.run(scenario())
