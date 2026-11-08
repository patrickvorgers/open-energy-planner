"""Exercise real HA metadata events while excluding ordinary measurements from catalogue work."""

import asyncio
from datetime import timedelta
from types import MappingProxyType

from custom_components.open_energy_planner import catalogue
from custom_components.open_energy_planner.catalogue_discovery import CatalogueDiscovery
from tests.custom_component.support.chain import configure_energy, running_chain


def test_measurement_burst_does_not_read_metadata_or_publish_catalogue(monkeypatch):
    """A thousand value-only updates do no metadata capture, while one changed unit reads one entity."""

    async def scenario():
        """Use HA's actual indexed state events and retain all unrelated source records."""
        async with running_chain(live=True) as (hass, *_):
            attributes = {"unit_of_measurement": "W", "device_class": "power"}
            for index in range(1000):
                hass.states.async_set(f"sensor.synthetic_{index:04}", "1", attributes)
            discovery = CatalogueDiscovery(hass)
            discovery.configure(["sensor.synthetic_0999"])
            await discovery.start()
            try:
                initial = await discovery.capture()
                discovery.acknowledge(initial)
                assert {item["entity_id"] for item in initial["entities"]} == {
                    "sensor.synthetic_0999"
                }
                reads = []
                original = catalogue.catalogue_entity

                def read(hass, state, names):
                    """Count actual projected entities without replacing their production metadata."""
                    reads.append(state.entity_id)
                    return original(hass, state, names)

                monkeypatch.setattr(catalogue, "catalogue_entity", read)
                generation = discovery.generation
                for index in range(1000):
                    hass.states.async_set(f"sensor.synthetic_{index:04}", "2", attributes)
                await hass.async_block_till_done()
                assert await discovery.capture() is None
                assert reads == []
                assert discovery.generation == generation
                hass.states.async_set(
                    "sensor.synthetic_0001", "2", {**attributes, "unit_of_measurement": "kW"}
                )
                await hass.async_block_till_done()
                assert await discovery.capture() is None
                assert reads == []
                assert discovery.generation == generation
                hass.states.async_set(
                    "sensor.synthetic_0999", "2", {**attributes, "unit_of_measurement": "kW"}
                )
                await hass.async_block_till_done()
                changed = await discovery.capture()
                assert reads == ["sensor.synthetic_0999"]
                assert (
                    next(
                        item
                        for item in changed["entities"]
                        if item["entity_id"] == "sensor.synthetic_0999"
                    )["unit"]
                    == "kW"
                )
                assert (
                    next(
                        item
                        for item in initial["entities"]
                        if item["entity_id"] == "sensor.synthetic_0999"
                    )["unit"]
                    == "W"
                )
            finally:
                discovery.close()

    asyncio.run(scenario())


def test_generation_change_during_capture_requires_repair_and_close_detaches(monkeypatch):
    """A concurrent metadata event prevents acknowledging a mixed capture, and close stops future callbacks."""

    async def scenario():
        """Change a selected source at an awaited capture boundary and exercise the real event listener."""
        async with running_chain(live=True) as (hass, entry, *_):
            entry.runtime_data.poll_cancel()
            entry.runtime_data.poll_cancel = None
            hass.states.async_set("sensor.active", "1", {"unit_of_measurement": "W"})
            discovery = CatalogueDiscovery(hass)
            discovery.configure(["sensor.active"])
            original = catalogue.setup_catalogue

            async def racing_capture(hass, **kwargs):
                """Capture old metadata and then deliver a genuine change before returning it."""
                body = await original(hass, **kwargs)
                hass.states.async_set("sensor.active", "1", {"unit_of_measurement": "kW"})
                await hass.async_block_till_done()
                return body

            monkeypatch.setattr(catalogue, "setup_catalogue", racing_capture)
            try:
                assert await discovery.capture() is None
                assert not discovery.healthy
                monkeypatch.setattr(catalogue, "setup_catalogue", original)
                repaired = await discovery.capture()
                assert repaired["entities"][0]["unit"] == "kW"
                discovery.acknowledge(repaired)
                assert discovery.healthy
                discovery.close()
                generation = discovery.generation
                hass.states.async_set("sensor.active", "1", {"unit_of_measurement": "W"})
                await hass.async_block_till_done()
                assert discovery.generation == generation
                assert await discovery.capture(force=True) is None
            finally:
                discovery.close()

    asyncio.run(scenario())


