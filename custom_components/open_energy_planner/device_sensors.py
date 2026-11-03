"""Capability-driven virtual sensors. Every value remains a proposed command for user automations."""

from homeassistant.components import persistent_notification
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfPower
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory

from .configuration_sensors import DeviceProperty, property_values
from .entity import PlannerEntity, source_device_name


class DeviceTarget(PlannerEntity, SensorEntity):
    """Expose one battery target or controllable PV on/off proposal under a stable virtual device."""

    def __init__(self, coordinator, device, key):
        """Bind stable capability identity, preserving per-device history across target changes."""
        super().__init__(
            coordinator,
            "unit_" + device["id"],
            key,
            {"power": "Power target", "mode": "Control mode", "enabled": "Enabled target"}[key],
        )
        self.device_id, self.kind, self.key = device["id"], device["kind"], key
        self._attr_device_info["name"] = device.get("name", device["id"])
        if key == "power":
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_suggested_display_precision = 0
        else:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_translation_key = "battery_mode" if key == "mode" else "solar_enabled"
            self._attr_options = (
                ["fixed_power", "follow_grid_target"] if key == "mode" else ["on", "off"]
            )

    def command(self):
        """Read only the currently active complete plan. Disconnection does not invent a fallback target."""
        command = self.coordinator.projection.command
        return (
            next(
                (
                    device_item
                    for device_item in command.devices
                    if device_item.device_id == self.device_id and device_item.kind == self.kind
                ),
                None,
            )
            if command
            else None
        )

    @property
    def available(self):
        """Keep stale/expired/paused or removed capabilities unavailable to physical automations."""
        return self.command() is not None and any(
            device_item["id"] == self.device_id and device_item["kind"] == self.kind
            for device_item in self.coordinator.devices or []
        )

    @property
    def native_value(self):
        """Return a whole DC-watt battery target or a PV enable proposal without driving equipment."""
        command = self.command()
        if command is None:
            return None
        if self.key == "mode":
            return command.mode
        return command.power_w if self.kind == "battery" else "on" if command.enabled else "off"

    @property
    def extra_state_attributes(self):
        """Publish command interpretation without retriggering unchanged targets for a new plan ID."""
        command = self.command()
        return {
            "mode": command.mode
            if self.kind == "battery" and command
            else "on_off"
            if self.kind == "solar"
            else None,
            "applicable": bool(command and (self.key != "power" or command.mode == "fixed_power")),
            "grid_device": "grid"
            if self.kind == "battery" and command and command.mode == "follow_grid_target"
            else None,
            "measurement_side": "battery_terminal_dc"
            if self.kind == "battery"
            else "solar_production",
        }


