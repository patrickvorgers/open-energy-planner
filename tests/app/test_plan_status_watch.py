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


def test_shutdown_releases_a_pending_watch(monkeypatch):
    """Return a retryable failure immediately when the app restarts with a matched wait."""

    async def scenario():
        """Gate a real matched HTTP watch and then run the application's shutdown callbacks."""
        application = web.Application()
        watch = plan_status_watch.PlanStatusWatch()
        entered = asyncio.Event()
        initial = await watch.wait(None)
        wait = watch.wait

        async def track_wait(cursor):
            """Record the matching wait just before entering the pending branch."""
            assert cursor == initial
            entered.set()
            return await wait(cursor)

        monkeypatch.setattr(watch, "wait", track_wait)
        application.router.add_get("/v1/ha/plan-status/watch", watch.response)
        application.on_shutdown.append(watch.shutdown)
        async with TestServer(application) as server, ClientSession() as session:
            url = server.make_url("/v1/ha/plan-status/watch")
            awaiting = asyncio.create_task(session.get(url, params={"cursor": initial}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                assert not awaiting.done()
                await application.shutdown()
                async with await asyncio.wait_for(awaiting, timeout=2) as response:
                    assert response.status == 503
            finally:
                awaiting.cancel()
                await asyncio.gather(awaiting, return_exceptions=True)

    asyncio.run(scenario())
