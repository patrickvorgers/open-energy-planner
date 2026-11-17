"""Authenticated whole-day reads remain independent of contracts and plans."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from open_energy_planner.prices.daily import DailyPrices
from open_energy_planner.prices.model import PriceKind
from open_energy_planner.prices.runtime import ConfiguredPrices
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from tests.app.price.test_prices import published
from tests.ha_contract.support.metadata import setup_metadata

TOKEN = "local-daily-price-test-0123456789abcdef"


def test_daily_price_api_requires_authentication_and_bounded_aware_periods(tmp_path, monkeypatch):
    """Real HTTP rejects invalid inputs/configuration and withdraws uncovered future rankings while retaining final days."""
    monkeypatch.setattr("open_energy_planner.app.PRICE_SCHEDULE_POLL_SECONDS", 0.01)

    async def scenario():
        """Exercise production routing and persistence with one independently generated final day."""
        now = datetime.now(UTC)
        day = now.astimezone(ZoneInfo("Europe/Amsterdam")).date()
        evidence = published(day, now)
        future_published = published(day + timedelta(days=1), now)
        forecast = replace(
            future_published,
            source_id="forecast_test",
            expires_at=now + timedelta(hours=6),
            points=tuple(
                replace(point, kind=PriceKind.FORECAST) for point in future_published.points
            ),
        )
        configuration_issue = None
        configured_prices = None

        @asynccontextmanager
        async def factory(engine):
            """Provide offline source evidence without requesting any market provider."""
            nonlocal configured_prices

            async def read_area():
                """Expose a controllable admission issue while retaining the last service."""
                return "NL", configuration_issue

            @asynccontextmanager
            async def opened(_area):
                """Provide a real price service and retained final-day evidence without HTTP sources."""
                documents = SqlDocuments(engine, "prices")
                service = PriceService("NL", None, None, PriceStore(documents))
                service.series = (evidence, forecast)
                DailyPrices(documents).update(
                    service.daily_timeline(now), "NL", "Europe/Amsterdam", now
                )
                yield service

            configured_prices = ConfiguredPrices(read_area, opened)
            try:
                await configured_prices.configure()
                yield configured_prices
            finally:
                await configured_prices.close()

        app = create_app(tmp_path, TOKEN, source="home_assistant", price_factory=factory)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as session:
                async with session.get(url + "/v1/prices/daily") as response:
                    assert response.status == 401
                headers = {"Authorization": "Bearer " + TOKEN}
                async with session.post(
                    url + "/v1/ha/catalogue",
                    json={**setup_metadata(), "timezone": "Europe/Amsterdam"},
                    headers=headers,
                ) as response:
                    assert response.status == 200
                params = {
                    "start": evidence.points[0].start.isoformat(),
                    "end": evidence.points[-1].end.isoformat(),
                }
                async with session.get(
                    url + "/v1/prices/daily", params=params, headers=headers
                ) as response:
                    assert response.status == 200
                    document = await response.json()
                assert document["days"][0]["status"] == "final"
                assert document["days"][0]["cheapest"] == [
                    point.start.isoformat() for point in evidence.points[:12]
                ]
                for invalid in (
                    {"start": "2026-11-17T00:00:00", "end": "2026-11-18T00:00:00Z"},
                    {"start": "private-query-sentinel"},
                    {"start": "2026-11-17T00:00:00Z", "end": "2026-11-22T00:00:00Z"},
                    {"start": params["end"], "end": params["start"]},
                ):
                    async with session.get(
                        url + "/v1/prices/daily", params=invalid, headers=headers
                    ) as response:
                        assert response.status == 400
                        assert "private-query-sentinel" not in await response.text()
                future_params = {
                    "start": forecast.points[0].start.isoformat(),
                    "end": forecast.points[-1].end.isoformat(),
                }
                async with session.get(
                    url + "/v1/prices/daily", params=future_params, headers=headers
                ) as response:
                    assert (await response.json())["days"][0]["status"] == "provisional"
                configured_prices.service.series = ()
                async with asyncio.timeout(5):
                    while True:
                        async with session.get(
                            url + "/v1/prices/daily", params=future_params, headers=headers
                        ) as response:
                            future = (await response.json())["days"][0]
                        if future["status"] == "incomplete":
                            break
                        await asyncio.sleep(0.01)
                assert future["quarters"] == future["cheapest"] == future["most_expensive"] == []
                async with session.get(
                    url + "/v1/prices/daily", params=params, headers=headers
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["days"] == document["days"]
                configuration_issue = "contract_cache_unreadable"
                await configured_prices.configure()
                assert (
                    configured_prices.market_area == "NL" and configured_prices.service is not None
                )
                async with session.get(
                    url + "/v1/prices/daily", params=params, headers=headers
                ) as response:
                    assert response.status == 200
                    assert await response.json() == {
                        "days": [],
                        "issue": "daily_prices_unavailable",
                    }
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_daily_refresh_withdraws_forecast_that_expires_during_provider_wait(tmp_path, monkeypatch):
    """A provider wait crossing expiry cannot preserve future provisional rankings with an old clock."""
    current = datetime(2026, 11, 17, 12, tzinfo=UTC)

    class Clock(datetime):
        """Advance app wall time deterministically without changing source delivery times."""

        @classmethod
        def now(cls, zone=None):
            """Return the scenario clock in the requested timezone."""
            return current.astimezone(zone)

    monkeypatch.setattr("open_energy_planner.app.datetime", Clock)
    monkeypatch.setattr("open_energy_planner.app.PRICE_SCHEDULE_POLL_SECONDS", 3600)

    async def scenario():
        """Run the actual refresh lifecycle once across expiry and inspect its durable result."""
        nonlocal current
        started, release = asyncio.Event(), asyncio.Event()
        daily = None
        published_day = published(current.date() + timedelta(days=1), current)
        forecast = replace(
            published_day,
            expires_at=current + timedelta(minutes=5),
            points=tuple(
                replace(
                    point,
                    start=point.start + timedelta(hours=1),
                    end=point.end + timedelta(hours=1),
                    kind=PriceKind.FORECAST,
                )
                for point in published_day.points
            ),
        )
        start, end = forecast.points[0].start, forecast.points[-1].end

        @asynccontextmanager
        async def factory(engine):
            """Retain tomorrow's provisional day before an offline provider wait crosses expiry."""
            nonlocal daily
            documents = SqlDocuments(engine, "prices")
            service = PriceService("NL", None, None, PriceStore(documents))
            service.series = (forecast,)
            daily = DailyPrices(documents)
            daily.update(service.daily_timeline(current), "NL", "UTC", current)

            async def refresh(_now):
                """Hold the original refresh clock while the provider is awaiting a response."""
                started.set()
                await release.wait()

            monkeypatch.setattr(service, "refresh", refresh)
            yield service

        runner = web.AppRunner(create_app(tmp_path, TOKEN, price_factory=factory))
        await runner.setup()
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            assert daily.read("NL", "UTC", start, end)[0]["status"] == "provisional"
            current = forecast.expires_at + timedelta(minutes=1)
            release.set()
            async with asyncio.timeout(3):
                while daily.read("NL", "UTC", start, end)[0]["status"] != "incomplete":
                    await asyncio.sleep(0.01)
            result = daily.read("NL", "UTC", start, end)[0]
            assert result["quarters"] == result["cheapest"] == result["most_expensive"] == []
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
