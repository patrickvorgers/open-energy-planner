"""Exercise the advisory plan subscription without transferring command data."""

import asyncio

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from open_energy_planner.home_assistant import plan_status_watch


def test_publication_revocation_and_restart_wake_matching_cursor(monkeypatch):
    """Keep duplicate positions quiet and notify on publication, revocation and a new process."""

    async def scenario():
        """Start deterministic matching waits before replacing the authority."""
        monkeypatch.setattr(plan_status_watch, "WATCH_SECONDS", 0.02)
        watch = plan_status_watch.PlanStatusWatch()
        watch.publish(("sources-A", None))
        initial = await watch.wait(None)
        waiting = asyncio.create_task(watch.wait(initial))
        await asyncio.sleep(0)
        watch.publish(("sources-A", None))
        assert not waiting.done()
        watch.publish(("sources-A", "plan-B"))
        published = await waiting
        assert initial != published
        assert await watch.wait(initial) == published
        assert await watch.wait(published) == published

        waiting = asyncio.create_task(watch.wait(published))
        await asyncio.sleep(0)
        watch.publish(("sources-A", None))
        revoked = await waiting
        assert revoked not in {initial, published}
        restarted = plan_status_watch.PlanStatusWatch()
        assert await restarted.wait(revoked) != revoked

    asyncio.run(scenario())


def test_watch_http_replies_only_with_cursor_and_rejects_invalid_parameter():
    """Return a small non-cacheable cursor and reject malformed wake requests."""

    async def scenario():
        """Use real HTTP against the app's subscription handler."""
        application = web.Application()
        watch = plan_status_watch.PlanStatusWatch()
        application.router.add_get("/v1/ha/plan-status/watch", watch.response)
        async with TestServer(application) as server, ClientSession() as session:
            async with session.get(server.make_url("/v1/ha/plan-status/watch")) as response:
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"
                result = await response.json()
                assert result == {"cursor": await watch.wait(None)}
            async with session.get(
                server.make_url("/v1/ha/plan-status/watch?cursor=plan-A")
            ) as response:
                assert response.status == 400

    asyncio.run(scenario())
