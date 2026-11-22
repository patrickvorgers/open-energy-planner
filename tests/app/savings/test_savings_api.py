"""Authenticated period access and historical reads never alter commands or invent measurement coverage."""

import asyncio

import pytest
from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from tests.app.savings.support.scenarios import END, START

TOKEN = "settlement-test-only-0123456789abcdef"


def test_period_requires_auth_and_bounds_without_replanning(tmp_path):
    """Unknown metering stays absent. Unauthorized or unbounded period requests fail without changing the active plan."""

    async def scenario():
        """Exercise the actual HTTP boundary and persistent app lifecycle."""
        from tests.app.planning.test_planning_pipeline import ImmediateSolver

        runner = web.AppRunner(create_app(tmp_path, TOKEN, solver=ImmediateSolver()))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with ClientSession() as client:
                async with client.get(url + "/v1/savings") as response:
                    assert response.status == 401
                headers = {"Authorization": "Bearer " + TOKEN}
                async with client.post(
                    url + "/v1/settlement", headers=headers, data="{invalid"
                ) as response:
                    assert response.status == 400
                    assert await response.text() == "Settlement body must be JSON"
                for _ in range(100):
                    async with client.get(url + "/v1/snapshot", headers=headers) as response:
                        state = await response.json()
                    if state["plan"] and state["planning"]["status"] == "ready":
                        before = state["plan"]
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("A current startup plan was not published")
                assert before["plan_id"]
                async with client.get(
                    url + "/v1/savings",
                    params={"start": START.isoformat(), "end": END.isoformat()},
                    headers=headers,
                ) as response:
                    assert response.status == 200
                    value = await response.json()
                    assert value["days"] == []
                    assert value["cash"]["savings"] is None
                    assert value["cash"]["coverage_seconds"] == 0
                    assert value["cash"]["expected_seconds"] == 900
                async with client.get(
                    url + "/v1/savings",
                    params={
                        "start": START.isoformat(),
                        "end": START.isoformat(),
                    },
                    headers=headers,
                ) as response:
                    assert response.status == 400
                async with client.get(url + "/v1/snapshot", headers=headers) as response:
                    assert (await response.json())["plan"] == before
                async with client.get(url + "/overview") as response:
                    assert response.status == 200
                    assert response.content_type == "text/html"
                    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_slow_settlement_does_not_hold_plan_publication_lock(tmp_path, monkeypatch):
    """A blocked settlement database round leaves the authenticated HA snapshot immediately readable."""
    from threading import Event

    from open_energy_planner.savings.daily import DailySavings

    entered, release = Event(), Event()

    def slow_process(self, *_args):
        """Emulate slow remote financial storage until the read-only control-path check finishes."""
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(DailySavings, "process", slow_process)

    async def scenario():
        """Use the actual worker and HTTP boundary. Release the blocked writer before cleanup."""
        runner = web.AppRunner(create_app(tmp_path, TOKEN, source="home_assistant"))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as client:
                response = await asyncio.wait_for(client.get(url + "/v1/snapshot"), 2)
                async with response:
                    assert response.status == 200
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(scenario())


def test_price_fetch_is_free_but_retention_waits_for_financial_publication(tmp_path, monkeypatch):
    """Network fetching and HA reads stay free. Price retention starts only after a blocked financial publication finishes."""
    from contextlib import asynccontextmanager
    from threading import Event

    from open_energy_planner.prices.service import PriceService
    from open_energy_planner.prices.storage.adapters.store import PriceStore
    from open_energy_planner.savings.daily import DailySavings
    from open_energy_planner.storage.adapters.documents import SqlDocuments
    from tests.app.price.test_prices import published

    entered, release, published_finance, saving = Event(), Event(), Event(), Event()

    def financial_publication(self, *_args):
        """Hold the worker's publication barrier until concurrent source and control-path checks finish."""
        entered.set()
        assert release.wait(10)
        published_finance.set()

    monkeypatch.setattr(DailySavings, "process", financial_publication)

    async def scenario():
        """Use the actual app lifecycle, shared engine and price acceptance instead of acquiring a test-only lock."""
        fetching, release_fetch, accepting, accepted = (asyncio.Event() for _ in range(4))

        class Source:
            """Keep remote fetching suspended independently of financial storage."""

            async def fetch_day(self, day, now):
                """Signal real provider work, then return one complete published delivery day."""
                fetching.set()
                await release_fetch.wait()
                return published(day, now, value=0.9)

        @asynccontextmanager
        async def factory(engine):
            """Borrow the actual active app database and observe durable acceptance ordering."""
            service = PriceService("NL", Source(), None, PriceStore(SqlDocuments(engine, "prices")))
            save, accept = service.store.save, service._accept

            def ordered_save(rows):
                """Detect persistence before financial publication even if the database itself would serialize writes."""
                saving.set()
                assert published_finance.is_set()
                return save(rows)

            async def observed_accept(candidate, day=None):
                """Expose the acceptance attempt before it waits on the app's publication barrier."""
                accepting.set()
                await accept(candidate, day)
                accepted.set()

            monkeypatch.setattr(service.store, "save", ordered_save)
            monkeypatch.setattr(service, "_accept", observed_accept)
            yield service

        runner = web.AppRunner(
            create_app(tmp_path, TOKEN, price_factory=factory, source="home_assistant")
        )
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            await asyncio.wait_for(fetching.wait(), 5)
            # Acquiring the financial barrier while fetching is suspended proves network IO is outside it.
            assert await asyncio.to_thread(entered.wait, 5)
            release_fetch.set()
            await asyncio.wait_for(accepting.wait(), 5)
            assert not await asyncio.to_thread(saving.wait, 0.2)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as client:
                response = await asyncio.wait_for(client.get(url + "/v1/snapshot"), 2)
                async with response:
                    assert response.status == 200
            release.set()
            await asyncio.wait_for(accepted.wait(), 5)
            assert saving.is_set()
        finally:
            release_fetch.set()
            release.set()
            await runner.cleanup()

    asyncio.run(scenario())
