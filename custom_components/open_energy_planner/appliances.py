"""Thin device-targeted appliance actions and read-only task projection. The app owns all decisions."""

import hashlib
from datetime import date, time, timedelta
from functools import partial
from itertools import pairwise
from math import isfinite

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .consumer_sensors import CONSUMER_ID_HEX_LENGTH, ConsumerSensor
from .protocol import timestamp
from .subentries import add_grouped_entities, retire_empty_groups

MAX_CYCLE_HISTORY_HOURS = 12
FINE_STATISTIC_MINUTES = 5
MAX_CYCLE_READING_GAP_SECONDS = 900
MAX_PROFILE_PHASES = 48
MAX_CYCLE_POWER_W = 20000
MAX_CORRELATION_LENGTH = 64
PROFILE_INTERVAL_HOURS = 0.25
WATTS_PER_KILOWATT = 1000
ENERGY_UNIT_FACTORS = {"Wh": 0.001, "kWh": 1, "MWh": 1000}
TASK_STATUSES = (
    "cancelled",
    "pending",
    "scheduled",
    "requested",
    "start_not_confirmed",
    "running",
    "completed",
    "deadline_not_achievable",
    "forecast_not_ready",
)


def consumer_identity(entity):
    """Use the same stable Energy identity as existing forecast devices."""
    return "consumer_" + hashlib.sha256(entity.encode()).hexdigest()[:CONSUMER_ID_HEX_LENGTH]


