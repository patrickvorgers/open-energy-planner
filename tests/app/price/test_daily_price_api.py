"""Authenticated whole-day reads remain independent of contracts and plans."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, web

from open_energy_planner.app import create_app
from open_energy_planner.prices.daily import DailyPrices
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from tests.app.price.test_prices import published
from tests.frontend.browser.helpers import TOKEN, discover


def test_daily_price_api_requires_authentication_and_bounded_aware_periods(tmp_path):
    """Real HTTP returns retained selections without a plan or contract, rejecting invalid periods safely."""

    async def scenario():
        """Exercise production routing and persistence with one independently generated final day."""
        now = datetime.now(UTC)
        day = now.astimezone(ZoneInfo("Europe/Amsterdam")).date()
        evidence = published(day, now)

        @asynccontextmanager
        async def factory(engine):
            """Provide offline source evidence without requesting any market provider."""
            documents = SqlDocuments(engine, "prices")
            service = PriceService("NL", None, None, PriceStore(documents))
            service.series = (evidence,)
            DailyPrices(documents).update(
                service.daily_timeline(now), "NL", "Europe/Amsterdam", now
            )
            yield service

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
                    json=discover(None, None, publish=False),
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
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