def test_entities_without_registry_entries_are_added_and_removed():
    """HA domain lifecycle events keep standalone sources current without polling every entity."""

    async def scenario():
        """Create and remove an unregistered synthetic entity through actual HA state APIs."""
        async with running_chain(live=True) as (hass, *_):
            discovery = CatalogueDiscovery(hass)
            discovery.configure(["sensor.unregistered_source"])
            await discovery.start()
            try:
                discovery.acknowledge(await discovery.capture())
                hass.states.async_set(
                    "sensor.unregistered_source",
                    "5",
                    {"unit_of_measurement": "%", "device_class": "battery"},
                )
                await hass.async_block_till_done()
                added = await discovery.capture()
                assert any(
                    item["entity_id"] == "sensor.unregistered_source" for item in added["entities"]
                )
                discovery.acknowledge(added)
                hass.states.async_remove("sensor.unregistered_source")
                await hass.async_block_till_done()
                removed = await discovery.capture()
                assert (
                    next(
                        item
                        for item in removed["entities"]
                        if item["entity_id"] == "sensor.unregistered_source"
                    )["unit"]
                    == ""
                )
            finally:
                discovery.close()

    asyncio.run(scenario())


def test_device_core_and_recorder_events_update_only_their_projection(monkeypatch):
    """Device names, HA settings and history refresh independently without reading unrelated source metadata."""
    from homeassistant.components.recorder.const import EVENT_RECORDER_HOURLY_STATISTICS_GENERATED
    from homeassistant.core_config import EVENT_CORE_CONFIG_UPDATE
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.open_energy_planner import catalogue_discovery

    async def scenario():
        """Deliver real device-registry and bus events against a one-source active watch."""
        async with running_chain(live=True) as (hass, entry, *_):
            entry.runtime_data.poll_cancel()
            entry.runtime_data.poll_cancel = None
            device = dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={("fixture", "active-meter")},
                name="Synthetic meter",
            )
            source = er.async_get(hass).async_get_or_create(
                "sensor",
                "fixture",
                "active-meter",
                suggested_object_id="active_meter",
                config_entry=entry,
                device_id=device.id,
            )
            hass.states.async_set(source.entity_id, "1", {"unit_of_measurement": "W"})
            discovery = CatalogueDiscovery(hass)
            discovery.configure([source.entity_id])
            try:
                initial = await discovery.capture()
                discovery.acknowledge(initial)
                reads, histories = [], []
                original = catalogue.catalogue_entity

                def project(hass, state, names):
                    """Count metadata reads while using the actual device/name projection."""
                    reads.append(state.entity_id)
                    return original(hass, state, names)

                async def history(_hass, terms):
                    """Record the separate Recorder projection without requiring historical measurements."""
                    histories.append(terms)
                    return []

                monkeypatch.setattr(catalogue, "catalogue_entity", project)
                monkeypatch.setattr(catalogue_discovery, "setup_history", history)
                dr.async_get(hass).async_update_device(
                    device.id, name_by_user="Renamed synthetic meter"
                )
                await hass.async_block_till_done()
                renamed = await discovery.capture()
                assert reads == [source.entity_id]
                assert renamed["entities"][0]["device_name"] == "Renamed synthetic meter"
                discovery.acknowledge(renamed)
                reads.clear()
                hass.config.country = "BE"
                hass.bus.async_fire(EVENT_CORE_CONFIG_UPDATE)
                await hass.async_block_till_done()
                configured = await discovery.capture()
                assert configured["country"] == "BE"
                assert reads == []
                discovery.acknowledge(configured)
                hass.bus.async_fire(EVENT_RECORDER_HOURLY_STATISTICS_GENERATED)
                await hass.async_block_till_done()
                assert await discovery.capture() is None
                assert len(histories) == 1 and reads == []
                discovery.configure([])
                removed = await discovery.capture()
                assert removed["entities"] == []
            finally:
                discovery.close()

    asyncio.run(scenario())


def test_close_during_capture_cannot_reopen_state_watch(monkeypatch):
    """Unload during an awaited manager read does no late projection or listener registration."""
    from custom_components.open_energy_planner import catalogue_discovery

    async def scenario():
        """Pause the real capture boundary, close the owner and then release its pending read."""
        async with running_chain(live=True) as (hass, entry, *_):
            entry.runtime_data.poll_cancel()
            entry.runtime_data.poll_cancel = None
            hass.states.async_set("sensor.active", "1", {"unit_of_measurement": "W"})
            discovery = CatalogueDiscovery(hass)
            discovery.configure(["sensor.active"])
            discovery.acknowledge(await discovery.capture())
            entered, release = asyncio.Event(), asyncio.Event()
            original = catalogue_discovery.async_get_manager

            async def paused_manager(hass):
                """Retain genuine manager data while exposing a deterministic unload window."""
                entered.set()
                await release.wait()
                return await original(hass)

            def unexpected_projection(*_arguments):
                """Fail if a closed owner reads a new source projection."""
                raise AssertionError("Metadata was projected after close")

            monkeypatch.setattr(catalogue_discovery, "async_get_manager", paused_manager)
            monkeypatch.setattr(catalogue, "catalogue_entity", unexpected_projection)
            task = asyncio.create_task(discovery.capture(force=True))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                discovery.close()
            finally:
                release.set()
            assert await task is None
            assert discovery._state_cancel is None
            assert not discovery.healthy

    asyncio.run(scenario())


