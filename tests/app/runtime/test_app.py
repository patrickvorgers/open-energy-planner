"""Real HTTP, durable demo state and identical control retry semantics."""

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("aiohttp")
from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.ha_contract.support.commands import command_plan

TOKEN = "local-test-only-0123456789abcdef"


def test_demo_restart_retains_plan_intent_and_immutable_previous_plans(tmp_path):
    """Restart stored demo state. Retain enable intent/current identity and the original archived plan unchanged."""
    first = SqlPlanRepository(tmp_path)
    original = command_plan()
    first.state["plan"] = original
    first.state["enabled"] = False
    first.save()
    loaded = SqlPlanRepository(tmp_path)
    assert loaded.state["plan"] == original
    assert loaded.state["enabled"] is False
    assert loaded.get_plan(original["plan_id"]) == original


def test_short_token_is_rejected(tmp_path):
    """Configure an undersized installation token. Reject app creation before exposing the API."""
    with pytest.raises(ValueError):
        create_app(tmp_path, "short")


def test_control_retries_create_only_one_new_plan_and_ack_is_not_execution(tmp_path):
    """Retry one enable request. Retain one new plan and record acknowledgement without claiming physical execution."""

    async def scenario():
        from tests.app.planning.test_planning_pipeline import ImmediateSolver

        runner = web.AppRunner(create_app(tmp_path, TOKEN, solver=ImmediateSolver()))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:

                async def ready(previous=None):
                    """Wait for the current production publication rather than assuming synchronous planning."""
                    for _ in range(100):
                        async with session.get(url + "/v1/snapshot") as response:
                            state = await response.json()
                        if (
                            state["plan"]
                            and state["planning"]["status"] == "ready"
                            and state["plan"]["plan_id"] != previous
                        ):
                            return state["plan"]["plan_id"]
                        await asyncio.sleep(0.01)
                    pytest.fail("A fresh current-device plan was not published")

                original_id = await ready()
                plans = []
                for request_index in range(2):
                    async with session.put(
                        url + "/v1/enabled",
                        json={
                            "enabled": True,
                            "request_id": "same-control-request",
                        },
                    ) as response:
                        assert response.status == 200
                        assert await response.json() == {
                            "accepted": True,
                            "enabled": True,
                            "request_id": "same-control-request",
                        }
                    plans.append(await ready(original_id) if request_index == 0 else await ready())
                assert plans[0] == plans[1]
                async with session.post(url + "/v1/ack", json={"plan_id": plans[0]}) as response:
                    assert response.status == 200
                async with session.get(url + "/v1/snapshot") as response:
                    data = await response.json()
                    assert data["acknowledged_plan"] == plans[0]
                assert len(SqlPlanRepository(tmp_path).list_plans()) == 2
                async with session.get(url + "/v1/plans?limit=1") as response:
                    assert response.status == 200
                    history = await response.json()
                assert len(history["plans"]) == 1 and history["next_before"]
                async with session.get(
                    url + "/v1/plans", params={"before": history["next_before"]}
                ) as response:
                    previous = await response.json()
                assert len(previous["plans"]) == 1
                assert previous["plans"][0]["plan_id"] != history["plans"][0]["plan_id"]
                async with session.get(url + "/v1/plans/" + plans[0]) as response:
                    assert response.status == 200
                    archived = await response.json()
                assert archived == data["plan"]
                async with session.get(url + "/v1/plans/missing") as response:
                    assert response.status == 404
                async with session.get(url + "/v1/plans?limit=101") as response:
                    assert response.status == 400
                async with (
                    ClientSession() as unauthenticated,
                    unauthenticated.get(url + "/v1/plans") as response,
                ):
                    assert response.status == 401
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [[], {"enabled": "false"}, {"enabled": True}])
def test_bad_controls_do_not_change_persisted_intent(tmp_path, body):
    """Send malformed enable requests. Return HTTP 400 while persisted enable intent stays unchanged."""

    async def scenario():
        runner = web.AppRunner(create_app(tmp_path, TOKEN, source="home_assistant"))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
                async with session.put(url + "/v1/enabled", json=body) as response:
                    assert response.status == 400
                assert SqlPlanRepository(tmp_path).state["enabled"] is True
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_synthetic_device_catalogue_survives_expired_publication_inputs(tmp_path, monkeypatch):
    """Expired publication clears the requested snapshot and commands, while the declared battery remains discoverable."""
    from custom_components.open_energy_planner.client import LocalClient
    from open_energy_planner.planning import PlanningCoordinator
    from tests.app.planning.test_planning_pipeline import ImmediateSolver

    publication_rejection = PlanningCoordinator.publication_rejection

    def reject_expired(self, document, now, current):
        """Exercise the real publication expiry guard with a clock two minutes after capture."""
        return publication_rejection(self, document, now + timedelta(minutes=2), current)

    monkeypatch.setattr(PlanningCoordinator, "publication_rejection", reject_expired)

    async def scenario():
        """Observe actual authenticated HTTP before solving and after coordinator invalidation."""
        started, release = asyncio.Event(), asyncio.Event()

        class DelayedSolver(ImmediateSolver):
            """Hold the actual HiGHS candidate until the initial catalogue has been observed."""

            async def solve(self, problem):
                """Solve the same installation only after the initial HTTP snapshot is read."""
                started.set()
                await release.wait()
                return await super().solve(problem)

        runner = web.AppRunner(create_app(tmp_path, TOKEN, solver=DelayedSolver()))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            await asyncio.wait_for(started.wait(), 2)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:
                async with session.get(url + "/v1/snapshot") as response:
                    before = await response.json()
                assert before["planning"]["requested_snapshot_id"]
                assert before["installation"]["devices"] == [
                    {
                        "id": "battery",
                        "kind": "battery",
                        "name": "Battery",
                        "configured": True,
                        "can_follow_grid": False,
                    }
                ]
                release.set()
                for _ in range(100):
                    async with session.get(url + "/v1/snapshot") as response:
                        after = await response.json()
                    if after["planning"]["reason"] == "publication_inputs_expired":
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("Publication expiry did not invalidate the current snapshot")
                assert after["planning"]["status"] == "not_ready"
                assert after["planning"]["requested_snapshot_id"] is None
                assert after["installation"]["devices"] == before["installation"]["devices"]
                assert after["plan"] is None
                execution = await LocalClient(session, url, TOKEN).snapshot()
                assert execution["plan"] is None
                assert execution["installation"]["devices"] == before["installation"]["devices"]
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(scenario())
