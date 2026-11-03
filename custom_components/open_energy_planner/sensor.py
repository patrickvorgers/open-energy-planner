"""HA projection, including the difference between zero, unknown and unavailable."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfPower
from homeassistant.helpers.entity import EntityCategory

from .device_sensors import register_devices
from .entity import PlannerEntity


async def async_setup_entry(hass, entry, async_add_entities):
    """Register virtual status, active plan time and supported grid/battery commands."""
    coordinator = entry.runtime_data
    base = [
        PlannerSensor(coordinator, "ems", "status", "Status"),
        PlannerSensor(coordinator, "ems", "plan_generated_at", "Active plan generated at"),
        PlannerSensor(coordinator, "grid", "target", "Power target"),
        PlannerSensor(coordinator, "grid", "mode", "Control mode"),
    ]
    async_add_entities(base)
    await register_devices(hass, entry, async_add_entities)
    from .consumer_sensors import register_consumers

    register_consumers(hass, entry, async_add_entities)
    from .thermal import register_entities as register_thermal

    register_thermal(coordinator, async_add_entities, "sensor")
    from .appliances import register_entities

    register_entities(hass, entry, async_add_entities)
    from .ev import register_entities as register_ev
    from .ev_controls import EVSensor

    register_ev(
        hass,
        entry,
        async_add_entities,
        (
            *(
                ("vehicles", key, EVSensor)
                for key in ("actual_soc", "capacity_kwh", "maximum_w", "next_departure")
            ),
            *(
                ("points", key, EVSensor)
                for key in (
                    "power",
                    "actual_power",
                    "status",
                    "shortfall",
                    "predicted_soc",
                    "maximum_w",
                    "minimum_w",
                    "control",
                )
            ),
        ),
    )


class PlannerSensor(PlannerEntity, SensorEntity):
    """Expose current virtual commands and status with availability separate from applicability."""

    def __init__(self, coordinator, device, key, name):
        """Configure a projection field with timestamp, enum or whole-watt display metadata."""
        super().__init__(coordinator, device, key, name)
        self.key = key
        if key == "status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [
                "active",
                "paused",
                "waiting_for_new_plan",
                "not_ready",
                "expired",
                "stale",
            ]
            self._attr_translation_key = "status"
        elif key == "plan_generated_at":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        elif key == "target":
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_suggested_display_precision = 0
        elif key == "mode":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_translation_key = "grid_mode"
            self._attr_options = ["zero_grid", "inactive"]

    @property
    def available(self):
        """Keep status readable. Expose plan time and commands only while a valid command exists."""
        return self.key == "status" or self.coordinator.projection.command is not None

    @property
    def native_value(self):
        """Return status, active plan generation time or commands, preserving zero versus inapplicable None."""
        projection = self.coordinator.projection
        if self.key == "status":
            return projection.status
        command = projection.command
        if not command:
            return None
        if self.key == "plan_generated_at":
            return self.coordinator.schedule.plan.generated_at
        if self.key == "mode":
            return "zero_grid" if command.grid_active else "inactive"
        if self.key == "target":
            return 0 if command.grid_active else None
        return None

    @property
    def extra_state_attributes(self):
        """Expose plan/input status or grid-target applicability and its AC measurement boundary."""
        command = self.coordinator.projection.command
        if self.key == "plan_generated_at":
            plan = self.coordinator.schedule.plan
            return {"plan_id": plan.plan_id if command and plan else None}
        if self.key == "status":
            plan = self.coordinator.schedule.plan
            return {
                "plan_id": plan.plan_id if plan else None,
                "strategy": plan.strategy if plan else None,
                "connection_issue": self.coordinator.issue,
                "connection_type": "supervisor"
                if self.coordinator.entry.data.get("supervisor")
                else "standalone",
                "planning": self.coordinator.planning_status,
                "observations": self.coordinator.observations,
            }
        if not command:
            return {"applicable": False}
        if self.key == "mode":
            return None
        return {
            "applicable": command.grid_active,
            "mode": command.mode,
            "measurement_side": "grid_ac",
        }
