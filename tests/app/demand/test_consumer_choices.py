"""Verify authenticated consumer choices, version conflicts, Energy-owned meters and persistence after reopening."""

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.appliances.forecasting import ConsumerForecasts
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


def test_settings_view_omits_forecasts_but_preserves_selection_and_full_read(tmp_path, monkeypatch):
    """Appliance editors need choices and ownership, not large original model forecasts."""
    instances = []
    original_init = ConsumerForecasts.__init__

    def remember(service, *arguments, **keywords):
        """Retain the actual runtime so both HTTP views read the same forecast result."""
        original_init(service, *arguments, **keywords)
        instances.append(service)

    monkeypatch.setattr(ConsumerForecasts, "__init__", remember)

    async def scenario():
        """Read the real authenticated API without substituting its projection or serializer."""
        token = "consumer-settings-test-0123456789abcdef"
        headers = {"Authorization": "Bearer " + token}
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            result = {"status": "active", "synthetic_forecast": "x" * 200000}
            instances[0].result = result
            full = await (await client.get("/v1/consumers", headers=headers)).json()
            settings = await (
                await client.get("/v1/consumers?view=settings", headers=headers)
            ).json()
            assert full["result"] == result
            assert settings == {key: value for key, value in full.items() if key != "result"}
            assert len(json.dumps(settings)) < 1000
            assert instances[0].result == result
            assert (await client.get("/v1/consumers?view=invented", headers=headers)).status == 400

    asyncio.run(scenario())