def test_elapsed_time_never_triggers_metadata_audit(monkeypatch):
    """Healthy watches need no metadata reads after fifteen minutes or a day, until explicitly refreshed."""

    async def scenario():
        """Use independently specified elapsed times and the actual complete capture owner."""
        from homeassistant.util import dt as dt_util

        async with running_chain(live=True) as (hass, *_):
            discovery = CatalogueDiscovery(hass)
            calls = []
            original = catalogue.setup_catalogue

            async def capture(hass, **kwargs):
                """Count full recovery captures while retaining real HA discovery."""
                calls.append(True)
                return await original(hass, **kwargs)

            monkeypatch.setattr(catalogue, "setup_catalogue", capture)
            try:
                now = dt_util.utcnow()
                initial = await discovery.capture(now=now)
                discovery.acknowledge(initial)
                assert await discovery.capture(now=now + timedelta(minutes=14)) is None
                assert len(calls) == 1
                assert await discovery.capture(now=now + timedelta(minutes=15)) is None
                assert await discovery.capture(now=now + timedelta(days=1)) is None
                assert len(calls) == 1
                assert discovery.healthy
                assert await discovery.capture(force=True, now=now + timedelta(days=1)) is not None
                assert len(calls) == 2
            finally:
                discovery.close()

    asyncio.run(scenario())


def test_energy_selected_forecast_entry_and_planes_refresh_without_full_capture(monkeypatch):
    """Selected Forecast.Solar options and planes refresh geometry without scanning all entities."""
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState

    async def scenario():
        """Use actual Energy preferences, config-entry events and subentry updates without provider calls."""
        async with running_chain(live=True, recorder=True) as (hass, oep_entry, *_):
            oep_entry.runtime_data.poll_cancel()
            oep_entry.runtime_data.poll_cancel = None
            entries = []
            for identity in ("selected-roof", "unrelated-roof"):
                configured = ConfigEntry(
                    data={},
                    options={"inverter_size": 4000},
                    domain="forecast_solar",
                    entry_id=identity,
                    source="user",
                    title="Synthetic roof",
                    unique_id=None,
                    version=2,
                    state=ConfigEntryState.NOT_LOADED,
                    minor_version=1,
                    discovery_keys=MappingProxyType({}),
                    subentries_data=[
                        {
                            "subentry_id": "plane",
                            "subentry_type": "plane",
                            "title": "Synthetic plane",
                            "unique_id": None,
                            "data": {"modules_power": 3410, "azimuth": 205, "declination": 45},
                        }
                    ],
                )
                hass.config_entries._entries[identity] = configured
                entries.append(configured)
            selected, unrelated = entries
            await configure_energy(
                hass,
                oep_entry,
                [
                    {
                        "type": "solar",
                        "stat_energy_from": "sensor.roof",
                        "config_entry_solar_forecast": [selected.entry_id],
                    }
                ],
            )
            discovery = CatalogueDiscovery(hass)
            try:
                initial = await discovery.capture()
                discovery.acknowledge(initial)

                async def unexpected_capture(_hass):
                    """Fail if a targeted provider event falls back to a complete metadata read."""
                    raise AssertionError("Provider update caused a full capture")

                monkeypatch.setattr(catalogue, "setup_catalogue", unexpected_capture)
                hass.config_entries.async_update_entry(unrelated, options={"inverter_size": 3000})
                await hass.async_block_till_done()
                assert await discovery.capture() is None
                hass.config_entries.async_update_entry(selected, options={"inverter_size": 3000})
                await hass.async_block_till_done()
                changed = await discovery.capture()
                provider = changed["suggestions"]["solar_configurations"][0]["providers"][0]
                assert provider["inverter_limit_w"] == 3000
                assert provider["panels"][0]["capacity_w"] == 3410
                discovery.acknowledge(changed)
                plane = selected.get_subentries_of_type("plane")[0]
                hass.config_entries.async_update_subentry(
                    selected, plane, data={"modules_power": 4560, "azimuth": 180, "declination": 30}
                )
                await hass.async_block_till_done()
                changed = await discovery.capture()
                provider = changed["suggestions"]["solar_configurations"][0]["providers"][0]
                assert provider["panels"][0]["capacity_w"] == 4560
                assert provider["panels"][0]["azimuth"] == 180
                assert provider["panels"][0]["tilt"] == 30
                assert (
                    initial["suggestions"]["solar_configurations"][0]["providers"][0]["panels"][0][
                        "capacity_w"
                    ]
                    == 3410
                )
            finally:
                discovery.close()

    asyncio.run(scenario())
