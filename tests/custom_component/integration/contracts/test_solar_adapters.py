"""Read real HA panel structures without provider requests, credentials or wiring assumptions."""

import asyncio
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace

import pytest

pytest.importorskip("homeassistant", reason="HA adapter contracts run in the pinned HA container")

from homeassistant.config_entries import ConfigSubentry

from custom_components.open_energy_planner.solar_adapters import (
    CONFIGURATION_ADAPTERS,
    solar_configuration_suggestion,
)

DISCOVERY_PAGE_BUDGET_BYTES = 192 * 1024


@pytest.mark.parametrize("unload_during_preparation", [False, True])
def test_catalogue_preparation_uses_worker_and_drains_before_unload(
    monkeypatch, unload_during_preparation
):
    """HA remains responsive during worker preparation, and unload prevents late sends and captures."""
    from threading import Event, get_ident

    from custom_components.open_energy_planner import catalogue_transfer
    from tests.custom_component.support.chain import running_chain

    async def scenario():
        """Coordinate worker and event-loop checkpoints without timing-based performance assertions."""
        async with running_chain(live=True) as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.poll_cancel()
            coordinator.poll_cancel = None
            loop = asyncio.get_running_loop()
            loop_thread = get_ident()
            entered, closing = asyncio.Event(), asyncio.Event()
            release, finished = Event(), Event()
            preparation_threads, prepared_results, sent, captures = [], [], [], []
            original_prepare = catalogue_transfer.prepare_catalogue
            original_request = coordinator.client.request
            original_capture = coordinator.solar_forecast_bridge.capture
            original_close = coordinator.close

            def prepare(body):
                """Pause only a worker so an incorrect event-loop caller fails without deadlocking HA."""
                preparation_threads.append(get_ident())
                if get_ident() == loop_thread:
                    entered.set()
                else:
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(timeout=10), "Test did not release catalogue worker"
                result = original_prepare(body)
                prepared_results.append(result)
                finished.set()
                return result

            async def request(method, path, body=None):
                """Retain actual authenticated exchanges and record sends after preparation."""
                if path in ("/v1/ha/catalogue", "/v1/solar/ha-forecasts"):
                    sent.append((path, coordinator.closed))
                return await original_request(method, path, body)

            async def capture(suggestions, now):
                """Keep real forecast capture while recording its accepted normalized context."""
                captures.append(suggestions)
                return await original_capture(suggestions, now)

            async def close():
                """Signal real unload and retain its active-synchronization drain."""
                closing.set()
                await original_close()

            monkeypatch.setattr(catalogue_transfer, "prepare_catalogue", prepare)
            monkeypatch.setattr(coordinator.client, "request", request)
            monkeypatch.setattr(coordinator.solar_forecast_bridge, "capture", capture)
            monkeypatch.setattr(coordinator, "close", close)
            coordinator.catalogue_sent = None
            synchronization = asyncio.create_task(coordinator.sync())
            tasks = [synchronization]
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert len(preparation_threads) == 1
                assert preparation_threads[0] != loop_thread
                checkpoint = asyncio.Event()
                loop.call_soon(checkpoint.set)
                await checkpoint.wait()
                assert not finished.is_set()
                assert sent == captures == []
                if unload_during_preparation:
                    unload = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
                    tasks.append(unload)
                    await asyncio.wait_for(closing.wait(), timeout=5)
                    assert coordinator.closed
                    assert not unload.done()
                    assert not finished.is_set()
            finally:
                release.set()
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            assert not any(isinstance(outcome, BaseException) for outcome in outcomes)
            assert finished.is_set()
            if unload_during_preparation:
                assert outcomes[-1] is True
                assert sent == captures == []
            else:
                assert any(path == "/v1/ha/catalogue" for path, _closed in sent)
                assert any(path == "/v1/solar/ha-forecasts" for path, _closed in sent)
                assert all(not closed for _path, closed in sent)
                assert captures == [
                    prepared_results[0].snapshot["suggestions"].get("solar_configurations", [])
                ]

    asyncio.run(scenario())