def register_services(hass):
    """Register helper-free actions once. Resolve the selected virtual device to its owning entry."""
    if hass.services.has_service(DOMAIN, "appliance_ready"):
        return

    async def handle(call):
        """Validate device ownership and forward typed operations over the existing authenticated client."""
        device = dr.async_get(hass).async_get(call.data["device_id"])
        if device is None:
            raise HomeAssistantError("Unknown OEP appliance")
        coordinator = entity = None
        for entry in hass.config_entries.async_entries(DOMAIN):
            current = getattr(entry, "runtime_data", None)
            if not current:
                continue
            for row in current.consumer_forecasts.get("options", []):
                if (
                    DOMAIN,
                    entry.entry_id + "_" + consumer_identity(row["entity_id"]),
                ) in device.identifiers:
                    coordinator, entity = current, row["entity_id"]
        if coordinator is None:
            raise HomeAssistantError("Choose an Energy-owned OEP consumer device")
        try:
            if call.service == "appliance_context":
                await coordinator.sync()
                row = next(
                    (
                        item
                        for item in coordinator.appliances.get("devices", [])
                        if item["entity_id"] == entity
                    ),
                    None,
                )
                task = row.get("task") if row else None
                attempt = task.get("attempt") if task else None
                if not attempt:
                    raise ValueError("No matching execution attempt")
                return {
                    "entity_id": entity,
                    "task_id": task["id"],
                    "attempt_id": attempt["id"],
                    "status": attempt["status"],
                    "started_at": attempt.get("started_at"),
                    "issued_at": attempt["issued_at"],
                }
            body = {key: value for key, value in call.data.items() if key != "device_id"}
            body["entity_id"] = entity
            operation = {
                "appliance_ready": "tasks",
                "appliance_claim": "claim",
                "appliance_feedback": "feedback",
                "appliance_record_cycle": "cycles",
            }[call.service]
            if operation == "tasks":
                body.setdefault("ready_at", dt_util.utcnow().isoformat())
            if operation == "feedback":
                body.setdefault("at", dt_util.utcnow().isoformat())
                body.setdefault("cancelled", False)
            if operation == "cycles":
                start, end = timestamp(body["started_at"]), timestamp(body["completed_at"])
                if not timedelta(0) < end - start <= timedelta(hours=MAX_CYCLE_HISTORY_HOURS):
                    raise ValueError("Completed cycle range is invalid")
                await get_instance(hass).async_block_till_done()
                states = await get_instance(hass).async_add_executor_job(
                    partial(
                        get_significant_states,
                        hass,
                        start,
                        end + timedelta(seconds=1),
                        [entity],
                        significant_changes_only=False,
                    )
                )
                readings = states.get(entity, [])
                points = []
                for state in readings:
                    unit = state.attributes.get("unit_of_measurement")
                    if unit not in ENERGY_UNIT_FACTORS or state.state in ("unknown", "unavailable"):
                        raise ValueError("Fine cumulative cycle readings are unavailable")
                    amount = float(state.state) * ENERGY_UNIT_FACTORS[unit]
                    if not isfinite(amount):
                        raise ValueError("Invalid cumulative cycle reading")
                    points.append({"at": max(start, state.last_updated).isoformat(), "kwh": amount})
                # The real current state supplies an end observation only when it was actually updated there.
                current = hass.states.get(entity)
                if (
                    current
                    and current.last_updated >= end
                    and current.attributes.get("unit_of_measurement") in ENERGY_UNIT_FACTORS
                ):
                    points.append(
                        {
                            "at": current.last_updated.isoformat(),
                            "kwh": float(current.state)
                            * ENERGY_UNIT_FACTORS[current.attributes["unit_of_measurement"]],
                        }
                    )
                points = list({point["at"]: point for point in points}.values())
                points.sort(key=lambda point: timestamp(point["at"]))
                if (
                    len(points) < 2
                    or timestamp(points[0]["at"]) > start
                    or timestamp(points[-1]["at"]) < end
                    or any(
                        (timestamp(right["at"]) - timestamp(left["at"])).total_seconds()
                        > MAX_CYCLE_READING_GAP_SECONDS
                        for left, right in pairwise(points)
                    )
                ):
                    fine = await get_instance(hass).async_add_executor_job(
                        partial(
                            statistics_during_period,
                            hass,
                            start - timedelta(minutes=FINE_STATISTIC_MINUTES),
                            end,
                            {entity},
                            "5minute",
                            {"energy": "kWh"},
                            {"state"},
                        )
                    )
                    points = [
                        {
                            "at": (
                                dt_util.utc_from_timestamp(row["start"])
                                + timedelta(minutes=FINE_STATISTIC_MINUTES)
                            ).isoformat(),
                            "kwh": row["state"],
                        }
                        for row in fine.get(entity, [])
                        if row.get("state") is not None
                    ]
                body["points"] = points
                body["synthetic"] = bool(current and current.attributes.get("synthetic"))
            result = await coordinator.client.request("POST", "/v1/appliances/" + operation, body)
            await coordinator.sync()
            if operation == "claim":
                claimed = result.get("result") if isinstance(result, dict) else None
                if (
                    not isinstance(claimed, dict)
                    or claimed.get("authorized") is not True
                    or claimed.get("task_id") != body["task_id"]
                    or claimed.get("attempt_id") != body["attempt_id"]
                    or timestamp(claimed.get("expires_at")) <= dt_util.utcnow()
                ):
                    raise ValueError("Start claim is invalid or expired")
            return result
        except (ValueError, KeyError, TypeError) as error:
            raise HomeAssistantError(str(error)) from None

    schemas = {
        "appliance_context": {},
        "appliance_ready": {
            vol.Required("request_id"): cv.string,
            vol.Optional("ready_at"): cv.string,
            vol.Required("deadline"): cv.string,
        },
        "appliance_claim": {
            vol.Required("task_id"): cv.string,
            vol.Required("attempt_id"): cv.string,
        },
        "appliance_feedback": {
            vol.Required("task_id"): cv.string,
            vol.Required("attempt_id"): cv.string,
            vol.Required("event"): vol.In(("started", "not_started", "completed")),
            vol.Optional("at"): cv.string,
            vol.Optional("cancelled"): cv.boolean,
        },
        "appliance_record_cycle": {
            vol.Required("cycle_id"): cv.string,
            vol.Required("started_at"): cv.string,
            vol.Required("completed_at"): cv.string,
        },
    }
    for name, fields in schemas.items():
        hass.services.async_register(
            DOMAIN,
            name,
            handle,
            schema=vol.Schema({vol.Required("device_id"): cv.string, **fields}),
            supports_response=SupportsResponse.OPTIONAL,
        )


