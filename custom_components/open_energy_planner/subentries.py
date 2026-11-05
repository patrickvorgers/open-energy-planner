"""Register app-owned planning groups through Home Assistant's native subentries.

Groups organize virtual devices only. Stable identifiers and user-owned registry
metadata remain unchanged. Retirement waits for both catalogue withdrawal and
the existing entity/device removal lifecycle to finish.
"""

import logging
from collections import defaultdict
from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

GROUP_TITLES = {
    "solar": "Solar",
    "battery": "Batteries",
    "consumption": "Consumption and cycles",
    "thermal": "Electric boilers",
    "ev": "EV charging",
}
SUBENTRY_TYPE = "planning_group"
_LOGGER = logging.getLogger(__name__)


@callback
def add_grouped_entities(coordinator, async_add_entities, entities):
    """Add each role to its native group, moving existing devices in place.

    HA's entity platform updates existing entity registrations when it receives
    config_subentry_id. Move the device association first so all its platforms
    share the same owner without recreating names, areas, labels or identities.
    """
    entry, hass = coordinator.entry, coordinator.hass
    batches = defaultdict(list)
    for entity in entities:
        batches[entity.planning_group].append(entity)
    devices = dr.async_get(hass)
    for group, additions in batches.items():
        subentry_id = None
        if group is not None:
            subentry = next(
                (
                    item
                    for item in entry.subentries.values()
                    if item.subentry_type == SUBENTRY_TYPE and item.unique_id == group
                ),
                None,
            )
            if subentry is None:
                subentry = ConfigSubentry(
                    data=MappingProxyType({}),
                    subentry_type=SUBENTRY_TYPE,
                    title=GROUP_TITLES[group],
                    unique_id=group,
                )
                hass.config_entries.async_add_subentry(entry, subentry)
            subentry_id = subentry.subentry_id
        for entity in additions:
            for identifier in entity.device_info["identifiers"]:
                device = devices.async_get_device_by_identifier(identifier, entry.entry_id)
                if device and device.config_subentry_id != subentry_id:
                    devices.async_update_device(device.id, new_config_subentry_id=subentry_id)
        async_add_entities(additions, config_subentry_id=subentry_id)


@callback
def configured_groups(coordinator):
    """Read group membership from accepted app roles, never display names."""
    desired = {device["kind"] for device in coordinator.devices or []}
    consumers = coordinator.consumer_forecasts
    sources = {item["entity_id"] for item in consumers.get("options", [])}
    if any(item["entity_id"] in sources for item in consumers.get("selected", [])) or any(
        row.get("shiftable") and row["entity_id"] in sources
        for row in coordinator.appliances.get("devices", [])
    ):
        desired.add("consumption")
    if coordinator.thermal.get("devices"):
        desired.add("thermal")
    if coordinator.ev.get("vehicles") or coordinator.ev.get("points"):
        desired.add("ev")
    return desired


@callback
def retire_empty_groups(coordinator):
    """Remove only withdrawn groups with no remaining HA devices or entities.

    A still-configured role protects scheduled additions on every platform.
    Failed removals retain their group instead of deleting registry metadata.
    Unavailable app connections keep the last accepted catalogue.
    """
    entry, hass = coordinator.entry, coordinator.hass
    desired = configured_groups(coordinator)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    occupied = {item.config_subentry_id for item in (*devices, *entities)}
    for subentry in tuple(entry.subentries.values()):
        if (
            subentry.subentry_type == SUBENTRY_TYPE
            and subentry.unique_id in GROUP_TITLES
            and subentry.unique_id not in desired
            and subentry.subentry_id not in occupied
        ):
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)


@callback
def register_group_recovery(coordinator):
    """Reload once when HA deletes a group that the app still configures.

    Native deletion also removes its devices and entities. A normal integration
    reload recreates all platforms from the authoritative app instead of leaving
    stale entity instances in their reconciliation owners. App-side withdrawal
    remains authoritative and never triggers recovery.
    """
    entry = coordinator.entry
    reloading = False

    async def updated(hass, changed_entry):
        """Coalesce native group deletions into one standard integration reload."""
        nonlocal known, reloading
        current = {
            item.subentry_id: item.unique_id
            for item in changed_entry.subentries.values()
            if item.subentry_type == SUBENTRY_TYPE and item.unique_id in GROUP_TITLES
        }
        withdrawn = {group for identity, group in known.items() if identity not in current}
        known = current
        if not reloading and withdrawn & configured_groups(coordinator):
            reloading = True
            _LOGGER.info(
                "Home Assistant removed a planning group that is still configured in the app. "
                "Reloading the connection to restore its virtual devices"
            )
            await hass.config_entries.async_reload(entry.entry_id)

    known = {
        item.subentry_id: item.unique_id
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE and item.unique_id in GROUP_TITLES
    }
    entry.async_on_unload(entry.add_update_listener(updated))
