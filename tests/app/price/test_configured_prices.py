"""Configured prices start without credentials/area guesses and replace complete source services."""

import asyncio
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.prices.bootstrap import configured_price_factory
from open_energy_planner.prices.runtime import ConfiguredPrices
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.savings.support.scenarios import contract


def test_saved_contract_starts_and_changes_price_area_without_restart(tmp_path):
    """No contract opens no providers. Saving NL then BE switches resources and retains the financial barrier."""

    async def scenario():
        """Use the actual SQL contract pointer and replace only external provider composition."""
        events = []
        with closing(SqlPlanRepository(tmp_path)) as repository:
            store = ContractStore(SqlDocuments(repository.engine, "contracts"))

            def factory(area):
                """Record region lifetimes without cloud requests."""

                @asynccontextmanager
                async def opened(engine):
                    """Keep the previous service alive until a replacement has opened."""
                    assert engine is repository.engine
                    events.append(("open", area))

                    async def refresh(now):
                        """Accept a refresh without changing this independent source fixture."""
                        return False

                    try:
                        yield SimpleNamespace(
                            series=(area,),
                            version=area,
                            issues={},
                            refresh=refresh,
                            document=lambda now: {"market_area": area, "status": "ready"},
                        )
                    finally:
                        events.append(("close", area))

                return opened

            async with configured_price_factory(factory)(repository.engine) as prices:
                barrier = asyncio.Lock()
                prices.persistence_lock = barrier
                assert prices.document(datetime.now(UTC))["status"] == "not_configured"
                assert prices.series == () and events == []
                store.save(replace(contract(), market_area="NL"))
                assert await prices.refresh(datetime.now(UTC))
                assert prices.market_area == "NL" and prices.series == ("NL",)
                assert prices.service.persistence_lock is barrier
                store.save(replace(contract(), market_area="BE"))
                assert await prices.refresh(datetime.now(UTC))
                assert prices.market_area == "BE" and prices.series == ("BE",)
                assert prices.service.persistence_lock is barrier
                assert events == [("open", "NL"), ("open", "BE"), ("close", "NL")]
            assert events[-1] == ("close", "BE")

    asyncio.run(scenario())


def test_unreadable_saved_contract_keeps_app_available_without_provider_fallback(tmp_path):
    """A corrupt pointer remains repairable and visible. Startup never guesses NL or opens a provider."""

    async def scenario():
        """Exercise the actual durable pointer without altering its missing evidence."""
        with closing(SqlPlanRepository(tmp_path)) as repository:
            documents = SqlDocuments(repository.engine, "contracts")
            documents.write("current.json", "broken-reference")

            def forbidden(area):
                """Reject any attempt to select a provider from corrupt configuration."""
                pytest.fail(f"Unexpected provider for {area}")

            async with configured_price_factory(forbidden)(repository.engine) as prices:
                assert prices.document(datetime.now(UTC))["status"] == "unavailable"
                assert prices.issues["configuration"] == "contract_cache_unreadable"
                assert prices.series == ()
                assert not await prices.refresh(datetime.now(UTC))
            assert documents.read("current.json") == "broken-reference"

    asyncio.run(scenario())


def test_failed_area_replacement_keeps_old_resources_but_blocks_incompatible_prices():
    """A failed BE service cannot publish old NL prices. Retry commits BE before closing NL resources."""

    async def scenario():
        """Drive an explicit area change and one recoverable provider-opening failure."""
        area, fail = "NL", False
        closed = []

        async def read_area():
            """Return the current operator-selected area without persistence details."""
            return area, None

        @asynccontextmanager
        async def opened(selected):
            """Fail only the requested replacement before publishing its source service."""
            if fail:
                raise OSError("source unavailable")
            try:
                yield SimpleNamespace(series=(selected,), issues={})
            finally:
                closed.append(selected)

        prices = ConfiguredPrices(read_area, opened)
        try:
            assert await prices.configure()
            area, fail = "BE", True
            with pytest.raises(OSError, match="source unavailable"):
                await prices.refresh(datetime.now(UTC))
            assert prices.series == () and closed == []
            assert prices.issues["configuration"] == "market_area_reload_failed"
            fail = False
            assert await prices.configure()
            assert prices.series == ("BE",) and closed == ["NL"]
            area = None
            assert await prices.configure()
            assert prices.series == () and closed == ["NL", "BE"]
        finally:
            await prices.close()

    asyncio.run(scenario())


