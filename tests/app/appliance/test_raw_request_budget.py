"""The production app admits bounded raw imports without relaxing ordinary request limits."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.appliances.cycles.model import instant, restore_profile
from open_energy_planner.runtime.maintenance import create_managed_app
from tests.app.appliance.raw_support import dense_raw_trace
from tests.app.appliance.support import ENTITY, POWER
from tests.ha_contract.support.metadata import setup_metadata


def test_raw_import_uses_its_own_limit_through_production_gateway(tmp_path):
    """A valid large frame and its exact peak survive the gateway, while oversized and ordinary reads stay bounded."""

    async def scenario():
        """Use actual managed-app composition, authentication, catalogue selection and SQL-backed learning."""
        token = "raw-budget-fixture-only-0123456789"
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        application = create_managed_app(
            tmp_path, token, factory=create_app, source="home_assistant"
        )
        async with TestClient(TestServer(application)) as client:
            catalogue = setup_metadata()
            catalogue["entities"].extend(
                {
                    "entity_id": entity,
                    "name": entity,
                    "unit": unit,
                    "device_id": "",
                    "device_class": kind,
                    "state_class": state_class,
                }
                for entity, unit, kind, state_class in (
                    (ENTITY, "kWh", "energy", "total_increasing"),
                    (POWER, "W", "power", "measurement"),
                )
            )
            catalogue["suggestions"]["consumers"] = [
                {"entity_id": ENTITY, "upstream": None, "power_entity": POWER}
            ]
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=catalogue)
            ).status == 200
            current = await (await client.get("/v1/consumers", headers=headers)).json()
            assert (
                await client.put(
                    "/v1/consumers",
                    headers=headers,
                    json={
                        "expected_version": current["version"],
                        "selected": [{"entity_id": ENTITY, "weather_sensitive": False}],
                    },
                )
            ).status == 200
            body = dense_raw_trace()
            offset = datetime.now(UTC) - instant(body["end"]) - timedelta(seconds=1)
            for field in ("start", "end", "energy_reported_at", "power_reported_at"):
                body[field] = (instant(body[field]) + offset).isoformat()
            for field in ("power_states", "energy_states"):
                for row in body[field]:
                    row["at"] = (instant(row["at"]) + offset).isoformat()
            wire = json.dumps(body).encode()
            assert 256 * 1024 < len(wire) < 512 * 1024
            response = await client.post("/v1/appliances/raw_history", headers=headers, data=wire)
            assert response.status == 200
            state = (await response.json())["appliances"]
            learned = restore_profile(state["devices"][0]["profile"])
            assert learned.power_w == (1000, 3000, 1000)
            assert learned.phase_boundaries == pytest.approx((0, 600.3, 620.3, 1800))
            assert learned.energy_kwh == pytest.approx(23 / 45)
            near_limit = wire + b" " * (512 * 1024 - len(wire))
            assert (
                await client.post("/v1/appliances/raw_history", headers=headers, data=near_limit)
            ).status == 200
            assert (
                await client.post(
                    "/v1/appliances/raw_history", headers=headers, data=near_limit + b" "
                )
            ).status == 413
            assert (await client.post("/v1/appliances/raw_history", data=wire)).status == 401
            for operation in ("history", "device"):
                assert (
                    await client.post("/v1/appliances/" + operation, headers=headers, data=wire)
                ).status == 413
                assert (
                    await client.post(
                        "/v1/appliances/" + operation,
                        headers=headers,
                        data=b"{}" + b" " * (256 * 1024 - 3),
                    )
                ).status == 400
            retained = await (await client.get("/v1/appliances", headers=headers)).json()
            assert retained["revision"] == state["revision"]
            assert retained["devices"][0]["profile"]["samples"] == 1
            assert restore_profile(retained["devices"][0]["profile"]) == learned

    asyncio.run(scenario())