@pytest.mark.parametrize("paused_stage", ["catalogue", "forecast", "upload"])
def test_unload_drains_solar_work_without_reopening_listeners(monkeypatch, paused_stage):
    """Real HA unload waits for deferred work and releases listeners without starting a late forecast upload."""
    import logging

    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from homeassistant.util import dt as dt_util

    from tests.custom_component.support.chain import running_chain

    async def scenario():
        """Pause each asynchronous boundary and unload through the real config-entry lifecycle."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            coordinator.poll_cancel()
            coordinator.poll_cancel = None
            coordinator.catalogue_sent = None
            bridge = coordinator.solar_forecast_bridge
            provider_coordinator = DataUpdateCoordinator(
                hass, logging.getLogger(__name__), name="Fixture provider", config_entry=entry
            )
            provider_entry = SimpleNamespace(
                entry_id="fixture-provider",
                modified_at=dt_util.utcnow(),
                runtime_data=provider_coordinator,
            )
            entered, release = asyncio.Event(), asyncio.Event()
            bridge_closed = asyncio.Event()
            late_requests, captures = [], []
            original_request = coordinator.client.request
            original_close = bridge.close

            def close_bridge():
                """Record the teardown boundary independently of the integration unload result."""
                bridge_closed.set()
                original_close()

            async def request(method, path, body=None):
                """Delay catalogue or upload while recording any request started after closure."""
                if coordinator.closed:
                    late_requests.append(path)
                if path == "/v1/snapshot":
                    return {
                        **await original_request(method, path, body),
                        "source": "home_assistant",
                    }
                if path == {
                    "catalogue": "/v1/ha/catalogue",
                    "upload": "/v1/solar/ha-forecasts",
                }.get(paused_stage):
                    entered.set()
                    await release.wait()
                if path == "/v1/ha/catalogue":
                    return {"accepted": True}
                if path == "/v1/solar/ha-forecasts":
                    return {}
                return await original_request(method, path, body)

            async def capture(_suggestions, now):
                """Model a provider registering its listener after a deferred cache read."""
                captures.append(coordinator.closed)
                if paused_stage == "forecast":
                    entered.set()
                    await release.wait()
                bridge.watch(provider_entry)
                return {"observed_at": now.isoformat(), "groups": []}

            monkeypatch.setattr(coordinator.client, "request", request)
            monkeypatch.setattr(bridge, "capture", capture)
            monkeypatch.setattr(bridge, "close", close_bridge)
            synchronization = asyncio.create_task(coordinator.sync())
            await asyncio.wait_for(entered.wait(), timeout=5)
            queued_sync = asyncio.create_task(coordinator.sync())
            await asyncio.sleep(0)
            unload = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
            try:
                async with asyncio.timeout(5):
                    while not coordinator.closed:
                        await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not unload.done()
                assert not bridge_closed.is_set()
            finally:
                release.set()
                await asyncio.gather(synchronization, queued_sync, unload)
            assert unload.result() is True
            assert bridge_closed.is_set()
            assert not late_requests
            assert not any(captures)
            assert not bridge.watches
            assert not provider_coordinator._listeners

    asyncio.run(scenario())


@pytest.mark.parametrize("daily_total", [495, 600, None])
@pytest.mark.parametrize("timezone", ["UTC", "Pacific/Kiritimati"])
def test_raw_end_labelled_energy_preserves_full_and_partial_intervals(daily_total, timezone):
    """376 Wh is the 07:00-to-08:00 delta 465 minus 89, while the next 30 Wh occupies one quarter."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from custom_components.open_energy_planner.solar_forecasts import forecast_solar_prediction
    from open_energy_planner.forecast_quality.calculation.metrics import integral

    day_start = datetime(2026, 6, 20, tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    sunrise = datetime(2026, 6, 20, 6, 20, 17, tzinfo=UTC)
    raw = {
        sunrise.isoformat(): 0,
        "2026-06-20T07:00:00+00:00": 89,
        "2026-06-20T08:00:00+00:00": 376,
        "2026-06-20T08:15:00+00:00": 30,
    }
    watts = {datetime.fromisoformat(key): 100 for key in raw}
    watts[sunrise] = 0
    watts[datetime(2026, 6, 20, 8, 15, tzinfo=UTC)] = 0
    entry_value = SimpleNamespace(
        entry_id="fixture",
        runtime_data=SimpleNamespace(
            data=SimpleNamespace(
                timezone=timezone,
                watts=watts,
                wh_period={datetime.fromisoformat(key): value for key, value in raw.items()},
                wh_days={day_start: daily_total} if daily_total is not None else {},
            )
        ),
    )
    hass = SimpleNamespace()
    rows = asyncio.run(forecast_solar_prediction(hass, entry_value))
    by_start = {row["start"]: row for row in rows}
    assert by_start["2026-06-20T07:00:00+00:00"]["kwh"] == 0.376
    assert by_start["2026-06-20T08:00:00+00:00"] == {
        "start": "2026-06-20T08:00:00+00:00",
        "duration_h": 0.25,
        "kwh": 0.03,
    }
    assert by_start[sunrise.isoformat()]["kwh"] == 0.089
    assert by_start[sunrise.isoformat()]["duration_h"] == pytest.approx(2383 / 3600)
    total = integral(rows, day_start, day_start + timedelta(days=1))
    if daily_total == 495:
        assert total == pytest.approx(0.495)
    else:
        assert total is None
        assert set(by_start) == {
            sunrise.isoformat(),
            "2026-06-20T07:00:00+00:00",
            "2026-06-20T08:00:00+00:00",
        }


def test_naive_provider_daily_total_retains_western_timezone_night_coverage():
    """Isolate the regression in UTC without changing the HA test process timezone."""
    import os
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from tests.custom_component.integration.contracts.test_solar_adapters import "
                "_assert_naive_provider_daily_total_night_coverage; "
                "_assert_naive_provider_daily_total_night_coverage()"
            ),
        ],
        env={**os.environ, "TZ": "UTC", "PYTHONPATH": os.pathsep.join(sys.path)},
        check=True,
        timeout=30,
    )


