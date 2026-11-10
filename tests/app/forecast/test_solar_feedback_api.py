"""Preserve partial plan accuracy and separate whole-day admission through the actual authenticated API."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from tests.app.forecast.support.solar_feedback import seed
from tests.ha_contract.support.metadata import setup_metadata


def test_plan_feedback_partial_hours_and_complete_day_rule_over_http(tmp_path):
    """The real API keeps 17 paired plan hours and .07/.035 MAE while the same incomplete comparable day remains unscored."""
    seed(tmp_path)

    async def scenario():
        """Read actual retained forecasts and measurements without patching comparison calculations."""
        token = "solar-feedback-api-test-0123456789"
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            path = "/v1/forecast-quality/synthetic-feedback-plan"
            assert (await client.get(path)).status == 401
            headers = {"Authorization": "Bearer " + token}
            metadata = setup_metadata()
            metadata["timezone"] = "UTC"
            assert (
                await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            ).status == 200
            response = await client.get(path, headers=headers)
            assert response.status == 200
            source = (await response.json())["series"][0]
            for row, expected in [(source, 0.07), (source["alternatives"][0], 0.035)]:
                assert row["coverage"] == {"hours": 24, "measured_hours": 24, "paired_hours": 17}
                assert row["metrics"]["mae_kwh"] == pytest.approx(expected)
                assert [
                    index for index, hour in enumerate(row["rows"]) if hour["error_kwh"] is None
                ] == [8, 9, 10, 15, 16, 22, 23]
            response = await client.get("/v1/forecast-quality/days/2026-06-15", headers=headers)
            assert response.status == 200
            daily = (await response.json())["series"][0]
            assert daily["daily"]["error_kwh"] is None
            assert all(value is None for value in daily["metrics"].values())
            assert daily["daily"]["evaluation_issue"]

    asyncio.run(scenario())
