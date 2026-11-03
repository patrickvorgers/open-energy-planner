"""API readiness and publication persistence while a planning worker is outstanding."""

import asyncio
from datetime import UTC, datetime

from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

TOKEN = "local-test-only-0123456789abcdef"


def test_api_remains_responsive_and_pause_is_immediate_during_calculation(tmp_path):
    """Delay optimization. Keep authenticated HTTP responsive and apply pause before the solver finishes."""

    async def scenario():
        started_day = datetime.now(UTC).date()
        started, release = asyncio.Event(), asyncio.Event()

        class BlockedSolver:
            async def solve(self, problem):
                started.set()
                await release.wait()
                return HighsSolver().solve(problem)

        runner = web.AppRunner(create_app(tmp_path, TOKEN, solver=BlockedSolver()))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            await started.wait()
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                async with session.get(url + "/v1/snapshot") as response:
                    pending = await response.json()
                    assert pending["plan"] is None
                    assert pending["planning"]["status"] == "calculating"
                async with session.put(
                    url + "/v1/enabled",
                    json={
                        "enabled": False,
                        "request_id": "pause-during-solve",
                    },
                ) as response:
                    assert (await response.json())["enabled"] is False
                release.set()
                for _ in range(100):
                    async with session.get(url + "/v1/snapshot") as response:
                        state = await response.json()
                    if state["planning"]["status"] == "ready":
                        break
                    await asyncio.sleep(0.01)
                assert state["enabled"] is False
                assert state["plan"]["independent_costs"]["objective"] < 0
                archive = SqlPlanRepository(tmp_path)
                try:
                    assert archive.get_plan(state["plan"]["plan_id"]) == state["plan"]
                finally:
                    archive.close()
        finally:
            release.set()
            await runner.cleanup()
        from open_energy_planner.optimization.performance import CalculationHistory
        from open_energy_planner.storage.adapters.documents import SqlDocuments

        reopened = SqlPlanRepository(tmp_path)
        try:
            history = CalculationHistory(SqlDocuments(reopened.engine, "planning"))
            # Cleanup may finish on the UTC day after this short calculation started.
            observed_days = {started_day, datetime.now(UTC).date()}
            rows = [
                row
                for selected_day in observed_days
                for row in history.day(selected_day.isoformat(), "UTC")
            ]
            assert len(rows) == 1
            assert rows[0]["reason"] == "published"
            assert rows[0]["plan_id"] == state["plan"]["plan_id"]
            assert history.detail(rows[0]["id"])["progress"]
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_persistence_failure_does_not_expose_the_candidate_as_current(tmp_path, monkeypatch):
    """Fail initial plan persistence. Keep the uncommitted candidate out of current publication."""
    save = SqlPlanRepository.save

    def fail_candidate(repository):
        if repository.state["plan"] is not None:
            raise OSError("Synthetic disk fault")
        save(repository)

    monkeypatch.setattr(SqlPlanRepository, "save", fail_candidate)

    async def scenario():
        class Solver:
            async def solve(self, problem):
                return HighsSolver().solve(problem)

        runner = web.AppRunner(create_app(tmp_path, TOKEN, solver=Solver()))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                for _ in range(100):
                    async with session.get(url + "/v1/snapshot") as response:
                        state = await response.json()
                    if state["planning"]["status"] == "failed":
                        break
                    await asyncio.sleep(0.01)
                assert state["planning"]["status"] == "failed"
                assert state["plan"] is None
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
