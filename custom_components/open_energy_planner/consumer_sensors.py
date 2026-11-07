"""Read-only individual forecast devices. No physical commands or duplicated task state."""

import hashlib
from datetime import timedelta
from math import isfinite

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .entity import PlannerEntity
from .protocol import timestamp
from .subentries import add_grouped_entities, retire_empty_groups

MAX_STEPS = 256
MAX_METADATA_TEXT_LENGTH = 160
FORECAST_LIFETIME_MINUTES = 30
CONSUMER_ID_HEX_LENGTH = 16
WATTS_PER_KILOWATT = 1000
SUPPORTED_DURATIONS_HOURS = (0.25, 0.5, 0.75, 1)
FORECAST_STATUSES = (
    "waiting_for_history",
    "active",
    "shadow_waiting_for_decision_evidence",
    "history_or_weather_not_ready",
    "energy_consumer_removed",
    "existing_exclusions_unsupported",
    "consumer_overlaps_household_boundary",
    "energy_consumer_hierarchy_inconsistent",
    "household_boundary_missing",
)


def validate(value):
    """Bound the app projection before creating entities or parsing quantities."""
    if not isinstance(value, dict):
        raise TypeError("Invalid consumer forecasts")
    options, selected = value.get("options", []), value.get("selected", [])
    if not isinstance(options, list) or not isinstance(selected, list):
        raise TypeError("Invalid consumer selection")
    for item in (*options, *selected):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("entity_id"), str)
            or not item["entity_id"].startswith("sensor.")
            or len(item["entity_id"]) > MAX_METADATA_TEXT_LENGTH
        ):
            raise ValueError("Invalid consumer meter")
    for item in options:
        if not isinstance(item.get("name"), str) or len(item["name"]) > MAX_METADATA_TEXT_LENGTH:
            raise ValueError("Invalid consumer name")
        power_entity = item.get("power_entity")
        if power_entity is not None and (
            not isinstance(power_entity, str)
            or not power_entity.startswith("sensor.")
            or len(power_entity) > MAX_METADATA_TEXT_LENGTH
        ):
            raise ValueError("Invalid consumer power sensor")
    if any(type(item.get("weather_sensitive")) is not bool for item in selected):
        raise ValueError("Invalid consumer weather setting")
    result = value.get("result")
    if result is None:
        return value
    if not isinstance(result, dict) or result.get("status") not in FORECAST_STATUSES:
        raise ValueError("Invalid consumer status")
    records = result.get("devices", [])
    if not isinstance(records, list):
        raise TypeError("Invalid consumer forecast list")
    seen = set()
    for item in records:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("entity_id"), str)
            or item.get("entity_id") not in {row["entity_id"] for row in selected}
            or item["entity_id"] in seen
        ):
            raise ValueError("Unexpected or duplicated consumer forecast")
        if "status" in item and item["status"] not in FORECAST_STATUSES:
            raise ValueError("Invalid individual consumer status")
        seen.add(item["entity_id"])
        forecast = item["forecast"]
        if (
            not isinstance(forecast, dict)
            or not isinstance(forecast.get("model"), str)
            or len(forecast["model"]) > MAX_METADATA_TEXT_LENGTH
            or forecast.get("unit") != "kWh"
            or not isinstance(forecast.get("steps"), list)
        ):
            raise ValueError("Invalid consumer forecast")
        timestamp(forecast["issued_at"])
        if len(forecast["steps"]) > MAX_STEPS:
            raise ValueError("Too many forecast steps")
        for step in forecast["steps"]:
            timestamp(step["start"])
            if (
                any(
                    type(step[key]) not in (int, float) or not isfinite(step[key])
                    for key in ("kwh", "duration_h")
                )
                or step["kwh"] < 0
                or step["duration_h"] not in SUPPORTED_DURATIONS_HOURS
            ):
                raise ValueError("Invalid consumer forecast quantity")
    return value