def receive(coordinator, value):
    """Validate optional task projection and emit request/expiry events once per state transition."""
    from .wire_limits import MAX_API_RESPONSE_BYTES, validate_projection_size

    validate_projection_size(value, MAX_API_RESPONSE_BYTES)
    if not isinstance(value, dict) or not isinstance(value.get("devices", []), list):
        raise TypeError("Invalid appliance projection")
    previous = getattr(coordinator, "appliances", {})
    old = {row["entity_id"]: row for row in previous.get("devices", [])}
    known = {item["entity_id"] for item in coordinator.consumer_forecasts.get("options", [])}
    rows, seen = [], set()
    for row in value.get("devices", []):
        if not isinstance(row, dict) or not isinstance(row.get("entity_id"), str):
            raise TypeError("Invalid Energy appliance")
        if row["entity_id"] not in known:
            continue
        if row["entity_id"] in seen or type(row.get("shiftable")) is not bool:
            raise ValueError("Duplicate appliance or invalid permission")
        seen.add(row["entity_id"])
        learned = row.get("profile")
        if learned is not None and (
            not isinstance(learned, dict)
            or not isinstance(learned.get("power_w"), list)
            or not 1 <= len(learned["power_w"]) <= MAX_PROFILE_PHASES
            or any(
                type(power) not in (int, float)
                or not isfinite(power)
                or not 0 <= power <= MAX_CYCLE_POWER_W
                for power in learned["power_w"]
            )
        ):
            raise ValueError("Invalid average cycle profile")
        if row.get("finish_date") is not None:
            date.fromisoformat(row["finish_date"])
        if (
            row.get("finish_time") is not None
            and time.fromisoformat(row["finish_time"]).tzinfo is not None
        ):
            raise ValueError("Invalid local finish time")
        if row.get("finish_before") is not None:
            timestamp(row["finish_before"])
        task = row.get("task")
        if task is not None:
            if (
                not isinstance(task, dict)
                or task.get("status") not in TASK_STATUSES
                or not isinstance(task.get("id"), str)
                or not 1 <= len(task["id"]) <= MAX_CORRELATION_LENGTH
            ):
                raise ValueError("Invalid appliance status")
            frozen_profile = task.get("profile")
            if frozen_profile is not None and (
                not isinstance(frozen_profile, dict)
                or not isinstance(frozen_profile.get("power_w"), list)
                or not 1 <= len(frozen_profile["power_w"]) <= MAX_PROFILE_PHASES
                or any(
                    type(power) not in (int, float)
                    or not isfinite(power)
                    or not 0 <= power <= MAX_CYCLE_POWER_W
                    for power in frozen_profile["power_w"]
                )
            ):
                raise ValueError("Invalid accepted cycle profile")
            timestamp(task["deadline"])
            if task.get("scheduled_start"):
                timestamp(task["scheduled_start"])
            attempt = task.get("attempt")
            if (
                task["status"] in ("requested", "running", "start_not_confirmed")
                and attempt is None
            ):
                raise ValueError("Execution status requires its correlated attempt")
            if attempt is not None:
                if (
                    not isinstance(attempt, dict)
                    or not isinstance(attempt.get("id"), str)
                    or not 1 <= len(attempt["id"]) <= MAX_CORRELATION_LENGTH
                ):
                    raise ValueError("Invalid appliance attempt")
                timestamp(attempt["expires_at"])
        if row.get("request"):
            request = row["request"]
            if not isinstance(request, dict):
                raise ValueError("Invalid start request")
            timestamp(request["expires_at"])
            if any(
                not isinstance(request.get(key), str)
                or not 1 <= len(request[key]) <= MAX_CORRELATION_LENGTH
                for key in ("task_id", "attempt_id")
            ):
                raise ValueError("Invalid appliance request identity")
            if (
                not task
                or not task.get("attempt")
                or request["task_id"] != task["id"]
                or request["attempt_id"] != task["attempt"]["id"]
            ):
                raise ValueError("Start request does not match its task")
        rows.append(row)
    # Validate the entire optional document before publishing any event.
    for row in rows:
        task = row.get("task")
        identity = consumer_identity(row["entity_id"])
        device = dr.async_get(coordinator.hass).async_get_device_by_identifier(
            (DOMAIN, coordinator.entry.entry_id + "_" + identity), coordinator.entry.entry_id
        )
        before = old.get(row["entity_id"], {})
        previous_task = before.get("task")
        if (
            device
            and task
            and task["status"] == "cancelled"
            and previous_task
            and previous_task["id"] == task["id"]
            and previous_task["status"] != "cancelled"
        ):
            coordinator.hass.bus.async_fire(
                DOMAIN + "_appliance_cancelled",
                {
                    "device_id": device.id,
                    "task_id": task["id"],
                    "attempt_id": (task.get("attempt") or {}).get("id"),
                    "previous_status": previous_task["status"],
                },
            )
        if (
            device
            and row.get("request")
            and dt_util.utcnow() < timestamp(row["request"]["expires_at"])
        ):
            coordinator.hass.bus.async_fire(
                DOMAIN + "_appliance_request", {"device_id": device.id, **row["request"]}
            )
        if (
            device
            and task
            and task["status"] == "start_not_confirmed"
            and (before.get("task") or {}).get("status") != "start_not_confirmed"
        ):
            persistent_notification.async_create(
                coordinator.hass,
                "The appliance start was not confirmed. Verify physical execution or confirm cancellation before retrying.",
                title="Open Energy Planner: Start not confirmed",
                notification_id=DOMAIN + ".appliance." + identity,
            )
            coordinator.hass.bus.async_fire(
                DOMAIN + "_appliance_expired",
                {
                    "device_id": device.id,
                    "task_id": task["id"],
                    "attempt_id": task["attempt"]["id"],
                },
            )
        if (
            (before.get("task") or {}).get("status") == "start_not_confirmed"
            and task
            and task["status"] != "start_not_confirmed"
        ):
            persistent_notification.async_dismiss(
                coordinator.hass, DOMAIN + ".appliance." + identity
            )
    coordinator.appliances = {**value, "devices": rows}


