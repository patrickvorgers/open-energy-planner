"""Real HA registry and manifest metadata for integration, account and physical-device selection."""

import asyncio
from types import MappingProxyType

import pytest

from tests.custom_component.support.chain import running_chain


@pytest.mark.parametrize("service", [False, True])
@pytest.mark.parametrize("loaded", [False, True])
def test_source_identity_uses_manifest_name_and_preserves_account_and_device(service, loaded):
    """Loaded MQTT names and registry-only platform fallback keep account/device/service identities without forwarding provider secrets."""
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.loader import async_get_integration
    from homeassistant.util import dt as dt_util

    from custom_components.open_energy_planner.catalogue import catalogue_entity, source_choices
    from open_energy_planner.home_assistant.catalogue import HACatalogue

    async def check():
        """Read genuine loaded manifest and registry metadata without loading a provider or controlling any device."""
        async with running_chain(live=True, fixture_devices=False) as (hass, oep_entry, *_):
            oep_entry.runtime_data.poll_cancel()
            oep_entry.runtime_data.poll_cancel = None
            domain = "mqtt" if loaded else "synthetic_source"
            if loaded:
                await async_get_integration(hass, "mqtt")
            configured = ConfigEntry(
                data={"password": "synthetic-provider-secret"},
                options={},
                domain=domain,
                entry_id="synthetic-account",
                source="user",
                title="Synthetic account title",
                unique_id=None,
                version=1,
                minor_version=1,
                state=ConfigEntryState.NOT_LOADED,
                discovery_keys=MappingProxyType({}),
                subentries_data=[],
            )
            hass.config_entries._entries[configured.entry_id] = configured
            device = dr.async_get(hass).async_get_or_create(
                config_entry_id=configured.entry_id,
                identifiers={(domain, "synthetic-car")},
                name="Synthetic car",
                entry_type=dr.DeviceEntryType.SERVICE if service else None,
            )
            registered = er.async_get(hass).async_get_or_create(
                "sensor",
                domain,
                "synthetic-car-soc",
                config_entry=configured,
                device_id=device.id,
                suggested_object_id="synthetic_car_soc",
            )
            hass.states.async_set(
                registered.entity_id,
                "62",
                {
                    "friendly_name": "Battery level",
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                },
            )
            metadata = catalogue_entity(hass, hass.states.get(registered.entity_id), {})
            assert metadata["integration_domain"] == ("mqtt" if loaded else "synthetic_source")
            assert metadata["integration_display_name"] == (
                "MQTT" if loaded else "Synthetic source"
            )
            assert metadata["integration_id"] == "synthetic-account"
            assert metadata["integration_name"] == "Synthetic account title"
            assert metadata["device_id"] == device.id
            assert metadata["device_name"] == "Synthetic car"
            assert metadata["entity_id"] == registered.entity_id
            assert metadata.get("device_entry_type") == ("service" if service else None)
            assert "state" not in metadata
            assert "synthetic-provider-secret" not in str(metadata)
            body = await source_choices(hass, "ev")
            row = next(
                entity for entity in body["entities"] if entity["entity_id"] == registered.entity_id
            )
            assert row == metadata
            catalogue = HACatalogue()
            catalogue.receive(body, dt_util.utcnow())
            accepted = next(
                entity
                for entity in catalogue.value["entities"]
                if entity["entity_id"] == registered.entity_id
            )
            assert accepted == metadata

    asyncio.run(check())
