"""Authenticated contract configuration through real HTTP and the live planning chain."""

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.home_assistant.observations import PlanningBasis
from open_energy_planner.optimization.domain import Battery, Interval
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.storage.counters import CounterStore
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_prices import price_series
from tests.app.installation.support.problems import installation_problem
from tests.app.planning.test_live_app import Solver, configure, send, wait_ready
from tests.database.support.archive import (
    list_documents,
    make_store,
    read_current,
    read_plan,
    read_version,
)
from tests.ha_contract.test_observation_inputs import (
    configuration,
    configuration_document,
    report_body,
)

TOKEN = "local-test-only-0123456789abcdef"


@pytest.mark.parametrize("cancel_count", [1, 2])
@pytest.mark.parametrize("save_fails", [False, True])
@pytest.mark.parametrize("resource", ["contracts", "bonus-counters"])
def test_cancelled_contract_write_installs_successful_revision_before_exiting(
    tmp_path, monkeypatch, cancel_count, save_fails, resource
):
    """Cancel successful/failing settings saves once or twice. Install success before exit and preserve storage failures without activating a contract."""

    async def scenario():
        """Invoke app handlers with a controlled storage thread and prove later observations cannot use placeholder tariffs."""
        now = datetime.now(UTC)
        app = create_app(
            tmp_path,
            TOKEN,
            source="home_assistant",
            basis_source=CachedFuture(now),
            solver=Solver(),
        )
        runner = web.AppRunner(app)
        await runner.setup()
        started, release = threading.Event(), threading.Event()
        store_type = ContractStore if resource == "contracts" else CounterStore
        save = store_type.save

        def blocked_save(store, candidate):
            """Hold the real archive write until cancellation reaches its owning request."""
            started.set()
            if not release.wait(5):
                raise TimeoutError("Test writer was not released")
            if save_fails:
                raise OSError("Fixture contract write failure")
            save(store, candidate)

        class Request:
            """Provide already authenticated request JSON to the application handler under test."""

            def __init__(self, body):
                """Bind the explicit test document without HTTP or token-handling concerns."""
                self.body = body
                self.headers = {}

            async def json(self):
                """Return the immutable-by-convention request fixture."""
                return self.body

        def handler(path, method):
            """Select the registered application route without exposing its closure state."""
            return next(
                route.handler
                for route in app.router.routes()
                if route.resource.canonical == path and route.method == method
            )

        body = (
            settings()
            if resource == "contracts"
            else {
                "seeds": [
                    {
                        "rule_id": "solar",
                        "period_start": now.replace(
                            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
                        ).isoformat(),
                        "used_kwh": 0,
                        "meter_kwh": 50,
                        "entity_id": "sensor.export",
                        "observed_at": now.isoformat(),
                    }
                ]
            }
        )
        monkeypatch.setattr(store_type, "save", blocked_save)
        task = asyncio.create_task(handler("/v1/" + resource, "PUT")(Request(body)))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            release.set()
            with pytest.raises(OSError if save_fails else asyncio.CancelledError):
                await task
        try:
            if save_fails:
                assert read_current(tmp_path, store_type) is None
                return
            assert read_current(tmp_path, store_type) is not None
            response = await handler("/v1/sources", "PUT")(
                Request(configuration_document(configuration()))
            )
            import json

            state = json.loads(response.text)
            config = replace(configuration(), revision=state["measurement_config"]["revision"])
            response = await handler("/v1/observations", "POST")(
                Request(report_body(config, now=datetime.now(UTC)))
            )
            state = json.loads(response.text)
            # This app deliberately has no price runtime. Installation must therefore
            # report missing prices rather than use its pre-contract 999 placeholders.
            assert state["planning"]["reason"] == (
                "market_prices_unavailable"
                if resource == "contracts"
                else "contract_not_configured"
            )
            assert state["plan"] is None
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


class CachedFuture:
    """Provide declared energy forecasts. The production contract adapter must supply rates."""

    def __init__(self, now):
        """Initialize a two-quarter fixture with deliberately incorrect placeholder tariffs."""
        self.problem = installation_problem(
            (
                Interval(now, 0.25, 999, 999, load_kwh=0.25),
                Interval(now + timedelta(minutes=15), 0.25, 999, 999),
            ),
            battery=Battery(1, 0.5, charge_limit_w=0, discharge_limit_w=0),
        )

    def current(self, config, now, *, report=None):
        """Expose immutable forecasts and limits without pretending they are production predictions."""
        return PlanningBasis(
            config.revision,
            "test-energy-forecast",
            "placeholder",
            now + timedelta(minutes=30),
            self.problem,
        )


