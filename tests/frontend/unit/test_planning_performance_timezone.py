"""Use an explicit demo timezone without inventing a Home Assistant timezone."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.optimization.performance import CalculationHistory
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from open_energy_planner.synthetic import capture


@pytest.mark.parametrize("source", ["synthetic", "home_assistant"])
def test_history_without_discovery_uses_utc_only_for_synthetic_mode(tmp_path, source):
    """A retained demo calculation is readable in UTC, but HA still requires its own timezone."""
    started = datetime(2026, 1, 3, 23, 59, tzinfo=UTC)
    repository = SqlPlanRepository(tmp_path)
    history = CalculationHistory(SqlDocuments(repository.engine, "planning"))
    history.record(capture(started), None, started, started + timedelta(seconds=2), "rejected")
    repository.close()
    token = "synthetic-timezone-test-token-0123456789"

    async def scenario():
        """Read the real authenticated history route without a discovery catalogue."""
        async with TestClient(TestServer(create_app(tmp_path, token, source=source))) as client:
            response = await client.get(
                "/v1/planning-performance?day=2026-01-03",
                headers={"Authorization": f"Bearer {token}"},
            )
            if source == "home_assistant":
                assert response.status == 409
                assert await response.text() == "Home Assistant timezone is not available yet"
            else:
                assert response.status == 200
                body = await response.json()
                assert body["timezone"] == "UTC"
                assert body["day"] == "2026-01-03"
                assert len(body["calculations"]) == 1
                assert body["calculations"][0]["started_at"] == started.isoformat()

    asyncio.run(scenario())