class ApplianceSensor(ConsumerSensor):
    """Publish task status, planned start and current start request on the existing virtual consumer."""

    def __init__(self, coordinator, entity, name, key):
        """Keep source identity unchanged when granting or withdrawing shift permission."""
        super().__init__(coordinator, entity, name, "status")
        from homeassistant.components.sensor import SensorDeviceClass

        self.key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{consumer_identity(entity)}_{key}"
        self._attr_name = {
            "task_status": "Task status",
            "planned_start": "Planned start",
            "start_request": "Start request",
            "cycle_energy": "Average cycle energy",
            "cycle_duration": "Expected cycle duration",
            "planned_completion": "Planned completion",
        }[key]
        self._attr_translation_key = "appliance_" + key
        self._attr_device_class = (
            SensorDeviceClass.TIMESTAMP
            if key in ("planned_start", "planned_completion")
            else SensorDeviceClass.ENERGY
            if key == "cycle_energy"
            else SensorDeviceClass.DURATION
            if key == "cycle_duration"
            else SensorDeviceClass.ENUM
        )
        self._attr_options = (
            list(TASK_STATUSES) + ["idle"]
            if key == "task_status"
            else ["idle", "start"]
            if key == "start_request"
            else None
        )
        self._attr_native_unit_of_measurement = (
            "kWh" if key == "cycle_energy" else "min" if key == "cycle_duration" else None
        )

    def row(self):
        """Read only this device's app-owned task projection."""
        return next(
            (
                item
                for item in self.coordinator.appliances.get("devices", [])
                if item["entity_id"] == self.entity_id_source
            ),
            {},
        )

    @property
    def available(self):
        """Task evidence remains visible during plan outages. Commands require current authorization."""
        return bool(self.row()) and (
            self.key != "start_request" or self.coordinator.projection.command is not None
        )

    @property
    def native_value(self):
        """Distinguish a current request from schedule/status facts. Never expose a writable physical switch."""
        row = self.row()
        task = row.get("task") or {}
        if self.key == "task_status":
            return task.get("status", "idle")
        if self.key == "planned_start":
            return timestamp(task["scheduled_start"]) if task.get("scheduled_start") else None
        learned = task.get("profile") or row.get("profile")
        if self.key == "cycle_duration":
            return len(learned["power_w"]) * PROFILE_INTERVAL_HOURS * 60 if learned else None
        if self.key == "planned_completion":
            return (
                timestamp(task["scheduled_start"])
                + timedelta(hours=len(learned["power_w"]) * PROFILE_INTERVAL_HOURS)
                if learned and task.get("scheduled_start")
                else None
            )
        if self.key == "start_request":
            request = row.get("request")
            return (
                "start"
                if request
                and dt_util.utcnow() < timestamp(request["expires_at"])
                and self.coordinator.projection.command is not None
                else "idle"
            )
        return (
            sum((row.get("profile") or {}).get("power_w", []))
            * PROFILE_INTERVAL_HOURS
            / WATTS_PER_KILOWATT
            if row.get("profile")
            else None
        )

    @property
    def extra_state_attributes(self):
        """Expose durable correlation for automation inspection without requiring helpers."""
        task = self.row().get("task") or {}
        attempt = task.get("attempt") or {}
        return {
            "source_entity": self.entity_id_source,
            "task_id": task.get("id"),
            "attempt_id": attempt.get("id"),
            "expires_at": attempt.get("expires_at"),
            "deadline": task.get("deadline"),
        }


