"""Authenticated period access and historical reads never alter commands or invent measurement coverage."""

import asyncio

import pytest
from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from tests.app.savings.support.scenarios import END, START

TOKEN = "settlement-test-only-0123456789abcdef"


@pytest.mark.parametrize("multiple", [False, True])
def test_historical_bonus_ingress_is_authenticated_versioned_and_separate(
    tmp_path, monkeypatch, multiple
):
    """Single and multi-meter history reject bad evidence before persistence and preserve current confirmations."""
    from dataclasses import replace
    from datetime import datetime

    from open_energy_planner.contracts.bonus_history import BaselineRequest
    from open_energy_planner.savings.bonus import HistoricalBonuses
    from open_energy_planner.storage.counters import CounterStore
    from tests.app.contract.test_contract_app import running
    from tests.app.history.test_history_app import prepare
    from tests.database.support.archive import read_current, read_document

    services = []
    original = HistoricalBonuses.__init__

    def remember(self, store):
        """Observe the real app service without replacing its validation or persistence."""
        original(self, store)
        services.append(self)

    monkeypatch.setattr(HistoricalBonuses, "__init__", remember)

    async def scenario():
        """Transfer actual bounded annual JSON through the authenticated historical endpoint."""
        async with running(tmp_path) as (session, url):
            request, body = await prepare(session, url, multiple=multiple, statistics=False)
            pending = BaselineRequest(
                request["contract_version"],
                "frozen-historical-bindings",
                None,
                request["entity_id"],
                tuple(
                    (identity, datetime.fromisoformat(start))
                    for identity, start in request["periods"]
                ),
                tuple(
                    (identity, datetime.fromisoformat(start))
                    for identity, start in request["active_periods"]
                ),
                datetime.fromisoformat(request["end"]),
                tuple(request.get("entity_ids", [request["entity_id"]])),
            )
            services[0].pending = pending
            body["request_version"] = pending.version
            path = url + "/v1/settlement/bonus-history"
            async with ClientSession() as anonymous, anonymous.post(path, json=body) as response:
                assert response.status == 401
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["settlement_bonus_request"][
                    "version"
                ] == pending.version
            async with session.post(path, json={**body, "request_version": "stale"}) as response:
                assert response.status == 409
            async with session.post(path, json={**body, "hours": body["hours"][1:]}) as response:
                assert response.status == 400
            for timestamp in (1e300, -1e300, 1e12):
                async with session.post(
                    path, json={**body, "hours": [[timestamp, 1, 1]]}
                ) as response:
                    assert response.status == 400
                    assert await response.text() == "Invalid history timestamp"
                assert services[0].archive.current is None
            for total, meter in ((10**400, 1), (1, 10**400)):
                invalid = {**body, "hours": [[pending.end.timestamp() - 3600, total, meter]]}
                async with session.post(path, json=invalid) as response:
                    assert response.status == 400
                    assert await response.text() == "Invalid energy history value"
                assert services[0].archive.current is None
            async with session.post(
                path, json={"request_version": pending.version, "error": "recorder_unavailable"}
            ) as response:
                assert response.status == 200
                assert (await response.json())["accepted"] is False
            assert services[0].archive.current is None
            async with session.post(
                path,
                json={**body, "component_meters": [["sensor.wrong", body["hours"][-1][2]]]},
            ) as response:
                assert response.status == 400
                assert await response.text() == "History components changed"
            assert services[0].archive.current is None
            assert services[0].pending == pending
            async with session.post(path, json=body) as response:
                assert response.status == 200, await response.text()
                assert (await response.json())["accepted"] is True
            saved = services[0].archive.current
            assert saved.seeds[0].used_kwh == 2 * (len(body["hours"]) - 1)
            assert len(saved.seeds[0].component_meters) == (2 if multiple else 0)
            assert read_current(tmp_path, CounterStore) is None
            assert read_document(tmp_path, "savings-bonus-counters", "current.json")
            async with session.post(path, json=body) as response:
                assert response.status == 409
            services[0].pending = replace(pending, counter_version="changed-current-confirmation")
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["settlement_bonus_request"] is None
            async with session.post(path, json=body) as response:
                assert response.status == 409

    asyncio.run(scenario())


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
                    assert value["intervals"] == []
                    assert value["coverage"] == {"evaluated": 0, "expected": 1, "estimated": 0}
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