@asynccontextmanager
async def running(path, *, with_export=False):
    """Run the real app with isolated cached sources and no external provider or physical device calls."""
    now = datetime.now(UTC)
    original = price_series()
    shift = now - original.points[0].start
    series = replace(
        original,
        received_at=now,
        issued_at=now,
        points=tuple(
            replace(point, start=point.start + shift, end=point.end + shift)
            for point in original.points
        ),
    )
    store = make_store(path, PriceStore)
    store.save((series,))

    @asynccontextmanager
    async def prices(_database_engine):
        """Yield accepted typed prices without enabling network retrieval."""
        yield PriceService("NL", None, None, store)

    future = CachedFuture(now)
    if with_export:
        future.problem = installation_problem(
            tuple(
                replace(step, pv_kwh=pv)
                for step, pv in zip(future.problem.intervals, (0.75, 1), strict=True)
            ),
            battery=future.problem.batteries[0].battery,
        )
    runner = web.AppRunner(
        create_app(
            path,
            TOKEN,
            source="home_assistant",
            basis_source=future,
            solver=Solver(),
            price_factory=prices,
        )
    )
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
            yield session, url
    finally:
        await runner.cleanup()


def test_contract_preview_save_correction_and_plan_history_over_real_http(tmp_path):
    """Preview without writes, apply and correct rates, then preserve the original validated plan and rule revision."""

    async def scenario():
        """Exercise authenticated settings, measured-state assembly, real HiGHS validation and immutable plan history."""
        async with running(tmp_path) as (session, url):
            state = await configure(session, url)
            await send(session, url, state["measurement_config"])
            async with session.get(url + "/v1/snapshot") as response:
                assert (await response.json())["planning"]["reason"] == "contract_not_configured"
            body = settings()
            async with session.post(url + "/v1/contracts/preview", json=body) as response:
                assert response.status == 200
                preview = await response.json()
                assert [item["market"] for item in preview["preview"][0]] == [
                    -0.2,
                    -0.1,
                    0.0,
                    0.1,
                    0.2,
                ]
                assert preview["preview"][0][2] == {
                    "market": 0,
                    "buy_price": 0.121,
                    "sell_price": -0.02,
                }
            assert not list_documents(tmp_path, "contracts")
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 200
                saved = await response.json()
            first = await wait_ready(session, url)
            plan = first["plan"]
            assert plan["input_snapshot"]["contract_version"] == saved["version"]
            assert plan["input_snapshot"]["problem"]["intervals"][0]["buy_price"] == 0.242
            assert plan["independent_costs"]["import_cost"] > 0
            archive = plan["plan_id"]
            previous = read_plan(tmp_path, archive)
            body = settings(buy="market + 0.5")
            body["expected_version"] = saved["version"]
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 200
                corrected = await response.json()
            second = await wait_ready(session, url)
            assert second["plan"]["plan_id"] != plan["plan_id"]
            assert second["plan"]["input_snapshot"]["contract_version"] == corrected["version"]
            assert second["plan"]["input_snapshot"]["problem"]["intervals"][0]["buy_price"] == 0.6
            assert read_plan(tmp_path, archive) == previous
            assert (
                read_version(tmp_path, ContractStore, saved["version"])
                .periods[0]
                .import_formula.text
                == "(market + 0.10) * 1.21"
            )
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 409
            assert read_current(tmp_path, ContractStore).version == corrected["version"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "script",
        "bonus",
        "divide_zero",
        "positive_sample",
        "negative_sample",
        "bonus_sample",
        "shape",
    ],
)
def test_invalid_contract_api_requests_do_not_mutate_settings(tmp_path, fault):
    """Reject unsupported syntax, invalid structure or undefined sample rates before persistence and retain the accepted revision."""

    async def scenario():
        """Apply one valid contract then submit an invalid replacement over the same authenticated API."""
        async with running(tmp_path) as (session, url):
            async with session.put(url + "/v1/contracts", json=settings()) as response:
                original = await response.json()
            body = settings()
            body["expected_version"] = original["version"]
            if fault == "script":
                body["periods"][0]["import_formula"] = "__import__('os')"
            elif fault == "bonus":
                body["bonus"] = {"rate": 0.1}
            elif fault == "divide_zero":
                body["periods"][0]["import_formula"] = "1 / market"
            elif fault in {"positive_sample", "negative_sample"}:
                body["periods"][0]["import_formula"] = (
                    "1 / (market - 0.2)" if fault == "positive_sample" else "1 / (market + 0.2)"
                )
            elif fault == "bonus_sample":
                body["bonuses"] = [
                    {
                        "rule_id": "sample-edge",
                        "effective_from": body["periods"][0]["effective_from"],
                        "rate_formula": "1 / abs(market - 0.2)",
                        "cap_kwh": 100,
                        "timezone": "UTC",
                    }
                ]
            else:
                body["periods"] = [None]
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 400
            async with session.get(url + "/v1/contracts") as response:
                assert (await response.json())["version"] == original["version"]

    asyncio.run(scenario())


