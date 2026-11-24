"""Verify the focused HA reads and their authority against the real app and archive."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.appliances.forecasting import ConsumerForecasts, ConsumerView
from open_energy_planner.build_info import BUILD_INFO
from open_energy_planner.demand.runtime import DemandRuntime
from open_energy_planner.home_assistant import plan_api
from open_energy_planner.solar.runtime import SolarRuntime
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.planning.test_planning_pipeline import ImmediateSolver
from tests.ha_contract.support.connection import TOKEN


def test_command_projection_is_bounded_and_acknowledgement_idempotent(tmp_path, monkeypatch):
    """Read command status and a plan, then confirm only the first receipt needs a durable write."""
    original = SqlPlanRepository.save
    writes = []

    def tracked_save(repository):
        """Record the acknowledged plan whenever the real lifecycle commits durable state."""
        original(repository)
        writes.append(repository.state["acknowledged_plan"])

    monkeypatch.setattr(SqlPlanRepository, "save", tracked_save)

    async def scenario():
        """Keep metadata and archive evidence out of the command plan response."""
        headers = {
            "Authorization": "Bearer " + TOKEN,
            "X-OEP-View": "commands",
            "X-OEP-Release": BUILD_INFO["version"],
            "X-OEP-Build": BUILD_INFO["build"] or "",
        }
        async with TestClient(
            TestServer(create_app(tmp_path, TOKEN, solver=ImmediateSolver()))
        ) as client:
            plan_id = await published_plan(client, headers)
            async with client.get(
                "/v1/ha/plan", params={"plan_id": plan_id}, headers=headers
            ) as response:
                assert response.status == 200
                document = await response.json()
                assert len(await response.read()) < 256 * 1024
            assert document.keys() == {"instance_id", "plan", "devices", "ev_points"}
            assert document["plan"]["plan_id"] == plan_id
            assert {"input_snapshot", "independent_costs"}.isdisjoint(document["plan"])
            for _ in range(2):
                async with client.post(
                    "/v1/ack", json={"plan_id": plan_id}, headers=headers
                ) as response:
                    assert response.status == 200
                    assert await response.json() == {"accepted": True}
            assert writes.count(plan_id) == 1
            async with client.get("/v1/ha/plan-status", headers=headers) as response:
                assert (await response.json())["acknowledged_plan"] == plan_id
            async with client.get("/v1/snapshot", headers=headers) as response:
                assert response.status == 404
            expired = datetime.fromisoformat(document["plan"]["valid_until"]) + timedelta(seconds=1)
            monkeypatch.setattr(
                plan_api,
                "datetime",
                SimpleNamespace(
                    now=lambda _timezone: expired.astimezone(UTC),
                    fromisoformat=datetime.fromisoformat,
                ),
            )
            async with client.get("/v1/ha/plan-status", headers=headers) as response:
                assert (await response.json())["plan_id"] is None
            async with client.get(
                "/v1/ha/plan", params={"plan_id": plan_id}, headers=headers
            ) as response:
                assert response.status == 409

    asyncio.run(scenario())


def test_pause_revokes_inflight_projection_and_status_before_response(tmp_path, monkeypatch):
    """Hold a real command projection and prove a pause and lightweight status can overtake it."""
    original = plan_api.execution_document

    async def scenario():
        """Reject the old identity after the background projection resumes."""
        entered = asyncio.Event()
        release = threading.Event()
        flushing = asyncio.Event()
        finish_save = threading.Event()
        loop = asyncio.get_running_loop()
        save = SqlPlanRepository.save

        def delayed_projection(plan):
            """Pause inside actual command document rendering outside the app lock."""
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return original(plan)

        def delayed_pause(repository):
            """Leave the previously active plan committed until the gated pause can persist."""
            if not repository.state["enabled"]:
                loop.call_soon_threadsafe(flushing.set)
                assert finish_save.wait(5)
            save(repository)

        monkeypatch.setattr(plan_api, "execution_document", delayed_projection)
        monkeypatch.setattr(SqlPlanRepository, "save", delayed_pause)
        headers = {"Authorization": "Bearer " + TOKEN}
        async with TestClient(
            TestServer(create_app(tmp_path, TOKEN, solver=ImmediateSolver()))
        ) as client:
            plan_id = await published_plan(client, headers)
            reading = asyncio.create_task(
                client.get("/v1/ha/plan", params={"plan_id": plan_id}, headers=headers)
            )
            pausing = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                pausing = asyncio.create_task(
                    client.put(
                        "/v1/enabled",
                        json={"enabled": False, "request_id": "pause-plan"},
                        headers=headers,
                    )
                )
                await asyncio.wait_for(flushing.wait(), 2)
                async with asyncio.timeout(1):
                    async with client.get("/v1/ha/plan-status", headers=headers) as response:
                        status = await response.json()
                        assert status["plan_id"] is None and status["enabled"] is False
                    assert not pausing.done()
                    finish_save.set()
                    pause_response = await pausing
                    assert pause_response.status == 200
                    pause_response.release()
                    async with client.post(
                        "/v1/ack", json={"plan_id": plan_id}, headers=headers
                    ) as response:
                        assert response.status == 409
                assert not reading.done()
            finally:
                release.set()
                finish_save.set()
                if pausing is not None:
                    (await pausing).release()
                response = await reading
                assert response.status == 409
                response.release()

    asyncio.run(scenario())


def test_service_requests_skip_dashboard_and_command_assembly(tmp_path, monkeypatch):
    """The HA requests route returns only domain requests, diagnostics and required device views."""

    def unrelated_projection(*_args, **_kwargs):
        """A peer data request has no reason to materialize app dashboard or command documents."""
        raise AssertionError("HA requested a dashboard or plan document")

    async def scenario():
        """Keep a real published plan but fail if a data request consults its full document."""
        headers = {"Authorization": "Bearer " + TOKEN}
        async with TestClient(
            TestServer(create_app(tmp_path, TOKEN, solver=ImmediateSolver()))
        ) as client:
            await published_plan(client, headers)
            for runtime_type in (DemandRuntime, ConsumerForecasts, ConsumerView, SolarRuntime):
                monkeypatch.setattr(runtime_type, "document", unrelated_projection)
            monkeypatch.setattr(DemandRuntime, "capture_document", unrelated_projection)
            monkeypatch.setattr(plan_api, "execution_document", unrelated_projection)
            async with client.get("/v1/readiness") as response:
                assert response.status == 401
            async with client.get("/v1/readiness", headers=headers) as response:
                assert response.status == 200
                assert await response.json() == {
                    "source": "synthetic",
                    "plan_available": True,
                    "planning_status": "ready",
                }
            async with client.get("/v1/ha/requests", headers=headers) as response:
                assert response.status == 200
                metadata = await response.json()
            assert metadata.keys() == {
                "instance_id",
                "release",
                "source",
                "bonus_history",
                "savings_status",
                "forecast_quality_request",
                "settlement_request",
                "market_price_request",
                "demand",
                "solar",
                "consumers",
                "appliances",
                "ev",
                "thermal",
                "installation",
            }
            assert metadata["demand"].keys() == {
                "configuration",
                "history_issues",
                "issue",
                "request",
            }
            assert metadata["solar"].keys() == {
                "feedback_requests",
                "history_issues",
                "issue",
                "request",
            }
            assert metadata["consumers"].keys() == {"options", "selected", "result"}

    asyncio.run(scenario())


async def published_plan(client, headers):
    """Wait for synthetic startup publication through the focused public read."""
    async with asyncio.timeout(4):
        while True:
            async with client.get("/v1/ha/plan-status", headers=headers) as response:
                assert response.status == 200
                status = await response.json()
                if status["plan_id"] is not None:
                    return status["plan_id"]
            await asyncio.sleep(0.01)
