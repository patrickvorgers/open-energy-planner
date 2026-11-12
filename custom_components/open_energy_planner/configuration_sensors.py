"""Read-only device settings as separate HA entities, independent of plan commands."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import PlannerEntity

PERCENT_SCALE = 100
CONFIGURATION_DECIMAL_PLACES = 6
MONEY_DISPLAY_DECIMALS = 2

PROPERTIES = {
    "capacity_kwh": ("Usable capacity", "kWh"),
    "capacity_w": ("Panel capacity", "W"),
    "charge_limit_w": ("Maximum charge power", "W"),
    "discharge_limit_w": ("Maximum discharge power", "W"),
    "minimum_soc": ("Minimum state of charge", "%"),
    "charge_efficiency": ("Charge efficiency", "%"),
    "discharge_efficiency": ("Discharge efficiency", "%"),
    "dc_efficiency": ("DC efficiency", "%"),
    "wear_cost_per_kwh": ("Wear cost per kWh", None),
    "can_follow_grid": ("Grid following permitted", None),
    "recover_reserve": ("Mandatory reserve recovery", None),
    "calibration_enabled": ("Periodic calibration enabled", None),
    "calibration_interval_days": ("Calibration interval", "d"),
    "calibration_hold_hours": ("Calibration hold", "h"),
    "can_curtail": ("Curtailment permitted", None),
    "orientation": ("Panel orientation", None),
    "tilt": ("Panel tilt", "°"),
    "measurement_boundary": ("Measurement boundary", None),
    "inverter_id": ("Connected inverter", None),
    "soc_entity": ("State of charge sensor", None),
    "power_entity": ("Actual power sensor", None),
    "power_inverted": ("Measured power inverted", None),
    "entity_id": ("Production energy sensor", None),
    "energy_entity": ("Energy dashboard meter", None),
    "charge_energy_entity": ("Charge energy sensor", None),
    "inverter.ac_limit_w": ("Inverter AC power limit", "W"),
    "inverter.efficiency": ("Inverter efficiency", "%"),
    "inverter.hybrid": ("Hybrid inverter", None),
    "import_limit_w": ("Maximum grid import", "W"),
    "export_limit_w": ("Maximum grid export", "W"),
    "grid_power_entity": ("Actual grid power sensor", None),
    "grid_power_inverted": ("Measured grid power inverted", None),
    "grid_export_entity": ("Grid export energy sensor", None),
}


def property_values(device):
    """Return declared non-None settings, flattening inverter fields and falling back to the root Energy meter."""
    configuration = device.get("configuration", {})
    values = {
        **configuration,
        **{"inverter." + key: value for key, value in configuration.get("inverter", {}).items()},
    }
    values.setdefault("energy_entity", device.get("energy_entity"))
    return {key: values[key] for key in PROPERTIES if key in values and values[key] is not None}


class DeviceProperty(PlannerEntity, SensorEntity):
    """Expose one confirmed property with its unit and a stable identity. Never operate equipment."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device, key):
        """Bind a property field while keeping configuration and command entity identities separate."""
        name, unit = PROPERTIES[key]
        super().__init__(
            coordinator,
            "grid" if device["kind"] == "grid" else "unit_" + device["id"],
            "config_" + key.replace(".", "_"),
            name,
        )
        self.device_id, self.kind, self.key = device["id"], device["kind"], key
        self.planning_group = self.kind if self.kind != "grid" else None
        self._attr_device_info["name"] = device.get("name", device["id"])
        self._attr_native_unit_of_measurement = unit
        if key == "wear_cost_per_kwh":
            self._attr_suggested_display_precision = MONEY_DISPLAY_DECIMALS
        if unit == "W":
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_suggested_display_precision = 0
        elif unit in ("kWh", "%", "°"):
            self._attr_suggested_display_precision = 1
        if type(property_values(device).get(key)) is bool:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["yes", "no"]
            self._attr_translation_key = "permission"

    def values(self):
        """Read current confirmed settings rather than caching the entity-creation value."""
        if self.kind == "grid":
            return property_values({"configuration": self.coordinator.grid_configuration or {}})
        device = next(
            (
                device_item
                for device_item in self.coordinator.devices or []
                if device_item["id"] == self.device_id and device_item["kind"] == self.kind
            ),
            {},
        )
        return property_values(device)

    @property
    def available(self):
        """Keep settings readable without commands, but never fabricate missing or removed properties."""
        return self.key in self.values()

    @property
    def native_value(self):
        """Convert efficiencies to percent and booleans to explicit read-only enum states."""
        value = self.values().get(self.key)
        if type(value) is bool:
            return "yes" if value else "no"
        if value is not None and self.key.endswith("efficiency"):
            return round(value * PERCENT_SCALE, CONFIGURATION_DECIMAL_PLACES)
        return value

    @property
    def extra_state_attributes(self):
        """Describe wear-cost units without assuming a contract currency or inventing statistics."""
        return (
            {"cost_basis": "contract_currency_per_internal_discharged_kwh"}
            if self.key == "wear_cost_per_kwh"
            else None
        )
