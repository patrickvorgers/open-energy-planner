"""Native virtual thermal controls and evidence. No physical actions or helper entities."""

from aiohttp import ClientError
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .entity import PlannerEntity
from .protocol import timestamp
from .thermal import command, reading

HEATING_POWER_FRACTION = 0.5
LABELS = {
    "temperature": "Water temperature",
    "planned_power": "Requested heating power",
    "predicted_temperature": "Predicted interval-end temperature",
    "comfort_shortfall": "Predicted comfort shortfall",
    "requested_heating": "Heating requested",
    "measured_heating": "Measured heating",
    "minimum_c": "Minimum water temperature",
    "enabled": "Thermal planning enabled",
}


class ThermalEntity(PlannerEntity):
    """Read committed app-owned settings using one stable virtual-device identity."""

    def __init__(self, coordinator, row, key):
        """Bind the configured identity while preserving HA user-assigned device names."""
        super().__init__(coordinator, "thermal_" + row["id"], key, LABELS[key])
        self.identity, self.key = row["id"], key
        self._attr_device_info["name"] = row["name"]
        self._attr_icon = "mdi:water-boiler"
        url = self._attr_device_info["configuration_url"]
        if url.endswith("/setup"):
            self._attr_device_info["configuration_url"] = (
                url[: -len("/setup")] + "/appliance-settings/thermal"
            )

    def row(self):
        """Read current metadata rather than retaining stale constructor configuration."""
        return next(
            (
                row
                for row in getattr(self.coordinator, "thermal", {}).get("devices", [])
                if row["id"] == self.identity
            ),
            {},
        )

    @property
    def available(self):
        """Retain configuration controls only while the app still owns this virtual device."""
        return bool(self.row()) and not self.coordinator.closed

    def measured(self):
        """Return an independently fresh local measurement cohort or unknown."""
        row = self.row()
        return reading(self.coordinator.hass, row, dt_util.utcnow()) if row else None

    def planned(self):
        """Expose the current planned interval only under an accepted matching plan revision."""
        if command(self.coordinator, self.identity) is None:
            return None
        now = dt_util.utcnow()
        return next(
            (
                step
                for step in self.row().get("planned", [])
                if timestamp(step["start"]) <= now < timestamp(step["end"])
            ),
            None,
        )

    async def intent(self, field, value):
        """Forward explicit intent and refresh only after the application accepts it durably."""
        if not self.available:
            raise HomeAssistantError("This thermal device is no longer configured")
        try:
            await self.coordinator.client.request(
                "POST",
                "/v1/thermal/intent",
                {"device_id": self.identity, "field": field, "value": value},
            )
            await self.coordinator.sync()
        except (ClientError, TimeoutError, ValueError, KeyError) as error:
            raise HomeAssistantError("The app could not accept this thermal request") from error


class ThermalSensor(ThermalEntity, SensorEntity):
    """Keep measured temperature, proposed power and forecast state visibly distinct."""

    def __init__(self, coordinator, row, key):
        """Set physical units and precision without rounding the underlying evidence."""
        super().__init__(coordinator, row, key)
        self._attr_suggested_display_precision = 1
        if key in ("temperature", "predicted_temperature"):
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif key == "planned_power":
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_suggested_display_precision = 0
        else:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    @property
    def native_value(self):
        """Withhold unavailable measurements and predictions instead of fabricating zero."""
        if self.key == "temperature":
            measured = self.measured()
            return measured["temperature_c"] if measured is not None else None
        requested = command(self.coordinator, self.identity)
        if self.key == "planned_power":
            return self.row()["heater_w"] if requested else 0 if requested is False else None
        planned = self.planned()
        if planned is None:
            return None
        return planned["end_c" if self.key == "predicted_temperature" else "shortfall_kwh"]

    @property
    def extra_state_attributes(self):
        """Expose correlated plan/configuration identities and the app's readiness explanation."""
        plan = self.coordinator.schedule.plan
        return {
            "plan_id": plan.plan_id if plan else None,
            "configuration_version": getattr(self.coordinator, "thermal", {}).get(
                "configuration_version"
            ),
            "issue": self.row().get("issue"),
        }


class ThermalHeating(ThermalEntity, BinarySensorEntity):
    """Separate virtual heating permission from fresh measured element activity."""

    @property
    def is_on(self):
        """Withdraw permission immediately and avoid treating standby watts as element heating."""
        if self.key == "requested_heating":
            return command(self.coordinator, self.identity) is True
        measured = self.measured()
        if measured is None:
            return None
        return measured["power_w"] >= self.row()["heater_w"] * HEATING_POWER_FRACTION


class ThermalMinimum(ThermalEntity, NumberEntity):
    """A helper-free comfort intent. Temperature calculations stay in the application."""

    _attr_native_min_value = 0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_max_value(self):
        """Respect the currently configured upper bound rather than caching a former rating."""
        return self.row().get("maximum_c", 0)

    @property
    def native_value(self):
        """Return only the committed minimum temperature."""
        return self.row().get("minimum_c")

    async def async_set_native_value(self, value):
        """Send the user's comfort change without optimistic local state."""
        await self.intent("minimum_c", value)


class ThermalEnabled(ThermalEntity, SwitchEntity):
    """Control planning participation without switching the physical heating element."""

    @property
    def is_on(self):
        """Read the application-owned planning intent."""
        return self.row().get("enabled", False)

    async def async_turn_on(self, **kwargs):
        """Enable future thermal planning through the authenticated application boundary."""
        await self.intent("enabled", True)

    async def async_turn_off(self, **kwargs):
        """Withdraw planning intent. The user's automation owns physical fallback."""
        await self.intent("enabled", False)
