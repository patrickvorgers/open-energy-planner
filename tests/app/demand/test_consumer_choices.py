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


def test_unknown_put_view_preserves_configuration_and_forecast_state(tmp_path, monkeypatch):
    """An invalid response view must be rejected before a valid selection changes durable or live state."""
    from copy import deepcopy

    from open_energy_planner.demand.runtime import DemandRuntime

    instances = []
    original_init = DemandRuntime.__init__

    def remember(service, *arguments, **keywords):
        """Observe the actual forecast owner without replacing its save or validation behavior."""
        original_init(service, *arguments, **keywords)
        instances.append(service)

    monkeypatch.setattr(DemandRuntime, "__init__", remember)

    async def scenario():
        """A rejected PUT leaves the same version usable for a following successful write."""
        token = "invalid-view-test-0123456789abcdef"
        headers = {"Authorization": "Bearer " + token}
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            metadata = deepcopy(setup_metadata())
            metadata["suggestions"]["consumers"] = [{"entity_id": "sensor.heat", "upstream": None}]
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            ).status == 200
            initial = await (await client.get("/v1/consumers", headers=headers)).json()
            runtime = instances[0]
            original_candidate, original_key = object(), object()
            runtime.candidate = original_candidate
            runtime.last_key = original_key
            runtime.candidate_features = "preserved-test-family"
            runtime.learned_preferred = True
            payload = {
                "selected": [{"entity_id": "sensor.heat", "weather_sensitive": True}],
                "expected_version": initial["version"],
            }
            response = await client.put(
                "/v1/consumers?view=invented", headers=headers, json=payload
            )
            assert response.status == 400
            assert await response.text() == "Unknown consumer view"
            assert runtime.candidate is original_candidate
            assert runtime.last_key is original_key
            assert runtime.candidate_features == "preserved-test-family"
            assert runtime.learned_preferred is True
            assert await (await client.get("/v1/consumers", headers=headers)).json() == initial
            assert (
                await client.put("/v1/consumers?view=settings", headers=headers, json=payload)
            ).status == 200
            saved = await (await client.get("/v1/consumers", headers=headers)).json()
            assert saved["selected"] == payload["selected"]
            payload["expected_version"] = saved["version"]
            payload["selected"] = []
            assert (await client.put("/v1/consumers", headers=headers, json=payload)).status == 200
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            assert (await (await client.get("/v1/consumers", headers=headers)).json())[
                "selected"
            ] == []

    asyncio.run(scenario())
