"""Clear Home Assistant projections when a trusted app replaces the installation."""

from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN, projection_id


@callback
def discard_old_registry(hass, entry, *, keep=None):
    """Remove only OEP devices and entities owned by the specified config entry.

    Args:
        hass: The running Home Assistant instance.
        entry: The OEP entry whose registrations are being reconciled.
        keep: The current projection's identifier prefix. None removes all OEP
            registrations for the entry. A prefix retains the current projection
            during setup, including after interrupted registry persistence.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.platform == DOMAIN and (keep is None or not entity.unique_id.startswith(keep)):
            entities.async_remove(entity.entity_id)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if any(
            domain == DOMAIN and (keep is None or not identifier.startswith(keep))
            for domain, identifier in device.identifiers
        ):
            devices.async_remove_device(device.id)


async def reset_projection(hass, entry):
    """Discard old control intent and planning groups after the coordinator stops."""
    await remove_intent(hass, entry)
    discard_old_registry(hass, entry)
    for subentry_id in tuple(entry.subentries):
        hass.config_entries.async_remove_subentry(entry, subentry_id)


async def remove_intent(hass, entry):
    """Remove local OEP intent and its connection notification."""
    await Store(hass, 1, f"{DOMAIN}.{projection_id(entry)}").async_remove()
    persistent_notification.async_dismiss(hass, f"{DOMAIN}.{entry.entry_id}")