class ConsumerSensor(PlannerEntity, SensorEntity):
    """Publish an explicitly estimated forecast independently of execution commands."""

    planning_group = "consumption"

    def __init__(self, coordinator, entity_id, name, key):
        """Use a stable Energy-meter identity and physical device label."""
        identity = (
            "consumer_" + hashlib.sha256(entity_id.encode()).hexdigest()[:CONSUMER_ID_HEX_LENGTH]
        )
        super().__init__(
            coordinator,
            identity,
            key,
            {
                "power": "Forecast power",
                "energy": "Forecast horizon energy",
                "status": "Forecast status",
            }[key],
        )
        self.entity_id_source, self.key = entity_id, key
        self._attr_device_info["name"] = name
        if key == "power":
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_suggested_display_precision = 0
        elif key == "energy":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        elif key == "status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(FORECAST_STATUSES)
            self._attr_translation_key = "consumer_forecast_status"

    def forecast(self):
        """Find only the current source's forecast, never another device or archived result."""
        document = self.coordinator.consumer_forecasts
        return next(
            (
                row["forecast"]
                for row in (document.get("result") or {}).get("devices", [])
                if row["entity_id"] == self.entity_id_source
            ),
            None,
        )

    @property
    def available(self):
        """Missing or expired predictions are unavailable, rather than measured zero."""
        if self.key == "status":
            return True
        forecast = self.forecast()
        return bool(
            forecast
            and timestamp(forecast["issued_at"])
            <= dt_util.utcnow()
            < timestamp(forecast["issued_at"]) + timedelta(minutes=FORECAST_LIFETIME_MINUTES)
        )

    @property
    def native_value(self):
        """Show individual readiness, the whole-horizon estimate or the current interval's mean power.

        Legacy records without status retain their aggregate result status. A
        ready device with its own status is independent of incomplete peers.
        """
        document = self.coordinator.consumer_forecasts
        if self.key == "status":
            result = document.get("result") or {}
            record = next(
                (
                    row
                    for row in result.get("devices", [])
                    if row["entity_id"] == self.entity_id_source
                ),
                {},
            )
            return record.get("status", result.get("status")) or "waiting_for_history"
        forecast = self.forecast()
        if not forecast:
            return None
        if self.key == "energy":
            return sum(step["kwh"] for step in forecast["steps"])
        now = dt_util.utcnow()
        return next(
            (
                step["kwh"] / step["duration_h"] * WATTS_PER_KILOWATT
                for step in forecast["steps"]
                if timestamp(step["start"])
                <= now
                < timestamp(step["start"]) + timedelta(hours=step["duration_h"])
            ),
            None,
        )

    @property
    def extra_state_attributes(self):
        """Separate estimated model evidence from real power and active planning preference."""
        forecast = self.forecast()
        return {
            "source_entity": self.entity_id_source,
            "model": forecast["model"] if forecast else None,
            "forecast_generated_at": forecast["issued_at"] if forecast else None,
            "estimated": True,
        }


def register_consumers(hass, entry, async_add_entities):
    """Reconcile selected Energy consumers on normal coordinator updates."""
    coordinator, entities, pending = entry.runtime_data, {}, set()

    async def remove(key, entity):
        """Remove withdrawn source entities instead of leaving silent stale forecasts."""
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        registered = registry.async_get(entity.entity_id)
        device_id = registered.device_id if registered else None
        await entity.async_remove()
        if registry.async_get(entity.entity_id):
            registry.async_remove(entity.entity_id)
        if device_id and not er.async_entries_for_device(registry, device_id):
            devices = dr.async_get(hass)
            if devices.async_get(device_id):
                devices.async_remove_device(device_id)
        entities.pop(key, None)
        pending.discard(key)
        update()
        retire_empty_groups(coordinator)

    @callback
    def update():
        """Add only currently selected, still Energy-owned consumers."""
        document = coordinator.consumer_forecasts
        names = {item["entity_id"]: item["name"] for item in document.get("options", [])}
        desired = {
            (item["entity_id"], key)
            for item in document.get("selected", [])
            if item["entity_id"] in names
            for key in ("power", "energy", "status")
        }
        for key, entity in list(entities.items()):
            if key not in desired and key not in pending:
                pending.add(key)
                entry.async_create_task(
                    hass, remove(key, entity), "Remove withdrawn consumer forecast"
                )
        added = []
        for key in desired:
            if key not in entities and key not in pending:
                entities[key] = ConsumerSensor(coordinator, key[0], names[key[0]], key[1])
                added.append(entities[key])
        if added:
            add_grouped_entities(coordinator, async_add_entities, added)

    entry.async_on_unload(coordinator.subscribe(update))
    update()
