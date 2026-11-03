"""Authenticated HA price reports reach the automatic route without granting settings writes."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, Fingerprint, web

from open_energy_planner.app import create_app
from open_energy_planner.discovery import certificate, connection_app
from open_energy_planner.home_assistant.market_prices import price_request
from open_energy_planner.pairing import PAIRING_KEY
from open_energy_planner.prices.adapters.home_assistant import AutomaticPublishedPrices
from open_energy_planner.prices.api import register as register_prices
from open_energy_planner.prices.runtime import ConfiguredPrices
from open_energy_planner.prices.service import PriceService
from tests.app.price.test_ha_prices import MemoryStore, response
from tests.ha_contract.support.connection import PEER, SECRET, TOKEN, server


def test_peer_discovery_report_updates_route_and_rejects_foreign_identity(tmp_path):
    """Peer reports bypass provider locks and validate identity without granting settings writes."""

    async def scenario():
        """Exercise both app authentication and the explicit peer gateway allowlist over HTTP."""
        source = AutomaticPublishedPrices("NL", AsyncMock())
        prices = PriceService("NL", source, None, MemoryStore())
        prices.save_preferences = AsyncMock()

        @asynccontextmanager
        async def factory(_engine):
            """Supply controlled prices without any upstream provider traffic."""
            yield prices

        app = create_app(tmp_path, TOKEN, source="home_assistant", price_factory=factory)
        pairing = app[PAIRING_KEY]
        context, pin = certificate(tmp_path, pairing.instance_id)
        pairing.fingerprint = pin
        pending = pairing.request(PEER)["request_id"]
        await pairing.approve(pending)
        token = pairing.claim({"request_id": pending, "secret": SECRET})["token"]
        async with server(app) as upstream:
            gateway = connection_app(pairing, TOKEN, upstream, upstream + "/setup")
            async with server(gateway, context) as url, ClientSession() as client:
                body = {
                    "market_area": "NL",
                    "currency": "EUR",
                    "available": True,
                    "reason": "waiting_for_publication",
                    "responses": [],
                }
                async with client.post(upstream + "/v1/ha/market-prices", json=body) as result:
                    assert result.status == 401
                headers = {"Authorization": "Bearer " + token}
                async with (
                    prices.lock,
                    asyncio.timeout(1),
                    client.post(
                        url + "/v1/ha/market-prices",
                        json=body,
                        headers=headers,
                        ssl=Fingerprint(bytes.fromhex(pin)),
                    ) as result,
                ):
                    assert result.status == 200
                async with client.get(
                    upstream + "/v1/prices", headers={"Authorization": "Bearer " + TOKEN}
                ) as result:
                    assert (await result.json())["published_source"] == {
                        "mode": "automatic",
                        "selected": "home_assistant_nord_pool",
                        "reason": "waiting_for_publication",
                    }
                async with client.post(
                    url + "/v1/ha/market-prices",
                    json={**body, "market_area": "BE"},
                    headers=headers,
                    ssl=Fingerprint(bytes.fromhex(pin)),
                ) as result:
                    assert result.status == 400
                assert source.document(datetime.now(UTC))["selected"] == "home_assistant_nord_pool"
                now = datetime.now(UTC)
                await prices.refresh(now)
                async with client.get(
                    url + "/v1/snapshot", headers=headers, ssl=Fingerprint(bytes.fromhex(pin))
                ) as result:
                    requested = (await result.json())["market_price_request"]
                    assert requested["market_area"] == "NL"
                    assert requested["days"]
                day = now.astimezone(ZoneInfo(prices.schedule.timezone)).date()
                body = {**body, "reason": "ready", "responses": [response(source, day, now)]}
                async with client.post(
                    url + "/v1/ha/market-prices",
                    json=body,
                    headers=headers,
                    ssl=Fingerprint(bytes.fromhex(pin)),
                ) as result:
                    assert result.status == 200
                await prices.refresh(now)
                assert prices.complete(day)
                assert prices.store.series[0].points[0].value == -0.05
                async with client.put(
                    upstream + "/v1/prices/preferences",
                    headers={"Authorization": "Bearer " + TOKEN},
                    json={
                        "preferences": [None, None, None],
                        "configuration_version": None,
                        "market_area": "NL",
                    },
                ) as result:
                    assert result.status == 200
                    assert (await result.json())["published_source"][
                        "selected"
                    ] == "home_assistant_nord_pool"
                async with client.put(
                    url + "/v1/prices/preferences",
                    json={},
                    headers=headers,
                    ssl=Fingerprint(bytes.fromhex(pin)),
                ) as result:
                    assert result.status == 403

    asyncio.run(scenario())


def test_unreadable_configuration_hides_retained_route_until_recovery():
    """Retained price resources cannot advertise or accept an active route while configuration is unreadable."""

    async def scenario():
        """Exercise the real configured runtime and HTTP projection through failure and recovery."""
        issue = None
        source = AutomaticPublishedPrices("NL", AsyncMock())
        service = PriceService("NL", source, None, MemoryStore())

        async def area():
            """Simulate a saved contract pointer that becomes temporarily unreadable."""
            return "NL", issue

        @asynccontextmanager
        async def opened(_area):
            """Retain the same service without provider traffic or replacement prices."""
            yield service

        runtime = ConfiguredPrices(area, opened)
        await runtime.configure()
        body = {
            "market_area": "NL",
            "currency": "EUR",
            "available": True,
            "reason": "ready",
            "responses": [],
        }
        source.receive(body, datetime.now(UTC))
        app = web.Application()
        register_prices(app, lambda: runtime, None, None, asyncio.Lock())
        try:
            async with server(app) as url, ClientSession() as client:
                async with client.get(url + "/v1/prices") as result:
                    assert (await result.json())["published_source"][
                        "selected"
                    ] == "home_assistant_nord_pool"
                issue = "contract_cache_unreadable"
                await runtime.configure()
                assert runtime.service is service
                async with client.get(url + "/v1/prices") as result:
                    document = await result.json()
                    assert document["status"] == "unavailable"
                    assert "published_source" not in document
                assert price_request(runtime, datetime.now(UTC)) is None
                async with client.post(url + "/v1/ha/market-prices", json=body) as result:
                    assert result.status == 409
                issue = None
                await runtime.configure()
                async with client.get(url + "/v1/prices") as result:
                    assert (await result.json())["published_source"][
                        "selected"
                    ] == "home_assistant_nord_pool"
        finally:
            await runtime.close()

    asyncio.run(scenario())
