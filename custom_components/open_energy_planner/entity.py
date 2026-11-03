"""Common subscription/device identity. Never set physical entity states."""

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN, RELEASE


def source_device_name(hass, device, energy_sources=()):
    """Prefer Energy's explicit name, then the linked HA device and sensor names.

    Registry metadata remains usable before a source has a state. Device renames
    belong to HA's device-registry name_by_user and are never written here.
    """
    source = device.get("energy_entity")
    energy_name = next(
        (
            item.get("name")
            for item in energy_sources
            if item.get("type") == device["kind"]
            and source is not None
            and source in (item.get("stat_energy_from"), item.get("stat_energy_to"))
        ),
        None,
    )
    state = hass.states.get(source) if isinstance(source, str) else None
    entry = er.async_get(hass).async_get(source) if isinstance(source, str) else None
    physical = dr.async_get(hass).async_get(entry.device_id) if entry and entry.device_id else None
    for name in (
        energy_name,
        physical.name_by_user if physical else None,
        physical.name if physical else None,
        state.name if state else None,
        entry.name if entry else None,
        entry.original_name if entry else None,
        device.get("name"),
    ):
        if isinstance(name, str):
            name = name.strip()
            if name and name not in (source, device["id"]):
                return name
    return {"battery": "Battery", "solar": "Solar", "grid": "Grid"}[device["kind"]]


class PlannerEntity(Entity):
    """Provide stable virtual device/entity identities and event-driven projection subscriptions."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator, device, key, name):
        """Bind entry-scoped identity and display metadata without controlling physical equipment."""
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{device}_{key}"
        self._attr_name = name
        configuration_url = coordinator.entry.data.get(
            "ui_url", coordinator.entry.data["url"].rstrip("/") + "/setup"
        )
        if configuration_url.startswith("/"):
            # HA resolves this internal URL against the active frontend origin.
            configuration_url = "homeassistant:/" + configuration_url
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_{device}")},
            name={"ems": "Open Energy Planner", "grid": "Grid", "battery": "Battery"}.get(
                device, device
            ),
            manufacturer="Open Energy Planner",
            model="Virtual planning device",
            sw_version=RELEASE,
            configuration_url=configuration_url,
        )

    async def async_added_to_hass(self):
        """Subscribe HA state writes to projection updates and unregister on entity removal."""
        self.async_on_remove(self.coordinator.subscribe(self.async_write_ha_state))