def test_contract_routes_require_the_existing_installation_token(tmp_path):
    """Reject anonymous contract reads, previews and writes without creating any revision."""

    async def scenario():
        """Access all contract routes through real unauthenticated HTTP and require 401."""
        async with running(tmp_path) as (_, url):
            async with ClientSession() as session:
                for method, path in (
                    ("GET", "/v1/contracts"),
                    ("PUT", "/v1/contracts"),
                    ("POST", "/v1/contracts/preview"),
                ):
                    async with session.request(method, url + path, json=settings()) as response:
                        assert response.status == 401
            assert not list_documents(tmp_path, "contracts")

    asyncio.run(scenario())


@pytest.mark.parametrize("identity", [{"market_area": "DE-LU"}, {"currency": "USD"}])
def test_contract_identity_must_match_active_prices_before_persistence(tmp_path, identity):
    """Reject foreign-area/currency previews and saves, retaining the current valid NL/EUR revision."""

    async def scenario():
        """Validate identity through authenticated HTTP with a configured real normalized price runtime."""
        async with running(tmp_path) as (session, url):
            async with session.put(url + "/v1/contracts", json=settings()) as response:
                original = await response.json()
            body = {**settings(), **identity, "expected_version": original["version"]}
            for method, path in (("POST", "/v1/contracts/preview"), ("PUT", "/v1/contracts")):
                async with session.request(method, url + path, json=body) as response:
                    assert response.status == 400
            assert read_current(tmp_path, ContractStore).version == original["version"]
            assert len(list_documents(tmp_path, "contracts")) == 2  # Archive plus pointer.

    asyncio.run(scenario())


def test_current_price_inputs_need_no_plan_or_bonus_counter_and_follow_saved_rules(tmp_path):
    """Before any installation or plan, .10 market gives .08 base + .012 bonus. Saving .02 instead changes only the read-only preview, not history."""

    async def scenario():
        """Exercise the authenticated endpoint against cached prices and real contract persistence without running a solver."""
        async with running(tmp_path) as (session, url):
            body = settings()
            body["bonuses"] = [
                {
                    "rule_id": "preview",
                    "effective_from": "2026-01-01T00:00:00Z",
                    "rate_formula": "0.10 * (market + 0.02)",
                    "price_condition": {
                        "expression": "market + 0.02",
                        "operator": "greater_than",
                        "threshold": 0,
                    },
                    "cap_kwh": 7500,
                    "timezone": "Europe/Amsterdam",
                }
            ]
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 200
                saved = await response.json()
            async with session.get(url + "/v1/contracts/budgets") as response:
                assert any(row["issue"] for row in (await response.json())["budgets"])
            async with session.get(url + "/v1/prices/inputs") as response:
                assert response.status == 200
                first = await response.json()
            assert first["issue"] is None and first["has_bonus"]
            assert first["contract_version"] == saved["version"]
            assert first["intervals"][0]["buy_price"] == pytest.approx(0.242)
            assert first["intervals"][0]["total_export_price"] == pytest.approx(0.092)
            body["expected_version"] = saved["version"]
            body["bonuses"][0]["rate_formula"] = "0.02"
            async with session.put(url + "/v1/contracts", json=body) as response:
                assert response.status == 200
                changed = await response.json()
            async with session.get(url + "/v1/prices/inputs") as response:
                second = await response.json()
            assert second["contract_version"] == changed["version"] != first["contract_version"]
            assert second["intervals"][0]["total_export_price"] == pytest.approx(0.10)
            async with session.get(url + "/v1/plans") as response:
                assert (await response.json())["plans"] == []
            async with (
                ClientSession() as anonymous,
                anonymous.get(url + "/v1/prices/inputs") as response,
            ):
                assert response.status == 401

    asyncio.run(scenario())