def _assert_naive_provider_daily_total_night_coverage():
    """The real library parses a date-only daily key as local midnight without a timezone."""
    from datetime import timedelta

    from forecast_solar import Estimate

    from custom_components.open_energy_planner.solar_forecasts import forecast_solar_prediction
    from open_energy_planner.forecast_quality.calculation.metrics import integral

    estimate = Estimate.from_dict(
        {
            "result": {
                "watts": {
                    "2026-06-20T06:00:00-07:00": 0,
                    "2026-06-20T07:00:00-07:00": 400,
                    "2026-06-20T08:00:00-07:00": 0,
                },
                "watt_hours_period": {
                    "2026-06-20T06:00:00-07:00": 0,
                    "2026-06-20T07:00:00-07:00": 400,
                    "2026-06-20T08:00:00-07:00": 600,
                },
                "watt_hours_day": {"2026-06-20": 1000},
            },
            "message": {"ratelimit": {"limit": 12}, "info": {"timezone": "America/Los_Angeles"}},
        }
    )
    assert next(iter(estimate.wh_days)).tzinfo is None
    configured = SimpleNamespace(runtime_data=SimpleNamespace(data=estimate))
    rows = asyncio.run(forecast_solar_prediction(SimpleNamespace(), configured))
    day_start = datetime(2026, 6, 20, 7, tzinfo=UTC)
    assert integral(rows, day_start, day_start + timedelta(days=1)) == pytest.approx(1)


def test_selected_core_planes_reach_the_app_catalogue():
    """Real Energy associations preserve exact planes across the authenticated HA/app exchange."""
    from datetime import timedelta

    from aiohttp import ClientSession
    from forecast_solar import Estimate
    from homeassistant.components.forecast_solar.coordinator import (
        ForecastSolarDataUpdateCoordinator,
    )
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.util import dt as dt_util

    from tests.custom_component.support.chain import TOKEN, configure_energy, running_chain

    async def scenario():
        """Seed a loaded Forecast.Solar cache without making external provider requests."""
        async with running_chain(live=True, recorder=True) as (hass, oep_entry, url, _root):
            configured = ConfigEntry(
                data={"latitude": 52, "longitude": 6},
                options={"inverter_size": 12000},
                domain="forecast_solar",
                entry_id="eleven",
                source="user",
                title="Roof",
                unique_id=None,
                version=2,
                state=ConfigEntryState.LOADED,
                minor_version=1,
                discovery_keys=MappingProxyType({}),
                subentries_data=[
                    {
                        "subentry_id": "plane",
                        "subentry_type": "plane",
                        "title": "Roof plane",
                        "unique_id": None,
                        "data": {"modules_power": 3410, "azimuth": 205, "declination": 45},
                    }
                ],
            )
            hass.config_entries._entries[configured.entry_id] = configured
            hass.states.async_set(
                "sensor.roof",
                12,
                {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            )
            await configure_energy(
                hass,
                oep_entry,
                [
                    {
                        "type": "solar",
                        "stat_energy_from": "sensor.roof",
                        "config_entry_solar_forecast": ["eleven"],
                    }
                ],
            )
            async with (
                ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
                session.get(url + "/v1/setup") as response,
            ):
                assert response.status == 200
                result = await response.json()
            proposal = result["catalogue"]["data"]["suggestions"]["solar_configurations"][0]
            assert proposal["issue"] is None
            assert proposal["providers"][0]["panels"] == [
                {
                    "panel_id": "eleven.plane",
                    "name": "Roof",
                    "capacity_w": 3410,
                    "azimuth": 205,
                    "tilt": 45,
                }
            ]
            coordinator = ForecastSolarDataUpdateCoordinator(hass, configured)
            configured.runtime_data = coordinator
            day = dt_util.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            estimate = Estimate(
                watts={
                    day + timedelta(hours=hour): 400 if hour == 12 else 0
                    for hour in range(6, 19)
                    if hour != 7
                },
                wh_period={
                    day + timedelta(hours=hour): 400 if hour == 12 else 0
                    for hour in range(6, 19)
                    if hour != 7
                },
                wh_days={day: 400},
                api_rate_limit=12,
                api_timezone="UTC",
            )
            coordinator.async_set_updated_data(estimate)
            from custom_components.open_energy_planner.solar_forecasts import (
                forecast_solar_prediction,
            )

            assert await forecast_solar_prediction(hass, configured)
            bridge = oep_entry.runtime_data.solar_forecast_bridge
            initial = await bridge.capture([proposal], dt_util.utcnow())
            forwarded = initial["groups"][0]["providers"][0]
            assert forwarded["provider_refreshed_at"] is None
            assert forwarded["provider_issued_at"] is None
            steps = {row["start"]: row["kwh"] for row in forwarded["steps"]}
            assert steps[day.isoformat()] == 0
            assert steps[(day + timedelta(hours=11)).isoformat()] == 0.4
            assert steps[(day + timedelta(hours=12)).isoformat()] == 0
            assert (day + timedelta(hours=7)).isoformat() not in steps
            assert steps[(day + timedelta(hours=19)).isoformat()] == 0
            coordinator.async_set_updated_data(estimate)
            refreshed = await bridge.capture([proposal], dt_util.utcnow())
            assert refreshed["groups"][0]["providers"][0]["provider_refreshed_at"] is not None
            assert refreshed["groups"][0]["providers"][0]["provider_issued_at"] is None
            initial_revision = forwarded["configuration_revision"]
            assert (
                refreshed["groups"][0]["providers"][0]["configuration_revision"] == initial_revision
            )
            async with (
                ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
                session.post(url + "/v1/solar/ha-forecasts", json=refreshed) as response,
            ):
                assert response.status == 200, await response.text()
            hass.config_entries.async_update_entry(
                configured,
                options={
                    **configured.options,
                    "damping_morning": 0.4,
                    "api_key": "never-forward-config-key",
                },
            )
            assert (await bridge.capture([proposal], dt_util.utcnow()))["groups"] == []
            coordinator.async_set_updated_data(estimate)
            assert (await bridge.capture([proposal], dt_util.utcnow()))["groups"] == []
            replacement = ForecastSolarDataUpdateCoordinator(hass, configured)
            configured.runtime_data = replacement
            replacement.async_set_updated_data(estimate)
            discovered = await bridge.capture([proposal], dt_util.utcnow())
            assert discovered["groups"][0]["providers"][0]["provider_refreshed_at"] is None
            replacement.async_set_updated_data(estimate)
            changed = await bridge.capture([proposal], dt_util.utcnow())
            changed_provider = changed["groups"][0]["providers"][0]
            assert changed_provider["configuration_revision"] != initial_revision
            assert changed_provider["provider_refreshed_at"] is not None
            assert "never-forward-config-key" not in str(changed)
            async with (
                ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
                session.post(url + "/v1/solar/ha-forecasts", json=changed) as response,
            ):
                assert response.status == 200, await response.text()
            assert replacement._listeners
            bridge.close()
            assert not coordinator._listeners
            assert not replacement._listeners

    asyncio.run(scenario())


def entry(identity, *, domain="forecast_solar", capacity=3410, tilt=45, azimuth=205):
    """Use the actual Core subentry shape with unrelated credentials that must never leave HA."""
    plane = ConfigSubentry(
        data=MappingProxyType({"modules_power": capacity, "declination": tilt, "azimuth": azimuth}),
        subentry_id="plane_" + identity,
        subentry_type="plane",
        title="Not a geometry data source",
        unique_id=None,
    )
    return SimpleNamespace(
        domain=domain,
        entry_id=identity,
        title="Roof " + identity,
        modified_at=datetime(2026, 6, 20, tzinfo=UTC),
        options={"inverter_size": 12000, "api_key": "never-forward-this"},
        get_subentries_of_type=lambda kind: [plane] if kind == "plane" else [],
    )


def suggestion(*entries):
    """Read the explicit Energy association through the same HA config-entry lookup seam."""
    lookup = {item.entry_id: item for item in entries}
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lookup.get))
    return solar_configuration_suggestion(
        hass,
        {"stat_energy_from": "sensor.combined", "config_entry_solar_forecast": list(lookup)},
    )


