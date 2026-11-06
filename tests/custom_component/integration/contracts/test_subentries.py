"""Exercise native HA planning groups and stable registry ownership on Core 2026.10."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant", reason="Native subentries need the pinned real HA image")

from aiohttp import ClientError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

from tests.custom_component.support.chain import running_chain


def populate_groups(coordinator):
    """Provide synthetic accepted catalogues with misleading names and multiple devices per role."""
    coordinator.devices = [
        {"id": "store", "kind": "battery", "name": "Solar", "configured": False},
        {"id": "east", "kind": "solar", "name": "Battery", "configured": False},
        {"id": "west", "kind": "solar", "name": "EV charging", "configured": False},
    ]
    coordinator.consumer_forecasts = {
        "options": [
            {"entity_id": "sensor.cycle_energy", "name": "Cycle appliance"},
            {"entity_id": "sensor.heat_energy", "name": "Weather-sensitive consumption"},
        ],
        "selected": [
            {"entity_id": "sensor.cycle_energy", "weather_sensitive": False},
            {"entity_id": "sensor.heat_energy", "weather_sensitive": True},
        ],
    }
    coordinator.appliances = {"devices": [{"entity_id": "sensor.cycle_energy", "shiftable": True}]}
    coordinator.ev = {
        "vehicles": [
            {
                "id": "car",
                "name": "Vehicle",
                "departure_time": "07:00",
                "next_departure": "2026-11-06T07:00:00+00:00",
                "target_soc": 80,
                "soc_entity": "sensor.car_soc",
                "capacity_kwh": 50,
                "maximum_w": 7000,
            }
        ],
        "points": [
            {
                "id": "charger",
                "name": "Charge point",
                "control": "read_only",
                "vehicle_id": "car",
                "vehicle_ids": ["car"],
                "power_entity": "sensor.charger_power",
                "energy_entity": "sensor.charger_energy",
                "connected_entity": "binary_sensor.charger_connected",
                "minimum_w": 1400,
                "maximum_w": 7000,
                "actual": {},
            }
        ],
    }
    coordinator.thermal = {
        "devices": [
            {
                "id": "tank",
                "name": "Electric boiler",
                "minimum_c": 50,
                "maximum_c": 65,
                "heater_w": 2000,
                "enabled": True,
                "temperature_entity": "sensor.tank_temperature",
                "power_entity": "sensor.tank_power",
                "energy_entity": "sensor.tank_energy",
                "planned": [],
                "actual": None,
                "issue": None,
            }
        ]
    }


def registrations(hass, entry):
    """Read actual device and entity registries for this one isolated connection."""
    return (
        dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id),
        er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id),
    )


def test_roles_group_every_platform_without_creating_hubs():
    """Five role groups share one connection, while EMS and Grid retain their parent ownership."""

    async def scenario():
        """Exercise the normal platform subscriptions against real Core registries."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            populate_groups(coordinator)
            coordinator.refresh()
            await hass.async_block_till_done()
            groups = {item.subentry_id: item.title for item in entry.subentries.values()}
            assert set(groups.values()) == {
                "Solar",
                "Batteries",
                "Consumption and cycles",
                "Electric boilers",
                "EV charging",
            }
            devices, entities = registrations(hass, entry)
            counts = {}
            for device in devices:
                title = groups.get(device.config_subentry_id)
                counts[title] = counts.get(title, 0) + 1
                assert device.via_device_id is None
                owned = [item for item in entities if item.device_id == device.id]
                assert owned
                assert {item.config_subentry_id for item in owned} == {device.config_subentry_id}
            assert counts == {
                None: 2,
                "Solar": 2,
                "Batteries": 1,
                "Consumption and cycles": 2,
                "Electric boilers": 1,
                "EV charging": 2,
            }
            assert {item.domain for item in entities} == {
                "sensor",
                "switch",
                "date",
                "time",
                "button",
                "number",
                "select",
                "binary_sensor",
            }
            result = await hass.config_entries.subentries.async_init(
                (entry.entry_id, "planning_group"), context={"source": "user"}
            )
            assert result["type"] == "abort"
            assert result["reason"] == "managed_by_app"

    asyncio.run(scenario())


