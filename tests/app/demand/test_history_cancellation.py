"""Drain owned history work before releasing the real app lock after repeated request cancellation."""

import asyncio
from threading import Event

from aiohttp import ClientConnectionError

from open_energy_planner import app as app_module
from open_energy_planner.demand import api as demand_api
from open_energy_planner.demand.runtime import DemandRuntime
from open_energy_planner.demand.storage import DemandStore
from tests.app.demand.test_demand_app import definition, history_body
from tests.app.forecast.support.http import configure_forecasts, running_forecasts


def test_cancelled_history_request_keeps_owner_lock_until_commit_and_install(tmp_path, monkeypatch):
    """Two cancellations cannot release admission ownership or omit installation after a committed import."""
    owners = {}
    register = app_module.register_demand

    def capture(*args):
        """Retain the real runtime and asyncio lock without replacing any production route."""
        owners["runtime"], owners["lock"] = args[1], args[5]
        return register(*args)

    monkeypatch.setattr(app_module, "register_demand", capture)

    async def scenario():
        """Hold the real writer and cancel its request handler before releasing the durable operation."""
        async with running_forecasts(tmp_path) as (session, url):
            await configure_forecasts(session, url)
            response = await session.put(url + "/v1/demand", json=definition())
            assert response.status == 200
            body = await history_body(session, url, days=7)
            runtime = owners["runtime"]
            entered, release = Event(), Event()
            handler = []
            original_receive, original_write = runtime.receive, demand_api.complete_write

            def held(*args):
                """Hold the admitted worker before any new source/cohort pointer is written."""
                entered.set()
                assert release.wait(10)
                return original_receive(*args)

            async def observed_write(*args):
                """Capture the actual server handler task, retaining the production cancellation drain."""
                handler.append(asyncio.current_task())
                return await original_write(*args)

            async def post():
                """Keep the client connected until server-side cancellation closes its response."""
                async with session.post(url + "/v1/demand/history", json=body) as result:
                    return result.status, await result.text()

            monkeypatch.setattr(runtime, "receive", held)
            monkeypatch.setattr(demand_api, "complete_write", observed_write)
            pending = asyncio.create_task(post())
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                assert owners["lock"].locked() and len(handler) == 1
                for _ in range(2):
                    handler[0].cancel()
                    await asyncio.sleep(0)
                assert owners["lock"].locked()
                assert not pending.done()
                assert runtime.histories == {}
            finally:
                release.set()
            result = await asyncio.gather(pending, return_exceptions=True)
            await asyncio.gather(handler[0], return_exceptions=True)
            assert isinstance(result[0], ClientConnectionError)
            assert runtime.histories
            restored = DemandRuntime(DemandStore(runtime.store.documents))
            assert restored.histories == runtime.histories
            response = await session.get(url + "/v1/demand")
            assert response.status == 200
            assert (await response.json())["request"] is None

    asyncio.run(scenario())
