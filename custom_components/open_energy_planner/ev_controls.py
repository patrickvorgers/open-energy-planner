"""Helper-free EV dashboard controls and read-only evidence on HA virtual devices."""

from datetime import date
from uuid import uuid4

from aiohttp import ClientError
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.date import DateEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.time import TimeEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .entity import PlannerEntity
from .ev import command, power_reading, soc_reading
from .protocol import timestamp

LABELS = {
    "target_soc": "Target state of charge",
    "departure_time": "Charging departure time",
    "departure_date": "Charging departure date",
    "departure_clock": "One-off departure time",
    "schedule_charging": "Plan charging request",
    "cancel_charging": "Cancel charging request",
    "vehicle": "Connected vehicle",
    "power": "Requested charging power",
    "charging": "Charging requested",
    "status": "Planning readiness",
    "actual_power": "Measured charging power",
    "shortfall": "Predicted energy shortfall",
    "predicted_soc": "Predicted departure state of charge",
    "actual_soc": "State of charge",
    "capacity_kwh": "Usable battery capacity",
    "maximum_w": "Maximum charging power",
    "minimum_w": "Minimum charging power",
    "next_departure": "Next departure",
    "control": "Control capability",
}
EV_STATUSES = (
    "energy_source_unavailable",
    "telemetry_unavailable",
    "disconnected",
    "connection_unknown",
    "vehicle_unknown",
    "measurement_only",
    "ready",
    "soc_or_limits_unavailable",
    "request_missing",
)


class EVEntity(PlannerEntity):
    """Read committed app state, preserving stable HA IDs and user-assigned device names."""

    planning_group = "ev"

    def __init__(self, coordinator, kind, row, key):
        """Group car intent separately from charger capabilities and currently permitted commands."""
        super().__init__(coordinator, "ev_" + row["id"], key, LABELS[key])
        self.kind, self.identity, self.key = kind, row["id"], key
        self._attr_device_info["name"] = row["name"]
        if self._attr_device_info["configuration_url"].endswith("/setup"):
            self._attr_device_info["configuration_url"] = (
                self._attr_device_info["configuration_url"][: -len("/setup")]
                + "/appliance-settings/ev"
            )
        self._attr_icon = "mdi:car-electric" if kind == "vehicles" else "mdi:ev-station"

    def row(self):
        """Return current configuration rather than retaining an obsolete constructor snapshot."""
        return next(
            (row for row in self.coordinator.ev.get(self.kind, []) if row["id"] == self.identity),
            {},
        )

    @property
    def available(self):
        """Only expose writes while the application still owns the configured device."""
        return bool(self.row()) and not self.coordinator.closed

    async def write(self, operation, body):
        """Forward intent without optimistic state and resynchronize after durable acceptance."""
        try:
            await self.coordinator.write_ev(operation, body)
        except (ClientError, TimeoutError, ValueError, KeyError) as error:
            raise HomeAssistantError("The app could not accept this EV request") from error

    async def intent(self, field, value):
        """Keep all departure and charging calculations in the application."""
        await self.write("intent", {"vehicle_id": self.identity, "field": field, "value": value})


class EVTarget(EVEntity, NumberEntity):
    """Desired percentage, independent of the authoritative measured vehicle SoC."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"

    @property
    def available(self):
        """Scalar controls edit a complete existing request, never create one implicitly."""
        return super().available and bool(self.row().get("charging_request"))

    @property
    def native_value(self):
        """Show the persisted target, never an optimistic pending edit."""
        row = self.row()
        return (row.get("charging_request") or {}).get("target_soc", row.get("target_soc"))

    async def async_set_native_value(self, value):
        """Set the vehicle's next charging target using the existing authenticated app connection."""
        await self.intent("target_soc", value)


class EVDepartureTime(EVEntity, TimeEntity):
    """Time portion of the explicit charging request departure."""

    @property
    def available(self):
        """Require a planned charging request before editing its time."""
        return super().available and bool(self.row().get("charging_request"))

    @property
    def native_value(self):
        """Display local HA wall time even when the browser uses another timezone."""
        row = self.row()
        return dt_util.as_local(timestamp(row["next_departure"])).time() if row else None

    async def async_set_value(self, value):
        """Send the chosen local time. DST validation belongs to the application."""
        await self.intent(self.key, value.isoformat())


class EVDepartureDate(EVEntity, DateEntity):
    """Request date paired with its compact native time control."""

    @property
    def available(self):
        """Require a planned charging request before editing its date."""
        return super().available and bool(self.row().get("charging_request"))

    @property
    def native_value(self):
        """Display the planned request departure in the installation timezone."""
        row = self.row()
        return (
            date.fromisoformat(
                dt_util.as_local(timestamp(row["next_departure"])).date().isoformat()
            )
            if row
            else None
        )

    async def async_set_value(self, value):
        """Set only the date while retaining the departure time in HA's timezone."""
        await self.intent("departure_date", value.isoformat())


