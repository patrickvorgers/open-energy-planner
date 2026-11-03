"""Public HA price actions obey discovery, availability and independent task ownership."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from aiohttp import ClientResponseError
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_registry as er

from custom_components.open_energy_planner.market_prices import MarketPriceBridge
from tests.custom_component.support.chain import running_chain


def test_discovery_availability_retry_and_transfer_ownership(tmp_path):
    """Only the eligible official entry is queried after availability, with retries and cached transfers."""

    async def scenario():
        """Run a real HA service registry with a controlled Nord Pool response and no external HTTP."""
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await bootstrap.async_load_base_functionality(hass)
        now = datetime(2026, 11, 1, 12, 10, tzinfo=UTC)
        request = {
            "market_area": "NL",
            "currency": "EUR",
            "days": [{"date": "2026-11-02", "request_id": "a" * 32}],
        }
        entry = SimpleNamespace(
            data={"areas": ["NL"], "currency": "EUR"},
            state=ConfigEntryState.LOADED,
            entry_id="configured-entry",
        )
        entries = []
        hass.config_entries = SimpleNamespace(async_entries=lambda domain: entries)
        calls = []

        async def prices(call):
            """Return one labelled fixture payload and record the public action parameters."""
            calls.append(call.data)
            return {"NL": [{"price": -50}]}

        hass.services.async_register(
            "nordpool", "get_prices_for_date", prices, supports_response=SupportsResponse.ONLY
        )
        bridge = MarketPriceBridge(hass)
        assert (await bridge.capture(request, now))["reason"] == "not_configured"
        entries.append(entry)
        registry = er.async_get(hass)
        sensor = registry.async_get_or_create(
            "binary_sensor", "nordpool", "NL-tomorrow_price_available"
        )
        hass.states.async_set(sensor.entity_id, "off")
        assert (await bridge.capture(request, now))["reason"] == "waiting_for_publication"
        assert calls == []
        hass.states.async_set(sensor.entity_id, "on")
        first = await bridge.capture(request, now)
        assert first["responses"][0]["request_id"] == "a" * 32
        assert calls == [
            {
                "config_entry": "configured-entry",
                "date": "2026-11-02",
                "areas": ["NL"],
                "currency": "EUR",
            }
        ]
        await bridge.capture(request, now + timedelta(minutes=1))
        assert len(calls) == 1
        capture = bridge.capture
        bridge.capture = AsyncMock(return_value=first)
        client = SimpleNamespace(request=AsyncMock(side_effect=TimeoutError))
        await bridge.transfer(request, client)
        assert bridge.replies
        client.request.side_effect = None
        await bridge.transfer(request, client)
        assert bridge.replies == {}
        for status in (400, 409, 413):
            bridge.replies = {now.date() + timedelta(days=1): first["responses"][0]}
            client.request.side_effect = ClientResponseError(None, (), status=status)
            await bridge.transfer(request, client)
            assert bridge.replies == {}
        bridge.capture = capture
        assert (await bridge.capture(request, now + timedelta(minutes=2)))["responses"] == []
        assert len(calls) == 1
        await bridge.capture(request, now + timedelta(minutes=30))
        assert len(calls) == 2
        entries.append(entry)
        assert (await bridge.capture(request, now))["reason"] == "ambiguous"
        entries.pop()
        entry.state = ConfigEntryState.NOT_LOADED
        assert (await bridge.capture(request, now))["reason"] == "not_loaded"
        entry.data["currency"] = "SEK"
        assert (await bridge.capture(request, now))["reason"] == "incompatible"
        await bridge.close()
        await hass.async_stop(force=True)

    asyncio.run(scenario())


def test_missing_diagnostic_uses_market_clock_and_close_cancels_work(tmp_path):
    """Fallback time follows CET/CEST rather than HA timezone and provider work is owned at unload."""

    async def scenario():
        """Check the 14:05 boundary and cancel a blocked optional transfer."""
        hass = HomeAssistant(str(tmp_path))
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await bootstrap.async_load_base_functionality(hass)
        entry = SimpleNamespace(
            data={"areas": ["NL"], "currency": "EUR"},
            state=ConfigEntryState.LOADED,
            entry_id="entry",
        )
        hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
        calls = []

        async def prices(call):
            """Count requests without reaching Nord Pool."""
            calls.append(call.data)
            return {}

        hass.services.async_register(
            "nordpool", "get_prices_for_date", prices, supports_response=SupportsResponse.ONLY
        )
        request = {
            "market_area": "NL",
            "currency": "EUR",
            "days": [{"date": "2026-11-02", "request_id": "b" * 32}],
        }
        bridge = MarketPriceBridge(hass)
        await bridge.capture(request, datetime(2026, 11, 1, 13, 4, tzinfo=UTC))
        assert calls == []
        await bridge.capture(request, datetime(2026, 11, 1, 13, 5, tzinfo=UTC))
        assert len(calls) == 1

        async def blocked(*_args):
            """Hold the optional transfer until integration unload cancels it."""
            await asyncio.Event().wait()

        client = SimpleNamespace(request=AsyncMock(side_effect=blocked))
        bridge.start({**request, "days": []}, client)
        task = bridge.task
        bridge.start(request, client)
        assert bridge.task is task
        await bridge.close()
        assert task.done()
        bridge.start(request, client)
        assert bridge.task is task
        await hass.async_stop(force=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_module", ["ev", "measurements"])
def test_module_failure_cannot_suppress_independent_market_price_work(monkeypatch, failed_module):
    """Broken measurement or EV projections cannot suppress the independent price exchange."""
    from custom_components.open_energy_planner import coordinator as coordinator_module
    from custom_components.open_energy_planner import ev

    async def scenario():
        """Run real HA synchronization and inject only the domain fault and price-start observer."""
        async with running_chain() as (_hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.poll_cancel()
            coordinator.poll_cancel = None
            snapshot = coordinator.client.snapshot
            request = {"market_area": "NL", "currency": "EUR", "days": []}

            async def with_price_request():
                """Add provider work to the real authenticated app snapshot."""
                data = {**await snapshot(), "market_price_request": request}
                if failed_module == "measurements":
                    data["measurement_config"] = {}
                return data

            monkeypatch.setattr(coordinator.client, "snapshot", with_price_request)
            broken = Mock(side_effect=ValueError("Invalid fixture module projection"))
            if failed_module == "ev":
                monkeypatch.setattr(ev, "receive", broken)
            else:
                monkeypatch.setattr(coordinator_module, "ObservationBridge", broken)
            started = Mock()
            monkeypatch.setattr(coordinator.market_prices, "start", started)
            await coordinator.sync()
            broken.assert_called_once()
            started.assert_called_once_with(request, coordinator.client)

    asyncio.run(scenario())