class DeviceConfiguration(PlannerEntity, SensorEntity):
    """Keep confirmed device properties readable even while commands are paused or unavailable."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "setup_status"

    def __init__(self, coordinator, device, key="setup_status"):
        """Use a stable device identity without publishing a physical control or fabricated measurement."""
        super().__init__(
            coordinator,
            "unit_" + device["id"],
            key,
            "Reserve status" if key == "reserve_status" else "Setup status",
        )
        self.key = key
        self._attr_translation_key = key
        self._attr_options = (
            ["measurement_unavailable", "reserve_satisfied", "recovery_required", "below_reserve"]
            if key == "reserve_status"
            else ["configured", "setup_required"]
        )
        self.device_id, self.kind = device["id"], device["kind"]
        self._attr_device_info["name"] = device.get("name", device["id"])

    def definition(self):
        """Read the latest configuration rather than retaining properties from entity creation."""
        return next(
            (
                device_item
                for device_item in self.coordinator.devices or []
                if device_item["id"] == self.device_id
            ),
            None,
        )

    @property
    def available(self):
        """Remain readable without a plan. A removed device has no current definition."""
        return self.definition() is not None

    @property
    def native_value(self):
        """Expose app-derived reserve need or distinguish confirmed ratings from incomplete setup."""
        if self.key == "reserve_status":
            return (self.definition() or {}).get("reserve_status", "measurement_unavailable")
        return (
            "configured" if (self.definition() or {}).get("configured", True) else "setup_required"
        )

    @property
    def extra_state_attributes(self):
        """Link device status to its authoritative Energy meter without bundling property values."""
        device = self.definition() or {}
        return {"energy_entity": device.get("energy_entity")}


async def register_devices(hass, entry, async_add_entities):
    """Add/remove entity capabilities as the app-owned catalogue changes, using stable IDs and subscriptions."""
    coordinator = entry.runtime_data
    from homeassistant.components.energy.data import async_get_manager

    energy = await async_get_manager(hass)
    entities = {}
    pending = set()

    async def remove(identity, entity):
        """Remove a withdrawn capability instead of leaving a silent stale command entity behind."""
        try:
            if entity.hass is not None:
                await entity.async_remove()
            registry = er.async_get(hass)
            entity_id = registry.async_get_entity_id(
                "sensor", "open_energy_planner", entity.unique_id
            )
            if entity_id:
                device_id = registry.async_get(entity_id).device_id
                registry.async_remove(entity_id)
                if device_id and not er.async_entries_for_device(registry, device_id):
                    dr.async_get(hass).async_remove_device(device_id)
                if identity[1] in ("power", "mode", "enabled"):
                    persistent_notification.async_create(
                        hass,
                        f"The virtual command entity {entity_id} was removed because its configured capability was withdrawn. Check automations that refer to it.",
                        title="Open Energy Planner configuration changed",
                        notification_id=f"oep_capability_{entry.entry_id}_{identity}",
                    )
        finally:
            pending.discard(identity)

    @callback
    def update():
        """Reconcile only capability identities. Unchanged targets do not recreate entities."""
        definitions = [
            {
                **device,
                "name": source_device_name(
                    hass, device, (energy.data or {}).get("energy_sources", ())
                ),
            }
            for device in coordinator.devices or []
        ]
        if coordinator.grid_configuration:
            definitions.append(
                {
                    "id": "grid",
                    "kind": "grid",
                    "name": "Grid",
                    "configuration": coordinator.grid_configuration,
                }
            )
        configured = {}
        for device in definitions:
            device_identity = "grid" if device["kind"] == "grid" else "unit_" + device["id"]
            registered = dr.async_get(hass).async_get_device_by_identifier(
                ("open_energy_planner", f"{entry.entry_id}_{device_identity}"), entry.entry_id
            )
            if registered and registered.name != device["name"]:
                dr.async_get(hass).async_update_device(registered.id, name=device["name"])
            keys = ["config_" + key for key in property_values(device)]
            if device["kind"] != "grid":
                keys.append("setup_status")
                if device["kind"] == "battery":
                    keys.append("reserve_status")
                if device.get("configured", True):
                    keys.extend(
                        ("power", "mode")
                        if device["kind"] == "battery"
                        else ("enabled",)
                        if device.get("can_curtail", True)
                        else ()
                    )
            identity = "grid" if device["kind"] == "grid" else "unit_" + device["id"]
            configured.update({(identity, key): device for key in keys})
        if coordinator.devices is not None:
            registry = er.async_get(hass)
            for key in ("mode", "power"):
                legacy = registry.async_get_entity_id(
                    "sensor", "open_energy_planner", f"{entry.entry_id}_battery_{key}"
                )
                if legacy:
                    registry.async_remove(legacy)
            devices = dr.async_get(hass)
            legacy_device = devices.async_get_device_by_identifier(
                ("open_energy_planner", f"{entry.entry_id}_battery"), entry.entry_id
            )
            if legacy_device and not er.async_entries_for_device(registry, legacy_device.id):
                devices.async_remove_device(legacy_device.id)
        for identity, entity in list(entities.items()):
            if identity not in configured or configured[identity]["kind"] != entity.kind:
                entities.pop(identity)
                pending.add(identity)
                entry.async_create_task(
                    hass, remove(identity, entity), "Remove withdrawn OEP target"
                )
        added = []
        for identity, device in configured.items():
            if identity not in entities and identity not in pending:
                entity = (
                    DeviceConfiguration(coordinator, device, identity[1])
                    if identity[1] in ("setup_status", "reserve_status")
                    else DeviceProperty(coordinator, device, identity[1].removeprefix("config_"))
                    if identity[1].startswith("config_")
                    else DeviceTarget(coordinator, device, identity[1])
                )
                entities[identity] = entity
                added.append(entity)
        if added:
            async_add_entities(added)

    entry.async_on_unload(coordinator.subscribe(update))
    update()