class EVRequestAction(EVEntity, ButtonEntity):
    """Create a complete default request or cancel its explicit current identity."""

    @property
    def available(self):
        """No request is created by scalar edits or a state refresh."""
        request = self.row().get("charging_request")
        return super().available and bool(request) == (self.key == "cancel_charging")

    async def async_press(self):
        """Use the same atomic request operations as the app and automation services."""
        if self.key == "schedule_charging":
            await self.write("requests", {"request_id": str(uuid4()), "vehicle_id": self.identity})
        else:
            request = self.row().get("charging_request")
            if not request:
                raise HomeAssistantError("No planned charging request to cancel")
            await self.write(
                "cancel",
                {
                    "request_id": request["id"],
                    "expected_version": request["version"],
                    "vehicle_id": self.identity,
                },
            )


class EVVehicle(EVEntity, SelectEntity):
    """Explicit car identity for this connection. An automation may select it without templates."""

    def labels(self):
        """Keep ordinary HA names readable, using the source only to disambiguate equal names."""
        allowed = self.row().get("vehicle_ids", [])
        cars = [car for car in self.coordinator.ev.get("vehicles", []) if car["id"] in allowed]
        return {
            car["id"]: car["name"]
            + (
                " (" + car["soc_entity"] + ")"
                if sum(other["name"] == car["name"] for other in cars) > 1
                else ""
            )
            for car in cars
        }

    @property
    def options(self):
        """Never default to the first allowed vehicle, including a single-vehicle charge point."""
        return ["Unknown", *self.labels().values()]

    @property
    def current_option(self):
        """Show the explicit current-session assignment or Unknown after a disconnect/restart."""
        actual = self.row().get("actual", {})
        local = self.coordinator.ev_sessions.get(self.identity, {})
        if (
            actual.get("session_id") != local.get("session_id")
            or local.get("connected") is not True
        ):
            return "Unknown"
        return self.labels().get(self.row().get("vehicle_id"), "Unknown")

    @property
    def available(self):
        """Require a live connected session before associating a car with it."""
        actual = self.row().get("actual", {})
        local = self.coordinator.ev_sessions.get(self.identity, {})
        return (
            super().available
            and actual.get("connected") is True
            and local.get("connected") is True
            and local.get("session_id") == actual.get("session_id")
        )

    async def async_select_option(self, option):
        """Bind the selected car to this exact nonce. Delayed requests fail after reconnecting."""
        values = {label: identity for identity, label in self.labels().items()}
        if option != "Unknown" and option not in values:
            raise HomeAssistantError("Unknown vehicle option")
        await self.write(
            "assign",
            {
                "point_id": self.identity,
                "vehicle_id": values.get(option),
                "session_id": self.row().get("actual", {}).get("session_id"),
            },
        )


class EVSensor(EVEntity, SensorEntity):
    """Expose current evidence, configuration and commands as separate readable sensor entities."""

    def __init__(self, coordinator, kind, row, key):
        """Assign native units/classes so HA dashboards distinguish power, energy, SoC and time."""
        super().__init__(coordinator, kind, row, key)
        units = {
            "power": ("W", "power"),
            "actual_power": ("W", "power"),
            "maximum_w": ("W", "power"),
            "minimum_w": ("W", "power"),
            "capacity_kwh": ("kWh", "energy"),
            "shortfall": ("kWh", "energy"),
            "predicted_soc": ("%", "battery"),
            "actual_soc": ("%", "battery"),
        }
        if key in units:
            self._attr_native_unit_of_measurement, self._attr_device_class = units[key]
            self._attr_suggested_display_precision = 0 if units[key][0] == "W" else 2
        elif key == "next_departure":
            self._attr_device_class = "timestamp"
        elif key in ("status", "control"):
            self._attr_device_class = "enum"
            self._attr_options = (
                list(EV_STATUSES) if key == "status" else ["read_only", "on_off", "variable"]
            )
            self._attr_translation_key = "ev_status" if key == "status" else "ev_control"

    @property
    def native_value(self):
        """Withhold unavailable evidence and expose zero requested watts when authorization is withdrawn."""
        row = self.row()
        if self.key == "power":
            target = command(self.coordinator, row) if row else None
            return target.power_w if target else 0
        if self.key in ("status", "control"):
            return row.get(self.key)
        if self.key == "actual_soc":
            measured = soc_reading(self.coordinator.hass, row, dt_util.utcnow()) if row else None
            return measured.value if measured else None
        if self.key == "actual_power":
            measured = power_reading(self.coordinator.hass, row, dt_util.utcnow()) if row else None
            return measured.value if measured else None
        if self.key in ("shortfall", "predicted_soc"):
            return (row.get("schedule") or {}).get(
                "shortfall_kwh" if self.key == "shortfall" else "predicted_soc"
            )
        if self.key == "next_departure":
            request = row.get("charging_request")
            return timestamp(request["departure"]) if request else None
        return row.get(self.key)


class EVCharging(EVEntity, BinarySensorEntity):
    """Current virtual permission only. Physical start/stop remains a user-owned HA automation."""

    @property
    def is_on(self):
        """Withdraw permission on paused/expired plans, unknown identity, unplug or reached target."""
        row = self.row()
        target = command(self.coordinator, row) if row else None
        return bool(target and target.power_w > 0)