def register_entities(
    hass,
    entry,
    async_add_entities,
    *,
    keys=(
        "task_status",
        "planned_start",
        "start_request",
        "cycle_energy",
        "cycle_duration",
        "planned_completion",
    ),
    factory=ApplianceSensor,
):
    """Register app-owned task sensors or supplied control factories on selected shiftable forecast devices."""
    coordinator, entities, pending_removal_keys = entry.runtime_data, {}, set()

    async def remove(key, entity):
        """Finish withdrawing an old entity before reconciling a replacement for the same key."""
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
        pending_removal_keys.discard(key)
        update()
        retire_empty_groups(coordinator)

    @callback
    def update():
        """Reconcile app-owned shift permissions without creating user helpers."""
        names = {
            item["entity_id"]: item["name"]
            for item in coordinator.consumer_forecasts.get("options", [])
        }
        desired = {
            (row["entity_id"], key)
            for row in coordinator.appliances.get("devices", [])
            if row.get("shiftable") and row["entity_id"] in names
            for key in keys
        }
        added = []
        for key in desired:
            if key not in entities and key not in pending_removal_keys:
                entities[key] = factory(coordinator, key[0], names[key[0]], key[1])
                added.append(entities[key])
        if added:
            add_grouped_entities(coordinator, async_add_entities, added)
        # The shared consumer lifecycle removes all entities when membership is withdrawn.
        for key, entity in list(entities.items()):
            if key not in desired and key not in pending_removal_keys:
                pending_removal_keys.add(key)
                entry.async_create_task(hass, remove(key, entity), "Remove appliance task entity")

    entry.async_on_unload(coordinator.subscribe(update))
    update()
