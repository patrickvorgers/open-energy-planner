"""Exercise real HA chooser requests, bounded transfers and promotion into active source watches."""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from aiohttp import ClientSession

from custom_components.open_energy_planner.client import AuthenticationError
from tests.custom_component.support.chain import TOKEN, configure_energy, running_chain


def test_large_scoped_queries_do_not_replace_energy_and_saved_car_becomes_active(monkeypatch):
    """A car at the tail of a paged query can be saved while unrelated sources stay outside active discovery."""

    async def scenario():
        """Use authenticated app HTTP, genuine HA states and the integration's actual transfer path."""
        async with running_chain(live=True, recorder=True, peer_gateway=True) as (
            hass,
            entry,
            url,
            *_,
        ):
            coordinator = entry.runtime_data
            coordinator.poll_cancel()
            coordinator.poll_cancel = None
            hass.states.async_set(
                "sensor.grid", "100", {"device_class": "power", "unit_of_measurement": "W"}
            )
            hass.states.async_set(
                "sensor.import",
                "10",
                {
                    "device_class": "energy",
                    "unit_of_measurement": "kWh",
                    "state_class": "total_increasing",
                },
            )
            for index in range(1500):
                hass.states.async_set(
                    f"sensor.candidate_{index:04}",
                    "50",
                    {"device_class": "battery", "unit_of_measurement": "%"},
                )
            hass.states.async_set(
                "sensor.zzz_zeekr",
                "60",
                {
                    "friendly_name": "Synthetic Zeekr battery",
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                },
            )
            hass.states.async_set(
                "water_heater.synthetic_boiler", "on", {"current_temperature": 50}
            )
            await configure_energy(
                hass,
                entry,
                [{"type": "grid", "stat_energy_from": "sensor.import", "stat_rate": "sensor.grid"}],
            )
            pages = []
            original_request = coordinator.client.request

            async def request(method, path, body=None):
                """Retain peer authorization and collect actual candidate wire sizes."""
                if path == "/v1/ha/source-choices":
                    pages.append(body)
                return await original_request(method, path, body)

            monkeypatch.setattr(coordinator.client, "request", request)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:
                async with session.get(url + "/v1/setup") as response:
                    before = (await response.json())["catalogue"]["data"]
                assert {row["entity_id"] for row in before["entities"]} == {
                    "sensor.grid",
                    "sensor.import",
                }
                for scope in ("ev", "thermal"):
                    async with session.post(
                        url + "/v1/ha/source-choices/" + scope, json={}
                    ) as response:
                        assert response.status == 200
                        assert (await response.json())["pending"]
                await coordinator.sync()
                async with session.get(url + "/v1/ha/source-choices/ev") as response:
                    result = await response.json()
                assert not result["pending"] and not result["failed"]
                assert any(
                    row["entity_id"] == "sensor.zzz_zeekr"
                    for row in result["catalogue"]["entities"]
                )
                async with session.get(url + "/v1/ha/source-choices/thermal") as response:
                    thermal = await response.json()
                assert any(
                    row["entity_id"] == "water_heater.synthetic_boiler"
                    for row in thermal["catalogue"]["entities"]
                )
                assert all(
                    row["entity_id"] != "sensor.zzz_zeekr"
                    for row in thermal["catalogue"]["entities"]
                )
                async with session.get(url + "/v1/setup") as response:
                    assert (await response.json())["catalogue"]["data"] == before
                assert sum(page["scope"] == "ev" for page in pages) > 1
                assert all(len(json.dumps(page).encode()) <= 262144 for page in pages)
                completed = len(pages)
                await coordinator.sync()
                assert len(pages) == completed
                async with session.get(url + "/v1/ev") as response:
                    current = await response.json()
                vehicle = {
                    "id": str(uuid4()),
                    "soc_entity": "sensor.zzz_zeekr",
                    "soc_timestamp_attribute": None,
                    "capacity_kwh": 60,
                    "maximum_w": 11000,
                    "target_soc": 80,
                    "departure_time": "07:00",
                }
                async with session.post(
                    url + "/v1/ev/configuration",
                    json={
                        "configuration": {"vehicles": [vehicle], "points": []},
                        "expected_version": current["configuration_version"],
                    },
                ) as response:
                    assert response.status == 200, await response.text()
                await coordinator.sync()
                async with session.get(url + "/v1/setup") as response:
                    active = (await response.json())["catalogue"]["data"]
                assert {row["entity_id"] for row in active["entities"]} == {
                    "sensor.grid",
                    "sensor.import",
                    "sensor.zzz_zeekr",
                }
            async with (
                ClientSession() as session,
                session.post(url + "/v1/ha/source-choices/ev", json={}) as response,
            ):
                assert response.status == 401

            with pytest.raises(AuthenticationError):
                await coordinator.client.request("POST", "/v1/ha/source-choices/ev", {})

    asyncio.run(scenario())


def test_forecast_refresh_and_watch_confirmation_do_not_require_metadata_capture(monkeypatch):
    """Independent forecast deadlines and provider generations work with an unchanged accepted source set."""
    from homeassistant.util import dt as dt_util

    from custom_components.open_energy_planner import catalogue

    async def scenario():
        """Use real app requests while observing forecast capture and rejecting any new metadata projection."""
        async with running_chain(live=True) as (_hass, entry, *_):
            coordinator = entry.runtime_data
            coordinator.poll_cancel()
            coordinator.poll_cancel = None
            await coordinator.sync()
            assert coordinator.catalogue_discovery.healthy
            captures, confirmations = [], []
            original_capture = coordinator.solar_forecast_bridge.capture
            original_request = coordinator.client.request

            async def capture(suggestions, now):
                """Retain the forecast adapter and count only actual scheduled captures."""
                captures.append(now)
                return await original_capture(suggestions, now)

            async def request(method, path, body=None):
                """Send confirmations through the real authenticated receiver."""
                if path == "/v1/ha/catalogue":
                    confirmations.append(body)
                return await original_request(method, path, body)

            def unexpected_projection(*_arguments):
                """Fail if a forecast or watch-confirmation deadline rebuilds source metadata."""
                raise AssertionError("Unchanged sources were reprojected")

            monkeypatch.setattr(coordinator.solar_forecast_bridge, "capture", capture)
            monkeypatch.setattr(coordinator.client, "request", request)
            monkeypatch.setattr(catalogue, "catalogue_entity", unexpected_projection)
            coordinator.solar_forecast_attempted_at = dt_util.utcnow() - timedelta(minutes=6)
            coordinator.catalogue_confirmed_at = dt_util.utcnow() - timedelta(minutes=2)
            await coordinator.sync()
            assert len(captures) == 1
            assert confirmations == [{"confirmed_revision": coordinator.catalogue_context.revision}]
            await coordinator.sync()
            assert len(captures) == 1
            coordinator.solar_forecast_bridge.generation += 1
            await coordinator.sync()
            assert len(captures) == 2
            assert coordinator.catalogue_discovery.healthy

    asyncio.run(scenario())