@pytest.mark.parametrize("multiple", [False, True])
def test_historical_statistics_ingress_preserves_live_counters(tmp_path, monkeypatch, multiple):
    """Authenticated historical sum endpoints retain current reference identity without changing today's budget."""
    from dataclasses import replace
    from datetime import datetime, timedelta

    from open_energy_planner.contracts.bonus_history import BaselineRequest
    from open_energy_planner.savings.bonus import HistoricalBonuses
    from open_energy_planner.storage.counters import CounterStore
    from tests.app.contract.test_contract_app import running
    from tests.app.history.test_history_app import prepare
    from tests.database.support.archive import read_current

    services = []
    original = HistoricalBonuses.__init__

    def remember(self, store):
        """Retain the actual app-owned service to set its otherwise worker-owned pending request."""
        original(self, store)
        services.append(self)

    monkeypatch.setattr(HistoricalBonuses, "__init__", remember)

    async def scenario():
        """Read the annual budget, then ingest an older endpoint through the real versioned HTTP route."""
        async with running(tmp_path) as (session, url):
            request, body = await prepare(session, url, multiple=multiple)
            async with session.post(url + "/v1/history", json=body) as response:
                assert response.status == 200, await response.text()
            current = read_current(tmp_path, CounterStore)
            pending = BaselineRequest(
                request["contract_version"],
                "historical-bindings",
                current.version,
                request["entity_id"],
                tuple(
                    (identity, datetime.fromisoformat(start))
                    for identity, start in request["periods"]
                ),
                tuple(
                    (identity, datetime.fromisoformat(start))
                    for identity, start in request["active_periods"]
                ),
                datetime.fromisoformat(request["end"]) - timedelta(hours=2),
                tuple(request.get("entity_ids", [request["entity_id"]])),
                True,
            )
            body = {
                "request_version": pending.version,
                "statistics": [
                    {**channel, "hours": channel["hours"][:-2]} for channel in body["statistics"]
                ],
            }
            services[0].pending = pending
            path = url + "/v1/settlement/bonus-history"
            async with ClientSession() as anonymous, anonymous.post(path, json=body) as response:
                assert response.status == 401
            async with session.post(path, json={**body, "request_version": "stale"}) as response:
                assert response.status == 409
            for timestamp in (1e300, -1e300):
                invalid_timestamp = {
                    **body,
                    "statistics": [
                        {**channel, "hours": [[timestamp, 1, 1]]} for channel in body["statistics"]
                    ],
                }
                async with session.post(path, json=invalid_timestamp) as response:
                    assert response.status == 400
                    assert "inspect application diagnostics" in await response.text()
                    assert services[0].archive.current is None
            invalid = {
                **body,
                "statistics": [
                    {**channel, "metadata_id": channel["metadata_id"] + 99}
                    for channel in body["statistics"]
                ],
            }
            async with session.post(path, json=invalid) as response:
                assert response.status == 400
                assert services[0].archive.current is None
            async with session.post(path, json=body) as response:
                assert response.status == 200, await response.text()
            saved = services[0].archive.current.seeds[0]
            assert saved.observed_at == pending.end
            assert saved.used_kwh == pytest.approx(2 * (len(body["statistics"][0]["hours"]) - 1))
            assert len(saved.component_meters) == (2 if multiple else 1)
            assert (
                saved.statistics.reference_version == current.seeds[0].statistics.reference_version
            )
            assert read_current(tmp_path, CounterStore) == current
            async with session.post(path, json=body) as response:
                assert response.status == 409
            services[0].pending = replace(pending, counter_version="obsolete")
            async with session.post(path, json=body) as response:
                assert response.status == 409

    asyncio.run(scenario())
