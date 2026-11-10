"""Preserve partial plan accuracy and separate whole-day admission through the actual authenticated API."""

import asyncio
from contextlib import closing

import pytest
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from tests.app.forecast.support.solar_feedback import seed
from tests.ha_contract.support.metadata import setup_metadata


@pytest.mark.parametrize(
    "condition,paired,archive_reads",
    [
        ("valid", 17, 0),
        ("none", 0, 0),
        ("wrong_sensor", 0, 0),
        ("future", 0, 0),
        ("retarget_meter", 0, 0),
        ("retarget_source", 0, 0),
        ("retarget_feedback", 17, 1),
        ("historical", 17, 1),
    ],
)
def test_quality_feedback_snapshot_binding_and_fail_closed_admission(
    tmp_path, monkeypatch, condition, paired, archive_reads
):
    """Active unavailable or mismatched evidence never bypasses its guard through an archive fallback."""
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from open_energy_planner.forecast_quality.evidence import evidence
    from open_energy_planner.forecast_quality.history import HistoryArchive
    from open_energy_planner.solar.curtailment import load_feedback
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    seed(tmp_path)
    with closing(SqlPlanRepository(tmp_path)) as repository:
        demand = SqlDocuments(repository.engine, "demand")
        solar = SqlDocuments(repository.engine, "solar")
        now = datetime(2026, 6, 17, tzinfo=UTC)
        sensor = "binary_sensor.synthetic_curtailment"
        history = load_feedback(solar, sensor, now)
        key = ("roof", "sensor.synthetic_pv", sensor)
        if condition == "none":
            history = None
        elif condition == "wrong_sensor":
            history = replace(history, entity_id="binary_sensor.foreign")
        elif condition == "future":
            history = replace(history, checked_until=now + timedelta(hours=1))
        elif condition == "retarget_meter":
            key = ("roof", "sensor.changed", sensor)
        elif condition == "retarget_source":
            key = ("changed", "sensor.synthetic_pv", sensor)
        elif condition == "retarget_feedback":
            key = ("roof", "sensor.synthetic_pv", "binary_sensor.changed")
        snapshots = {} if condition == "historical" else {key: history}
        reads = []
        original = solar.read_many

        def counted_segments(names):
            names = tuple(names)
            reads.extend(name for name in names if name.startswith("curtailment-segment-"))
            return original(names)

        monkeypatch.setattr(solar, "read_many", counted_segments)
        result = evidence(
            repository.get_plan("synthetic-feedback-plan"),
            HistoryArchive(demand),
            demand,
            solar,
            now,
            solar_feedback=snapshots,
        )
        source = result["series"][0]
        assert source["coverage"]["paired_hours"] == paired
        assert len(reads) == archive_reads
        if paired:
            assert source["metrics"]["mae_kwh"] == pytest.approx(0.07)
            assert source["alternatives"][0]["metrics"]["mae_kwh"] == pytest.approx(0.035)
        else:
            assert source["metrics"]["mae_kwh"] is None
            assert source["alternatives"][0]["coverage"]["paired_hours"] == 0


@pytest.mark.parametrize("active", [True, False])
def test_repeated_quality_views_reuse_active_feedback_but_replay_historical_links(
    tmp_path, monkeypatch, active
):
    """Real plan and day reads reuse healthy runtime snapshots and retain historical archive fallback."""
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from tests.app.solar.support import fixture_weather

    seed(tmp_path, active=active)
    reads = []
    original = SqlDocuments.read_many

    def counted_segments(documents, names):
        """Observe actual feedback segment queries while leaving every archive result unchanged."""
        names = tuple(names)
        reads.extend(name for name in names if name.startswith("curtailment-segment-"))
        return original(documents, names)

    monkeypatch.setattr(SqlDocuments, "read_many", counted_segments)

    async def scenario():
        """Read both authenticated real endpoints repeatedly after startup restoration completes."""
        token = "solar-feedback-view-test-0123456789"
        async with TestClient(
            TestServer(
                create_app(
                    tmp_path, token, source="home_assistant", solar_weather_factory=fixture_weather
                )
            )
        ) as client:
            headers = {"Authorization": "Bearer " + token}
            metadata = setup_metadata()
            metadata["timezone"] = "UTC"
            if active:
                metadata["entities"].append(
                    {
                        "entity_id": "sensor.synthetic_pv",
                        "name": "Synthetic PV",
                        "unit": "kWh",
                        "device_id": "synthetic-meter",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    }
                )
                metadata["suggestions"].update(
                    solar=True,
                    solar_sources=["sensor.synthetic_pv"],
                    terms=[
                        {"entity_id": "sensor.import", "sign": 1},
                        {"entity_id": "sensor.synthetic_pv", "sign": 1},
                    ],
                    solar_configurations=[
                        {
                            "entity_id": "sensor.synthetic_pv",
                            "providers": [],
                            "issue": "solar_configuration_not_available",
                        }
                    ],
                )
            submitted = await client.post("/v1/ha/catalogue", headers=headers, json=metadata)
            assert submitted.status == 200, await submitted.text()
            reads.clear()
            for _ in range(3):
                response = await client.get(
                    "/v1/forecast-quality/synthetic-feedback-plan", headers=headers
                )
                assert response.status == 200
                source = (await response.json())["series"][0]
                assert source["coverage"]["paired_hours"] == 17
                assert source["metrics"]["mae_kwh"] == pytest.approx(0.07)
                assert source["alternatives"][0]["metrics"]["mae_kwh"] == pytest.approx(0.035)
                response = await client.get("/v1/forecast-quality/days/2026-06-15", headers=headers)
                assert response.status == 200
                daily = (await response.json())["series"][0]
                assert daily["daily"]["error_kwh"] is None
                assert all(value is None for value in daily["metrics"].values())
            assert len(reads) == (0 if active else 6)

    asyncio.run(scenario())


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
