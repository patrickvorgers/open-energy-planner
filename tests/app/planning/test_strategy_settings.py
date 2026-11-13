"""Saved strategy preferences remain independent of published and acknowledged plans."""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.optimization.domain import Strategy
from open_energy_planner.optimization.strategy_api import register


@pytest.mark.parametrize("issue", [None, "invalid_strategy"])
def test_strategy_preferences_read_save_and_compare_without_an_archive(issue):
    """Read intent, retain configuration issues and reject stale writes without needing a plan repository."""

    async def scenario():
        """Exercise HTTP validation and persistence with only the settings dependencies."""
        saved = []
        store = SimpleNamespace(current=Strategy.COST, issue=issue)

        def save(candidate):
            """Record the persisted preference and clear the configuration issue."""
            saved.append(candidate)
            store.current = candidate
            store.issue = None

        store.save = save

        async def persist(save, candidate, **_kwargs):
            """Apply the same serialized persistence boundary without a planner."""
            save(candidate)

        app = web.Application()
        register(app, store, object(), asyncio.Lock(), persist)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/v1/strategy")
            assert response.status == 200
            assert await response.json() == {"requested": "minimize cost", "issue": issue}
            response = await client.put(
                "/v1/strategy",
                json={"strategy": "minimize consumption", "expected_strategy": "minimize cost"},
            )
            assert response.status == 200
            assert await response.json() == {"requested": "minimize consumption", "issue": None}
            assert saved == [Strategy.CONSUMPTION]
            response = await client.put(
                "/v1/strategy",
                json={"strategy": "minimize cost", "expected_strategy": "minimize cost"},
            )
            assert response.status == 409
            assert saved == [Strategy.CONSUMPTION]

    asyncio.run(scenario())