def test_adoption_and_reload_preserve_registry_metadata(monkeypatch):
    """Moving an existing device and disabled entity retains IDs, names, area and manual labels."""
    from custom_components.open_energy_planner import subentries

    async def scenario():
        """First register the old ungrouped device, then adopt it using native entity-platform setup."""
        original_add = subentries.add_grouped_entities

        def ungrouped(_coordinator, add, entities):
            """Reproduce the previous integration's parent-only registration."""
            add(entities)

        monkeypatch.setattr(subentries, "add_grouped_entities", ungrouped)
        # Registration owners import the helper at module load.
        from custom_components.open_energy_planner import device_sensors

        monkeypatch.setattr(device_sensors, "add_grouped_entities", ungrouped)
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            devices, entities = registrations(hass, entry)
            battery = next(item for item in devices if item.name == "Battery")
            sensor = next(item for item in entities if item.device_id == battery.id)
            area = ar.async_get(hass).async_create("Synthetic utility room")
            label = lr.async_get(hass).async_create("Synthetic owner label")
            devices_registry, entities_registry = dr.async_get(hass), er.async_get(hass)
            devices_registry.async_update_device(
                battery.id, name_by_user="My storage", area_id=area.id, labels={label.label_id}
            )
            entities_registry.async_update_entity(
                sensor.entity_id,
                name="My energy evidence",
                labels={label.label_id},
                disabled_by=er.RegistryEntryDisabler.USER,
            )
            saved_devices = {item.id for item in devices}
            saved_entities = {item.unique_id: item.entity_id for item in entities}
            catalogue = deepcopy(coordinator.devices)
            assert not entry.subentries
            assert entry.runtime_data is coordinator
            monkeypatch.setattr(device_sensors, "add_grouped_entities", original_add)
            monkeypatch.setattr(subentries, "add_grouped_entities", original_add)
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            devices, entities = registrations(hass, entry)
            assert {item.id for item in devices} == saved_devices
            assert {item.unique_id: item.entity_id for item in entities} == saved_entities
            moved = devices_registry.async_get(battery.id)
            assert moved.config_subentry_id in entry.subentries
            assert moved.name_by_user == "My storage"
            assert moved.area_id == area.id
            assert moved.labels == {label.label_id}
            retained = entities_registry.async_get(sensor.entity_id)
            assert retained.name == "My energy evidence"
            assert retained.disabled_by is er.RegistryEntryDisabler.USER
            assert retained.labels == {label.label_id}
            assert retained.config_subentry_id == moved.config_subentry_id
            group_ids = set(entry.subentries)
            coordinator = entry.runtime_data
            coordinator.devices = catalogue
            coordinator.refresh()
            await hass.async_block_till_done()
            assert set(entry.subentries) == group_ids

    asyncio.run(scenario())


@pytest.mark.parametrize("app_unavailable", [False, True])
def test_native_group_deletion_automatically_restores_app_membership(monkeypatch, app_unavailable):
    """Deleting a managed HA group reloads the connection and restores its app-configured devices."""

    async def scenario():
        """Delete through Core's official API and wait for normal lifecycle recovery."""
        async with running_chain() as (hass, entry, _url, _root):
            old_coordinator = entry.runtime_data
            devices, entities = registrations(hass, entry)
            original_counts = len(devices), len(entities)
            unaffected_devices = {item.id for item in devices if item.config_subentry_id is None}
            assert {item.title for item in entry.subentries.values()} == {"Batteries"}
            removed_id = next(iter(entry.subentries))
            from custom_components.open_energy_planner.client import LocalClient

            original_snapshot = LocalClient.snapshot
            if app_unavailable:
                monkeypatch.setattr(LocalClient, "snapshot", AsyncMock(side_effect=ClientError()))
            hass.config_entries.async_remove_subentry(entry, removed_id)
            await hass.async_block_till_done()
            assert entry.runtime_data is not old_coordinator
            assert old_coordinator.closed
            if app_unavailable:
                assert entry.runtime_data.issue == "synchronization_failed"
                monkeypatch.setattr(LocalClient, "snapshot", original_snapshot)
                await entry.runtime_data.sync()
                await hass.async_block_till_done()
            assert {item.title for item in entry.subentries.values()} == {"Batteries"}
            assert removed_id not in entry.subentries
            devices, entities = registrations(hass, entry)
            assert (len(devices), len(entities)) == original_counts
            assert {
                item.id for item in devices if item.config_subentry_id is None
            } == unaffected_devices
            assert len(hass.config_entries.async_entries("open_energy_planner")) == 1
            assert all(
                item.config_subentry_id is None or item.config_subentry_id in entry.subentries
                for item in (*devices, *entities)
            )

    asyncio.run(scenario())


def test_withdrawal_retires_empty_groups_but_outage_keeps_them(monkeypatch):
    """Connection failure preserves groups and metadata, while explicit catalogue withdrawal removes them."""

    async def scenario():
        """Use the real failure path before removing every role through its normal subscriptions."""
        async with running_chain() as (hass, entry, _url, _root):
            coordinator = entry.runtime_data
            populate_groups(coordinator)
            coordinator.refresh()
            await hass.async_block_till_done()
            group_ids = set(entry.subentries)
            devices, entities = registrations(hass, entry)
            before = ({item.id for item in devices}, {item.entity_id for item in entities})
            monkeypatch.setattr(
                coordinator.client, "snapshot", AsyncMock(side_effect=ClientError())
            )
            await coordinator.sync()
            await hass.async_block_till_done()
            assert set(entry.subentries) == group_ids
            devices, entities = registrations(hass, entry)
            assert ({item.id for item in devices}, {item.entity_id for item in entities}) == before
            coordinator.devices = []
            coordinator.consumer_forecasts = {}
            coordinator.appliances = {}
            coordinator.ev = {}
            coordinator.thermal = {}
            coordinator.refresh()
            await hass.async_block_till_done()
            assert not entry.subentries
            assert entry.runtime_data is coordinator
            devices, entities = registrations(hass, entry)
            assert len(devices) == 2
            assert all(item.config_subentry_id is None for item in (*devices, *entities))

    asyncio.run(scenario())