def test_cancelled_area_read_finishes_before_price_context_releases_engine(tmp_path, monkeypatch):
    """Repeated shutdown cancellation drains a blocked SQL read and never opens providers afterward."""
    from threading import Event

    from open_energy_planner.prices import bootstrap

    entered, release, finished = Event(), Event(), Event()
    original = bootstrap.ContractStore

    def blocked(documents):
        """Keep the real detached SQL read alive until the test explicitly releases its ownership barrier."""
        entered.set()
        assert release.wait(5)
        try:
            return original(documents)
        finally:
            finished.set()

    def forbidden(area):
        """A cancelled configuration read may not start provider resources."""
        pytest.fail(f"Unexpected provider for {area}")

    async def scenario():
        """Cancel twice while the worker holds its engine, then permit normal thread cleanup."""
        monkeypatch.setattr(bootstrap, "ContractStore", blocked)
        with closing(SqlPlanRepository(tmp_path)) as repository:

            async def open_prices():
                """Own the source context exactly as the application lifecycle does."""
                async with configured_price_factory(forbidden)(repository.engine):
                    pytest.fail("Cancelled startup may not yield a price service")

            task = asyncio.create_task(open_prices())
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not finished.is_set()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert finished.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("withdrawal_error", [False, True])
def test_event_withdrawal_failure_closes_replacement_and_allows_retry(
    tmp_path, monkeypatch, withdrawal_error
):
    """Broken activity bookkeeping cannot leak newly opened price resources or strand loading state."""
    from open_energy_planner.activities.events import Events
    from open_energy_planner.storage.adapters.activities import ActivityStore

    async def scenario():
        """Use actual event withdrawal with a stale request ID and observe both provider lifetimes."""
        area, closed = "NL", []

        async def read_area():
            """Return the current explicit market selection."""
            return area, None

        @asynccontextmanager
        async def opened(selected):
            """Own a synthetic provider with a deliberately stale outstanding request."""
            try:
                yield SimpleNamespace(
                    series=(selected,),
                    issues={},
                    activity_requests={"old": "absent"},
                    published=None,
                    forecast=None,
                    alternative=None,
                    extra_forecasts=(),
                )
            finally:
                closed.append(selected)

        repository = SqlPlanRepository(tmp_path)
        events = Events(ActivityStore(repository.engine))
        prices = ConfiguredPrices(read_area, opened)
        prices.events = events
        try:
            assert await prices.configure()
            area = "BE"
            if withdrawal_error:
                withdraw = prices.withdraw_events

                async def unavailable():
                    """Fail optional event withdrawal before the replacement is installed."""
                    raise RuntimeError("synthetic event bookkeeping failure")

                monkeypatch.setattr(prices, "withdraw_events", unavailable)
                with pytest.raises(RuntimeError, match="synthetic event"):
                    await prices.configure()
                assert closed == ["BE"] and prices.market_area == "NL"
                assert prices.configuration_issue == "market_area_reload_failed"
                monkeypatch.setattr(prices, "withdraw_events", withdraw)
            assert await prices.configure()
            assert prices.market_area == "BE" and prices.configuration_issue is None
            assert closed == (["BE", "NL"] if withdrawal_error else ["NL"])
        finally:
            prices.service.activity_requests.clear()
            await prices.close()
            await events.close()
            repository.close()

    asyncio.run(scenario())