def _solar_transfer_fixture(monkeypatch, overflow_bytes, captured):
    """Size valid provider geometry against an independent actual-wire boundary and later meter.

    Args:
        monkeypatch: Pin the capture clock and transfer identity for byte arithmetic.
        overflow_bytes: Bytes above the independently specified wrapped page budget.
            Negative values leave spare room. Zero fits exactly.
        captured: Aware UTC capture time used in the envelope and the patched clock.

    Returns:
        tuple: The catalogue body and the two provider entries backing its solar proposals.
    """
    import json

    from custom_components.open_energy_planner import catalogue_transfer
    from tests.ha_contract.support.metadata import setup_metadata

    class FixedClock(datetime):
        """Use a fixed six-digit capture timestamp for independent byte-boundary arithmetic."""

        @classmethod
        def now(cls, timezone):
            """Return the independently specified UTC capture time."""
            return captured.astimezone(timezone)

    monkeypatch.setattr(catalogue_transfer, "datetime", FixedClock)
    monkeypatch.setattr(catalogue_transfer, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    body = setup_metadata()
    body["entities"].extend(
        {
            "entity_id": f"sensor.synthetic_{index:04}",
            "name": f"Independent synthetic source {index}",
            "device_id": "",
            "unit": "W",
            "device_class": "power",
            "state_class": "measurement",
        }
        for index in range(10000)
    )
    body["entities"].append(
        {
            "entity_id": "sensor.combined",
            "name": "Solar",
            "device_id": "",
            "unit": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        }
    )
    body["suggestions"].update(solar=True, solar_sources=["sensor.combined"])
    body["suggestions"]["terms"].append({"entity_id": "sensor.combined", "sign": 1})
    body["entities"].append({**body["entities"][-1], "entity_id": "sensor.small"})
    body["suggestions"]["solar_sources"].append("sensor.small")
    body["suggestions"]["terms"].append({"entity_id": "sensor.small", "sign": 1})
    prefix_pages = list(catalogue_transfer.catalogue_pages(body))
    transfer_index = prefix_pages[-1]["transfer"]["index"]
    assert transfer_index >= 10
    planes = [
        SimpleNamespace(
            subentry_id=f"plane_{index}",
            title="é" if index == 0 else "",
            data={"modules_power": 1, "azimuth": 180, "declination": 30},
        )
        for index in range(700)
    ]
    provider = entry("synthetic")
    provider.get_subentries_of_type = lambda _kind: planes
    proposal = suggestion(provider)
    wrapper = {
        "transfer": {
            "id": "a" * 32,
            "revision": "b" * 64,
            "captured_at": captured.isoformat(),
            "index": transfer_index,
            "complete": False,
        },
        "path": ["suggestions", "solar_configurations"],
        "items": [proposal],
    }
    remaining = DISCOVERY_PAGE_BUDGET_BYTES + overflow_bytes - len(json.dumps(wrapper).encode())
    assert remaining > 0
    for plane, panel in zip(planes, proposal["providers"][0]["panels"], strict=True):
        added = min(200 - len(plane.title), remaining)
        plane.title += "x" * added
        panel["name"] = plane.title
        remaining -= added
    assert remaining == 0
    assert len(json.dumps(wrapper).encode()) == DISCOVERY_PAGE_BUDGET_BYTES + overflow_bytes
    assert len(json.dumps(proposal).encode()) < DISCOVERY_PAGE_BUDGET_BYTES
    assert suggestion(provider) == proposal
    other_provider = entry("other")
    later_proposal = {**suggestion(other_provider), "entity_id": "sensor.small"}
    body["suggestions"]["solar_configurations"] = [proposal, later_proposal]
    return body, (provider, other_provider)


@pytest.mark.parametrize("overflow_bytes", [-1, 0, 1])
def test_optional_solar_geometry_uses_actual_transfer_envelope(monkeypatch, overflow_bytes):
    """Only geometry exceeding the real wrapped page becomes incomplete, retaining every source."""
    import json
    from copy import deepcopy

    from custom_components.open_energy_planner import catalogue_transfer
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    captured = datetime(2026, 11, 7, 12, 0, 0, 123456, tzinfo=UTC)
    body, _providers = _solar_transfer_fixture(monkeypatch, overflow_bytes, captured)
    proposal, later_proposal = body["suggestions"]["solar_configurations"]
    original = deepcopy(body)
    pages = list(catalogue_transfer.catalogue_pages(body))
    assert all(len(json.dumps(page).encode()) <= DISCOVERY_PAGE_BUDGET_BYTES for page in pages)
    catalogue = HACatalogue()
    for page in pages[:-1]:
        assert catalogue.receive_page(json.loads(json.dumps(page)), captured) is None
        assert catalogue.value is None
    complete = catalogue.receive_page(json.loads(json.dumps(pages[-1])), captured)
    catalogue.receive(complete, captured)
    assert catalogue.value["entities"] == original["entities"]
    assert catalogue.value["suggestions"]["terms"] == original["suggestions"]["terms"]
    assert catalogue.value["suggestions"]["solar_sources"] == ["sensor.combined", "sensor.small"]
    received_groups = catalogue.value["suggestions"]["solar_configurations"]
    assert len(received_groups) == 2
    assert received_groups[1] == later_proposal
    received = received_groups[0]
    if overflow_bytes > 0:
        assert received == {
            "entity_id": "sensor.combined",
            "providers": [],
            "issue": "solar_configuration_incomplete",
        }
    else:
        assert received == proposal
        assert len(received["providers"][0]["panels"]) == 700
    assert body == original


@pytest.mark.parametrize("transfer_result", ["accepted", "accepted_large", "partial", "mixed"])
def test_forecast_capture_uses_only_accepted_normalized_catalogue(
    monkeypatch, transfer_result, request
):
    """Only accepted normalized geometry reaches real forecast capture and authenticated app parsing."""
    evidence_properties = request.node.user_properties
    import json
    import logging
    from copy import deepcopy

    from aiohttp import ClientSession
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from homeassistant.util import dt as dt_util

    from custom_components.open_energy_planner import catalogue as catalogue_module
    from custom_components.open_energy_planner.solar_forecasts import FORECAST_ADAPTERS
    from tests.custom_component.support.chain import TOKEN, running_chain

    async def scenario():
        """Exercise real Coordinator, bridge, HTTP catalogue publication and forecast association checks."""
        async with running_chain(
            live=True,
            peer_gateway=transfer_result == "accepted_large",
            fixture_devices=transfer_result != "accepted_large",
        ) as (
            hass,
            oep_entry,
            url,
            _root,
        ):
            coordinator = oep_entry.runtime_data
            coordinator.poll_cancel()
            coordinator.poll_cancel = None
            captured = dt_util.utcnow()
            body, providers = _solar_transfer_fixture(monkeypatch, 1, captured)
            if transfer_result == "accepted_large":
                template = body["entities"][-1]
                for index in range(1600):
                    entity_id = f"sensor.solar_meter_{index:04}"
                    body["entities"].append(
                        {
                            **template,
                            "entity_id": entity_id,
                            "name": f"Synthetic solar source {index:04}",
                        }
                    )
                    body["suggestions"]["solar_sources"].append(entity_id)
                    body["suggestions"]["terms"].append({"entity_id": entity_id, "sign": 1})
                    body["suggestions"]["solar_configurations"].append(
                        {
                            "entity_id": entity_id,
                            "providers": [],
                            "issue": "solar_configuration_not_available",
                        }
                    )
            original = deepcopy(body)
            lookup = {provider.entry_id: provider for provider in providers}
            original_get_entry = hass.config_entries.async_get_entry

            def get_entry(identity):
                """Keep actual OEP entries while supplying independent cached solar providers."""
                return lookup.get(identity) or original_get_entry(identity)

            monkeypatch.setattr(hass.config_entries, "async_get_entry", get_entry)
            for provider in providers:
                cache = DataUpdateCoordinator(
                    hass, logging.getLogger(__name__), name=provider.title, config_entry=oep_entry
                )
                cache.async_set_updated_data(SimpleNamespace())
                provider.runtime_data = cache

            async def catalogue(_hass):
                """Supply the same captured immutable metadata to the real transfer coordinator."""
                return body

            predicted_entries = []

            async def prediction(_hass, provider):
                """Return one independently specified quarter kWh without external provider access."""
                predicted_entries.append(provider.entry_id)
                return [{"start": captured.isoformat(), "duration_h": 0.25, "kwh": 0.25}]

            monkeypatch.setattr(catalogue_module, "setup_catalogue", catalogue)
            monkeypatch.setitem(FORECAST_ADAPTERS, "forecast_solar", prediction)
            captures, uploads, accepted_uploads = [], [], []
            accepted_devices = []
            wire_sizes = []
            catalogue_accepted = False
            original_capture = coordinator.solar_forecast_bridge.capture
            original_request = coordinator.client.request

            async def capture(suggestions, now):
                """Record the accepted context while retaining actual bridge selection and adapter checks."""
                assert catalogue_accepted
                captures.append(deepcopy(suggestions))
                return await original_capture(suggestions, now)

            async def request(method, path, payload=None):
                """Retain authentication and app validation, with explicit incomplete or mixed wire failure."""
                nonlocal catalogue_accepted
                if path == "/v1/ha/catalogue":
                    wire_sizes.append(len(json.dumps(payload).encode()))
                    assert wire_sizes[-1] <= 196608
                    transfer = payload.get("transfer", {})
                    if transfer_result == "partial" and transfer.get("complete"):
                        return {"accepted": False}
                    if transfer_result == "mixed" and transfer.get("index") == 1:
                        payload = {**payload, "transfer": {**transfer, "id": "different-snapshot"}}
                    result = await original_request(method, path, payload)
                    catalogue_accepted = result.get("accepted") is True
                    if catalogue_accepted:
                        accepted_devices.append(result)
                    return result
                if path == "/v1/solar/ha-forecasts":
                    uploads.append(deepcopy(payload))
                    result = await original_request(method, path, payload)
                    accepted_uploads.append(result)
                    return result
                return await original_request(method, path, payload)

            monkeypatch.setattr(coordinator.solar_forecast_bridge, "capture", capture)
            monkeypatch.setattr(coordinator.client, "request", request)
            async with ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as session:
                async with session.get(url + "/v1/setup") as response:
                    before = (await response.json())["catalogue"]["data"]
                coordinator.catalogue_sent = None
                await coordinator.sync()
                async with session.get(url + "/v1/setup") as response:
                    after = (await response.json())["catalogue"]["data"]
            if transfer_result in ("accepted", "accepted_large"):
                assert catalogue_accepted
                assert len(uploads) == 1
                assert accepted_uploads == [{"accepted": True}]
                assert len(captures) == 1
                assert captures[0] == after["suggestions"]["solar_configurations"]
                assert captures[0][0]["issue"] == "solar_configuration_incomplete"
                assert captures[0][0]["providers"] == []
                assert predicted_entries == ["other"]
                assert [group["entity_id"] for group in uploads[0]["groups"]] == ["sensor.small"]
                assert after["entities"] == original["entities"]
                assert after["suggestions"]["terms"] == original["suggestions"]["terms"]
                if transfer_result == "accepted_large":
                    assert coordinator.devices == accepted_devices[0]["devices"]
                    reply_bytes = len(json.dumps(accepted_devices[0]).encode())
                    assert 262144 < reply_bytes < 4194304
                    assert len(coordinator.devices) == 1602
                    evidence_properties.extend(
                        [
                            ("catalogue_reply_bytes", reply_bytes),
                            ("catalogue_devices", len(coordinator.devices)),
                            ("largest_source_page_bytes", max(wire_sizes)),
                        ]
                    )
                    from aiohttp import ClientResponseError

                    from tests.database.support.archive import list_documents, read_document

                    receipt_names = [
                        name
                        for name in await hass.async_add_executor_job(
                            list_documents, _root / "app", "solar"
                        )
                        if name.startswith("ha-receipt-")
                    ]
                    assert len(receipt_names) == 1
                    receipt_text = await hass.async_add_executor_job(
                        read_document, _root / "app", "solar", receipt_names[0]
                    )
                    receipt = json.loads(receipt_text)
                    assert receipt["entity_id"] == "sensor.small"
                    assert receipt["prediction"]["configuration"]["entry_id"] == "other"
                    saved_steps = receipt["prediction"]["steps"]
                    assert len(saved_steps) == 1
                    assert datetime.fromisoformat(saved_steps[0]["start"]) == captured
                    assert saved_steps[0]["duration_h"] == 0.25
                    assert saved_steps[0]["kwh"] == 0.25
                    for change in ("unknown_meter", "changed_provider"):
                        invalid = deepcopy(uploads[0])
                        if change == "unknown_meter":
                            invalid["groups"][0]["entity_id"] = "sensor.not_selected"
                        else:
                            invalid["groups"][0]["providers"][0]["configuration"]["entry_id"] = (
                                "changed-provider"
                            )
                        with pytest.raises(ClientResponseError) as rejected:
                            await coordinator.client.request(
                                "POST", "/v1/solar/ha-forecasts", invalid
                            )
                        assert rejected.value.status == 400
                    assert (
                        await hass.async_add_executor_job(
                            read_document, _root / "app", "solar", receipt_names[0]
                        )
                        == receipt_text
                    )
            else:
                assert not catalogue_accepted
                assert captures == uploads == predicted_entries == []
                assert after == before
            assert body == original

    asyncio.run(scenario())


@pytest.mark.parametrize("oversized_part", ["entity", "solar_identity", "header"])
def test_unrepresentable_catalogue_fails_before_emitting_pages(oversized_part):
    """Mandatory and fallback overflow retain the prior catalogue without sending partial pages."""
    from copy import deepcopy

    from custom_components.open_energy_planner.catalogue_transfer import catalogue_pages
    from open_energy_planner.home_assistant.catalogue import HACatalogue
    from tests.ha_contract.support.metadata import setup_metadata

    captured = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    previous = setup_metadata()
    catalogue.receive(previous, captured)
    body = deepcopy(previous)
    if oversized_part == "entity":
        body["entities"][0]["name"] = "x" * DISCOVERY_PAGE_BUDGET_BYTES
    elif oversized_part == "solar_identity":
        body["suggestions"]["solar_configurations"] = [
            {
                "entity_id": "sensor." + "x" * DISCOVERY_PAGE_BUDGET_BYTES,
                "providers": [],
                "issue": None,
            }
        ]
    else:
        body["timezone"] = "x" * DISCOVERY_PAGE_BUDGET_BYTES
    original = deepcopy(body)
    sent = []
    with pytest.raises(ValueError, match="Catalogue record exceeds the discovery page budget"):
        for page in catalogue_pages(body):
            sent.append(page)
            assert catalogue.receive_page(page, captured) is None
    assert sent == []
    assert catalogue.value == previous
    assert catalogue.updated == captured
    assert body == original


def test_forecast_solar_uses_exact_fields_and_retains_two_selected_groups():
    """Two explicitly selected entries retain their geometry and identities without summing converter limits."""
    result = suggestion(entry("eleven"), entry("ten", capacity=3100, azimuth=125, tilt=40))
    assert result["entity_id"] == "sensor.combined"
    assert result["issue"] is None
    assert result["providers"] == [
        {
            "adapter": "forecast_solar",
            "entry_id": "eleven",
            "panels": [
                {
                    "panel_id": "eleven.plane_eleven",
                    "name": "Roof eleven",
                    "capacity_w": 3410,
                    "azimuth": 205,
                    "tilt": 45,
                }
            ],
            "inverter_limit_w": 12000,
        },
        {
            "adapter": "forecast_solar",
            "entry_id": "ten",
            "panels": [
                {
                    "panel_id": "ten.plane_ten",
                    "name": "Roof ten",
                    "capacity_w": 3100,
                    "azimuth": 125,
                    "tilt": 40,
                }
            ],
            "inverter_limit_w": 12000,
        },
    ]


def test_multiple_planes_keep_their_own_titles_and_single_plane_falls_back_to_its_title():
    """A single entry uses its user title, while separate planes keep their own display identities."""
    configured = entry("roof")
    east = configured.get_subentries_of_type("plane")[0]
    west = ConfigSubentry(
        data=east.data,
        subentry_id="west",
        subentry_type="plane",
        title="West roof",
        unique_id=None,
    )
    configured.get_subentries_of_type = lambda kind: [east, west] if kind == "plane" else []
    panels = suggestion(configured)["providers"][0]["panels"]
    assert [panel["name"] for panel in panels] == ["Not a geometry data source", "West roof"]
    configured.get_subentries_of_type = lambda kind: [west] if kind == "plane" else []
    configured.title = ""
    assert suggestion(configured)["providers"][0]["panels"][0]["name"] == "West roof"


@pytest.mark.parametrize("invalid", [None, True, "3410", -1, float("nan"), 10**309])
def test_incomplete_group_never_imports_only_its_valid_subset(invalid):
    """One malformed selected plane leaves the entire group manual rather than undercounting panels."""
    result = suggestion(entry("valid"), entry("invalid", capacity=invalid))
    assert result["providers"] == []
    assert result["issue"] == "solar_configuration_incomplete"


@pytest.mark.parametrize("name", [42, "x" * 201])
def test_invalid_panel_name_marks_only_the_optional_solar_import_incomplete(name):
    """Unbounded or malformed titles cannot reject the whole HA catalogue refresh."""
    configured = entry("roof")
    configured.title = name
    result = suggestion(configured)
    assert result["providers"] == []
    assert result["issue"] == "solar_configuration_incomplete"


def test_maximum_length_panel_name_remains_available_for_import():
    """The adapter preserves a valid 200-character title instead of truncating it."""
    configured = entry("roof")
    configured.title = "x" * 200
    result = suggestion(configured)
    assert result["issue"] is None
    assert result["providers"][0]["panels"][0]["name"] == "x" * 200


def test_solcast_is_explicitly_not_a_supported_adapter():
    """An installed custom integration cannot be mistaken for supported Forecast.Solar metadata."""
    assert (
        suggestion(entry("solcast", domain="solcast_solar"))["issue"]
        == "solar_configuration_unsupported"
    )


@pytest.mark.parametrize(
    ("first_selection", "other_provider", "expected_issues"),
    [
        (("shared",), "unsupported", ("solar_configuration_ambiguous",) * 2),
        (("shared", "other"), "unsupported", ("solar_configuration_ambiguous",) * 2),
        (("shared", "other"), "invalid", ("solar_configuration_ambiguous",) * 2),
        (("other",), "unsupported", ("solar_configuration_unsupported", None)),
    ],
)
def test_raw_provider_assignments_remain_ambiguous_when_one_group_is_incomplete(
    first_selection, other_provider, expected_issues
):
    """An unsupported or malformed peer cannot hide a shared forecast, while independent valid meters remain importable."""
    from unittest.mock import AsyncMock, patch

    from custom_components.open_energy_planner.catalogue import energy_suggestions

    providers = {
        "shared": entry("shared"),
        "other": entry("other", capacity=None)
        if other_provider == "invalid"
        else entry("other", domain="solcast_solar"),
    }
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=providers.get))
    manager = SimpleNamespace(
        data={
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.import",
                    "stat_energy_to": "sensor.export",
                },
                {
                    "type": "solar",
                    "stat_energy_from": "sensor.first",
                    "config_entry_solar_forecast": list(first_selection),
                },
                {
                    "type": "solar",
                    "stat_energy_from": "sensor.second",
                    "config_entry_solar_forecast": ["shared"],
                },
            ]
        }
    )

    async def scenario():
        """Normalize genuine Energy associations before checking the independent import decisions."""
        with patch(
            "homeassistant.components.energy.data.async_get_manager",
            AsyncMock(return_value=manager),
        ):
            result = await energy_suggestions(hass)
        suggestions = result["solar_configurations"]
        assert [suggestion["issue"] for suggestion in suggestions] == list(expected_issues)
        for suggestion, issue in zip(suggestions, expected_issues, strict=True):
            if issue is not None:
                assert suggestion["providers"] == []
            else:
                assert suggestion["providers"][0]["entry_id"] == "shared"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("count", "duplicate", "status", "accepted"),
    [
        (1, False, "ready", 1),
        (2, True, "incomplete", 1),
        (17, False, "ready", 17),
    ],
)
def test_duplicate_and_large_meter_sets_leave_an_explicit_readable_catalogue(
    count, duplicate, status, accepted
):
    """Duplicate membership stays incomplete while every distinct meter survives a larger installation."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.util import dt as dt_util

    from custom_components.open_energy_planner.catalogue import setup_catalogue
    from open_energy_planner.home_assistant.catalogue import HACatalogue
    from tests.custom_component.support.chain import running_chain

    async def scenario():
        """Read duplicate or larger membership through real HA discovery and the app validator."""
        async with running_chain(live=True) as (hass, _entry, _url, _root):
            sources = [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.import",
                    "stat_energy_to": "sensor.export",
                    "stat_rate": "sensor.grid",
                }
            ]
            sources.extend(
                {"type": "solar", "stat_energy_from": f"sensor.roof{0 if duplicate else index}"}
                for index in range(count)
            )
            for entity in {
                "sensor.import",
                "sensor.export",
                *(source["stat_energy_from"] for source in sources),
            }:
                hass.states.async_set(
                    entity,
                    1,
                    {
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                )
            hass.states.async_set(
                "sensor.grid",
                0,
                {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
            )
            with patch(
                "homeassistant.components.energy.data.async_get_manager",
                AsyncMock(return_value=SimpleNamespace(data={"energy_sources": sources})),
            ):
                body = await setup_catalogue(hass)
            hints = body["suggestions"]
            assert hints["energy_status"] == status
            assert len(hints["solar_sources"]) == accepted
            assert [row["entity_id"] for row in hints["solar_configurations"]] == hints[
                "solar_sources"
            ]
            assert len(set(hints["solar_sources"])) == accepted
            HACatalogue().receive(body, dt_util.utcnow())

    asyncio.run(scenario())


def test_another_adapter_can_reuse_the_same_catalogue_contract(monkeypatch):
    """A future supported adapter is selected by domain without adding provider switches to the catalogue."""
    monkeypatch.setitem(
        CONFIGURATION_ADAPTERS,
        "future_provider",
        lambda configured: {
            "adapter": "future_provider",
            "entry_id": configured.entry_id,
            "panels": [
                {"panel_id": "future_plane", "capacity_w": 2000, "azimuth": 150, "tilt": 25}
            ],
            "inverter_limit_w": None,
        },
    )
    result = suggestion(entry("future", domain="future_provider"))
    assert result["issue"] is None
    assert result["providers"][0]["panels"][0]["azimuth"] == 150
    from homeassistant.util import dt as dt_util

    from custom_components.open_energy_planner.solar_forecasts import (
        FORECAST_ADAPTERS,
        SolarForecastBridge,
    )
    from open_energy_planner.solar.ha_forecasts import parse_ha_predictions

    configured = entry("future", domain="future_provider")
    configured.runtime_data = SimpleNamespace(
        data=object(),
        last_update_success=True,
        async_add_listener=lambda listener: lambda: None,
    )

    async def read_prediction(hass, selected):
        """A second adapter supplies the shared hourly kWh contract without a service-specific app branch."""
        assert selected is configured
        return [
            {
                "start": dt_util.utcnow().replace(minute=0, second=0, microsecond=0).isoformat(),
                "duration_h": 1,
                "kwh": 0.4,
            }
        ]

    monkeypatch.setitem(FORECAST_ADAPTERS, "future_provider", read_prediction)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=lambda identity: configured)
    )

    async def scenario():
        """Forward and parse the added provider through unchanged bridge and app contracts."""
        bridge = SolarForecastBridge(hass)
        body = await bridge.capture([result], dt_util.utcnow())
        parsed = parse_ha_predictions(body, [result], dt_util.utcnow())
        forecast = parsed["sensor.combined"][0]
        assert forecast.configuration.adapter == "future_provider"
        assert forecast.steps[0].kwh == 0.4
        bridge.close()

    asyncio.run(scenario())
