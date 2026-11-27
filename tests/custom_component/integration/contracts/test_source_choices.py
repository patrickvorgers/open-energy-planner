"""Exercise real HA chooser requests, bounded transfers and promotion into active source watches."""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from aiohttp import ClientSession

from custom_components.open_energy_planner.client import AuthenticationError
from tests.custom_component.support.chain import TOKEN, configure_energy, running_chain


@pytest.mark.parametrize("scope", ["ev", "thermal"])
def test_source_choices_survive_an_observation_upload_timeout(monkeypatch, scope):
    """Vehicle and plug choices complete even when unrelated measurement upload fails."""

    async def scenario():
        """Use real HA discovery and app admission before a failing observation transport."""
        async with running_chain(live=True, peer_gateway=True) as (hass, entry, url, *_):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            hass.states.async_set("sensor.car_soc", "60", {"unit_of_measurement": "%"})
            hass.states.async_set("binary_sensor.car_plug", "on")
            hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
            hass.states.async_set(
                "sensor.tank_temperature",
                "50",
                {"device_class": "temperature", "unit_of_measurement": "°C"},
            )
            original_status = coordinator.client.plan_status
            revision = "fixture-measurements"

            async def measurement_config(requested_revision):
                """Supply a valid measurement binding separately from the genuine chooser request."""
                assert requested_revision == revision
                return {
                    "measurement_config": {
                        "revision": revision,
                        "bindings": [
                            {
                                "key": "grid.power",
                                "entity_id": "sensor.grid",
                                "quantity": "power",
                                "required": True,
                                "multiplier": 1,
                                "maximum_age_s": 300,
                                "timestamp_attribute": None,
                            }
                        ],
                    }
                }

            async def plan_status():
                """Advertise the test binding on the independent command status route."""
                return {
                    **await original_status(),
                    "config_revision": revision,
                    "measurements_required": True,
                }

            upload_failed = asyncio.Event()

            async def observations(_report):
                """Fail independently of the metadata path without real-time waiting."""
                upload_failed.set()
                raise TimeoutError

            monkeypatch.setattr(coordinator.client, "plan_status", plan_status)
            monkeypatch.setattr(coordinator.client, "measurement_config", measurement_config)
            monkeypatch.setattr(coordinator.client, "observations", observations)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:
                async with session.post(
                    url + "/v1/ha/source-choices/" + scope, json={}
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["pending"]
                await coordinator.sync()
                await asyncio.wait_for(upload_failed.wait(), timeout=3)
                assert coordinator.issue is None
                async with session.get(url + "/v1/ha/source-choices/" + scope) as response:
                    result = await response.json()
                assert not result["pending"] and not result["failed"]
                identities = {row["entity_id"] for row in result["catalogue"]["entities"]}
                assert (
                    {"sensor.car_soc", "binary_sensor.car_plug"}
                    if scope == "ev"
                    else {"sensor.tank_temperature"}
                ) <= identities

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", [{"plan_id": None}, {"config_revision": "changed"}])
def test_source_transfer_cannot_delay_known_command_invalidation(monkeypatch, changed):
    """Revoke a previously usable command before awaiting optional chooser publication."""

    async def scenario():
        """Observe real command state at the first authenticated source page upload."""
        async with running_chain(peer_gateway=True) as (hass, entry, url, *_):
            coordinator = entry.runtime_data
            async with asyncio.timeout(3):
                while coordinator.schedule.plan is None:
                    await asyncio.sleep(0.01)
            coordinator.plan_watch_task.cancel()
            await asyncio.gather(coordinator.plan_watch_task, return_exceptions=True)
            assert coordinator.schedule.plan is not None
            hass.states.async_set("sensor.car_soc", "60", {"unit_of_measurement": "%"})
            original = coordinator.client.request
            published = []
            metadata_started, allow_metadata = asyncio.Event(), asyncio.Event()

            async def request(method, path, body=None):
                """Keep a real chooser upload blocked while the separate plan status revokes authority."""
                if path == "/v1/ha/source-choices":
                    metadata_started.set()
                    await allow_metadata.wait()
                    published.append(body)
                data = await original(method, path, body)
                if path == "/v1/ha/requests":
                    data["source"] = "home_assistant"
                elif path == "/v1/ha/plan-status":
                    data.update(changed)
                return data

            monkeypatch.setattr(coordinator.client, "request", request)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:
                async with session.post(url + "/v1/ha/source-choices/ev", json={}) as response:
                    assert response.status == 200
                task = asyncio.create_task(coordinator.sync_services())
                try:
                    await asyncio.wait_for(metadata_started.wait(), 5)
                    async with asyncio.timeout(1):
                        await coordinator.sync_plan()
                    assert coordinator.schedule.plan is None
                    assert coordinator.projection.command is None
                finally:
                    allow_metadata.set()
                    await task
            assert published

    asyncio.run(scenario())


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
            coordinator.plan_watch_task.cancel()
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
                assert {row["entity_id"] for row in result["catalogue"]["entities"]} == {
                    "sensor.grid",
                    "sensor.import",
                    "sensor.zzz_zeekr",
                    *(f"sensor.candidate_{index:04}" for index in range(1500)),
                }
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
                assert current["catalogue"] == result["catalogue"]
                async with session.get(url + "/v1/setup") as response:
                    assert (await response.json())["catalogue"]["data"] == before
                async with session.get(url + "/v1/thermal") as response:
                    assert (await response.json())["catalogue"] == before
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
                    assert response.status == 200, "EV configuration request failed"
                async with session.get(url + "/v1/ev") as response:
                    just_saved = await response.json()
                assert just_saved["vehicles"][0]["name"] == "Synthetic Zeekr battery"
                assert just_saved["catalogue"] == result["catalogue"]
                async with session.get(url + "/v1/setup") as response:
                    assert (await response.json())["catalogue"]["data"] == before
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


def test_obsolete_installation_connection_cannot_consume_a_current_source_request(monkeypatch):
    """An old config entry at a reused address cannot mix pages with the current installation."""
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigEntry, ConfigEntryState

    from custom_components.open_energy_planner.client import LocalClient

    async def scenario():
        """Load two genuine HA entries against one peer and retain only its matching producer."""
        async with running_chain(live=True, recorder=True, peer_gateway=True) as (
            hass,
            entry,
            url,
            *_,
        ):
            current = entry.runtime_data
            current.plan_watch_task.cancel()
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
            await configure_energy(
                hass,
                entry,
                [{"type": "grid", "stat_energy_from": "sensor.import", "stat_rate": "sensor.grid"}],
            )
            for index in range(1000):
                hass.states.async_set(
                    f"sensor.car_{index:04}",
                    "50",
                    {"device_class": "battery", "unit_of_measurement": "%"},
                )
            hass.states.async_set("binary_sensor.garage_plug", "on")
            obsolete = ConfigEntry(
                data={
                    "url": current.client.url,
                    "token": current.client.token,
                    "fingerprint": current.client.fingerprint.fingerprint.hex(),
                },
                options={},
                domain="open_energy_planner",
                entry_id="synthetic-obsolete-installation",
                source="integration_discovery",
                title="Obsolete synthetic installation",
                unique_id=str(uuid4()),
                version=1,
                minor_version=1,
                state=ConfigEntryState.NOT_LOADED,
                discovery_keys=MappingProxyType({}),
                subentries_data=[],
            )
            hass.config_entries._entries[obsolete.entry_id] = obsolete
            stale_writes = []
            original_request = LocalClient.request

            async def request(client, method, path, body=None):
                """Observe attempted writes while retaining the actual authenticated transport."""
                if client is not current.client and method != "GET":
                    stale_writes.append(path)
                return await original_request(client, method, path, body)

            monkeypatch.setattr(LocalClient, "request", request)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:
                async with session.post(url + "/v1/ha/source-choices/ev", json={}) as response:
                    assert (await response.json())["pending"]
                assert await hass.config_entries.async_setup(obsolete.entry_id)
                old = obsolete.runtime_data
                old.plan_watch_task.cancel()
                assert old.issue == "installation_identity_changed"
                assert old.schedule.plan is None
                assert stale_writes == []
                async with session.get(url + "/v1/ha/source-choices/ev") as response:
                    assert (await response.json())["pending"]
                await asyncio.gather(old.sync(), current.sync())
                async with session.get(url + "/v1/ha/source-choices/ev") as response:
                    result = await response.json()
                assert not result["pending"] and not result["failed"]
                assert {"sensor.car_0999", "binary_sensor.garage_plug"} <= {
                    row["entity_id"] for row in result["catalogue"]["entities"]
                }
                assert stale_writes == []
                from custom_components.open_energy_planner.client import InstallationMismatchError

                with pytest.raises(InstallationMismatchError):
                    await old.client.request("PUT", "/v1/enabled", {"enabled": False})
                async with session.get(url + "/v1/snapshot") as response:
                    assert (await response.json())["enabled"] is True
            await hass.config_entries.async_unload(obsolete.entry_id)

    asyncio.run(scenario())


def test_failed_publication_retries_after_backoff_without_another_metadata_event(monkeypatch):
    """A failed forced publication retains repair work and resumes forecasts only after acceptance."""
    from types import SimpleNamespace

    from aiohttp import ClientConnectionError
    from homeassistant.util import dt as dt_util

    from custom_components.open_energy_planner import coordinator as coordinator_module

    async def scenario():
        """Drive real authenticated publication and confirmation at fixed backoff boundaries."""
        async with running_chain(live=True) as (_hass, entry, *_):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            await coordinator.sync()
            assert coordinator.catalogue_discovery.healthy
            clock = [dt_util.utcnow()]
            monkeypatch.setattr(
                coordinator_module, "dt_util", SimpleNamespace(utcnow=lambda: clock[0])
            )
            uploads, confirmations, forecasts = [], [], []
            original_request = coordinator.client.request
            original_capture = coordinator.solar_forecast_bridge.capture

            async def request(method, path, body=None):
                """Fail the first snapshot send, retaining the real receiver for retry and confirmation."""
                if path == "/v1/ha/catalogue":
                    if "confirmed_revision" in body:
                        confirmations.append(body)
                    else:
                        uploads.append(body)
                        if len(uploads) == 1:
                            raise ClientConnectionError("Synthetic publication interruption")
                return await original_request(method, path, body)

            async def capture(suggestions, now):
                """Count actual forecast capture only when acknowledged metadata authorizes it."""
                forecasts.append(now)
                return await original_capture(suggestions, now)

            monkeypatch.setattr(coordinator.client, "request", request)
            monkeypatch.setattr(coordinator.solar_forecast_bridge, "capture", capture)
            coordinator.catalogue_sent = None
            await coordinator.sync()
            assert len(uploads) == 1
            assert not coordinator.catalogue_discovery.healthy
            assert confirmations == forecasts == []
            clock[0] += timedelta(seconds=299)
            await coordinator.sync()
            assert len(uploads) == 1
            assert confirmations == forecasts == []
            clock[0] += timedelta(seconds=1)
            await coordinator.sync()
            assert len(uploads) == 2
            assert coordinator.catalogue_discovery.healthy
            assert len(forecasts) == 1
            clock[0] += timedelta(seconds=60)
            await coordinator.sync()
            assert len(uploads) == 2
            assert confirmations == [{"confirmed_revision": coordinator.catalogue_context.revision}]

    asyncio.run(scenario())


def test_forecast_refresh_and_watch_confirmation_do_not_require_metadata_capture(monkeypatch):
    """Independent forecast deadlines and provider generations work with an unchanged accepted source set."""
    from homeassistant.util import dt as dt_util

    from custom_components.open_energy_planner import catalogue

    async def scenario():
        """Use real app requests while observing forecast capture and rejecting any new metadata projection."""
        async with running_chain(live=True) as (_hass, entry, *_):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            await coordinator.sync()
            assert coordinator.catalogue_discovery.healthy
            captures, confirmations, uploads = [], [], []
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
                if path == "/v1/solar/ha-forecasts":
                    uploads.append(body)
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
            assert len(captures) == len(uploads) == 1
            assert confirmations == [{"confirmed_revision": coordinator.catalogue_context.revision}]
            await coordinator.sync()
            assert len(captures) == len(uploads) == 1
            coordinator.solar_forecast_bridge.generation += 1
            await coordinator.sync()
            assert len(captures) == len(uploads) == 2
            assert coordinator.catalogue_discovery.healthy

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["page", "failure_report"])
def test_source_identity_failure_immediately_withdraws_commands(monkeypatch, failure):
    """Both source publication and failure reporting preserve a fatal installation mismatch."""
    from custom_components.open_energy_planner.client import InstallationMismatchError

    async def scenario():
        """Retain real snapshots and an active plan while one transfer stage reports the mismatch."""
        async with running_chain() as (_hass, entry, url, *_):
            coordinator = entry.runtime_data
            coordinator.plan_watch_task.cancel()
            assert coordinator.schedule.plan is not None
            original_snapshot = coordinator.client.service_requests
            original_request = coordinator.client.request
            attempts = []

            async def snapshot():
                """Include the genuine pending chooser request in an otherwise unchanged snapshot."""
                return {**await original_snapshot(), "source": "home_assistant"}

            async def request(method, path, body=None):
                """Inject an identity failure at publication or after an ordinary page failure."""
                if path == "/v1/ha/source-choices":
                    reporting_failure = body.get("failed") is True
                    attempts.append(reporting_failure)
                    if failure == "page" or reporting_failure:
                        raise InstallationMismatchError("Synthetic source installation mismatch")
                    raise ValueError("Synthetic interrupted source page")
                return await original_request(method, path, body)

            monkeypatch.setattr(coordinator.client, "service_requests", snapshot)
            monkeypatch.setattr(coordinator.client, "request", request)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:
                async with session.post(url + "/v1/ha/source-choices/ev", json={}) as response:
                    assert (await response.json())["pending"]
                await coordinator.sync()
            assert attempts == ([False] if failure == "page" else [False, True])
            assert coordinator.issue == "installation_identity_changed"
            assert coordinator.schedule.plan is None
            assert coordinator.projection.command is None

    asyncio.run(scenario())
