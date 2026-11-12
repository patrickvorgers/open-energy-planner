"""Small, plain wire contract. No Home Assistant, transport, ORM or solver imports."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from re import fullmatch
from uuid import UUID

from .const import RELEASE
from .wire_limits import MAX_SNAPSHOT_RESPONSE_BYTES, validate_projection_size

MAX_INSTALLATION_STEPS = 1024
MAX_CALIBRATION_INTERVAL_DAYS = 180
MIN_CALIBRATION_HOLD_HOURS = 0.25
MAX_CALIBRATION_HOLD_HOURS = 24
MAX_THERMAL_HEATER_W = 30000
THERMAL_CONFIGURATION_BOUNDS = {
    "volume_litres": (1, 10000),
    "minimum_c": (0, 120),
    "maximum_c": (1, 120),
    "ambient_c": (0, 60),
    "loss_w_per_c": (0, 1000),
    "heater_w": (1, MAX_THERMAL_HEATER_W),
}
THERMAL_PROFILE_HOURS = 24
MAX_THERMAL_HOURLY_DRAW_KWH = 100
DEFAULT_PLAN_MAX_AGE_SECONDS = 3600

MAX_PROTOCOL_IDENTIFIER_LENGTH = 128
MAX_TEXT_ATTRIBUTE_LENGTH = 160
MAX_NUMERIC_ATTRIBUTE_MAGNITUDE = 10000000.0
MAX_DOCUMENT_FIELDS = 32
MAX_COMMAND_POWER_W = 10000000


class RevokedPlan(ValueError):
    """An otherwise valid candidate belongs to an already revoked or older generation."""


def timestamp(value):
    """Parse an ISO timestamp and reject values without an explicit timezone."""
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO string")  # noqa: TRY004 - Preserve the existing ValueError validation contract.
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return result


def identifier(value):
    """Require a nonblank string identity of at most 128 characters."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_PROTOCOL_IDENTIFIER_LENGTH
    ):
        raise ValueError("Invalid identifier")
    return value


def validate_device_configuration(device):
    """Bound descriptive metadata at the peer boundary. Never pass arbitrary HA attributes into entities."""
    if "reserve_status" in device and (
        device.get("kind") != "battery"
        or device["reserve_status"]
        not in (
            "measurement_unavailable",
            "reserve_satisfied",
            "recovery_required",
            "below_reserve",
        )
    ):
        raise ValueError("Invalid reserve status")
    for flag in ("configured", "can_curtail", "can_follow_grid"):
        if flag in device and type(device[flag]) is not bool:
            raise ValueError("Invalid device capability")
    if device.get("energy_entity") is not None:
        identifier(device["energy_entity"])
    configuration = device.get("configuration", {})
    fields = {
        "source_id",
        "entity_id",
        "inverter_id",
        "capacity_w",
        "orientation",
        "tilt",
        "measurement_boundary",
        "can_curtail",
        "device_id",
        "soc_entity",
        "power_entity",
        "capacity_kwh",
        "charge_limit_w",
        "discharge_limit_w",
        "minimum_soc",
        "charge_efficiency",
        "discharge_efficiency",
        "wear_cost_per_kwh",
        "power_inverted",
        "dc_efficiency",
        "can_follow_grid",
        "recover_reserve",
        "calibration_enabled",
        "calibration_interval_days",
        "calibration_hold_hours",
        "energy_entity",
        "charge_energy_entity",
        "inverter",
        "grid_power_entity",
        "grid_power_inverted",
        "grid_export_entity",
        "import_limit_w",
        "export_limit_w",
    }
    if not isinstance(configuration, dict) or configuration.keys() - fields:
        raise ValueError("Invalid device configuration")
    for key, value in configuration.items():
        if key == "calibration_enabled" and type(value) is not bool:
            raise ValueError("Invalid calibration permission")
        if key == "calibration_interval_days" and (
            type(value) is not int or not 1 <= value <= MAX_CALIBRATION_INTERVAL_DAYS
        ):
            raise ValueError("Invalid calibration interval")
        if key == "calibration_hold_hours" and (
            type(value) not in (int, float)
            or not isfinite(value)
            or not MIN_CALIBRATION_HOLD_HOURS <= value <= MAX_CALIBRATION_HOLD_HOURS
        ):
            raise ValueError("Invalid calibration hold")
        if key == "recover_reserve" and type(value) is not bool:
            raise ValueError("Invalid reserve recovery permission")
        if key == "inverter":
            if not isinstance(value, dict) or set(value) != {
                "inverter_id",
                "ac_limit_w",
                "efficiency",
                "hybrid",
            }:
                raise ValueError("Invalid inverter configuration")
            values = value.values()
        else:
            values = (value,)
        for item in values:
            if item is None or type(item) is bool:
                continue
            if isinstance(item, str) and len(item) <= MAX_TEXT_ATTRIBUTE_LENGTH:
                continue
            if (
                type(item) in (int, float)
                and isfinite(item)
                and abs(item) <= MAX_NUMERIC_ATTRIBUTE_MAGNITUDE
            ):
                continue
            raise ValueError("Invalid device property")


@dataclass(frozen=True)
class DeviceCommand:
    """One configured virtual device's immutable target. No physical driver knowledge."""

    device_id: str
    kind: str
    power_w: int | float | None = None
    enabled: bool | None = None
    mode: str = "fixed_power"
    vehicle_id: str | None = None
    session_id: str | None = None


def device_commands(body):
    """Validate the bounded per-device target mapping before a complete plan is accepted."""
    if not isinstance(body, dict) or len(body) > MAX_DOCUMENT_FIELDS:
        raise ValueError("Invalid device targets")
    result = []
    for key, value in body.items():
        identity = identifier(key)
        if not isinstance(value, dict):
            raise TypeError("Device target requires an object")
        if value.get("kind") == "battery" and set(value) in (
            {"kind", "power_w"},
            {"kind", "power_w", "mode"},
        ):
            power = value["power_w"]
            mode = value.get("mode", "fixed_power")
            if mode not in ("fixed_power", "follow_grid_target"):
                raise ValueError("Invalid battery control mode")
            if mode == "follow_grid_target" and power is not None:
                raise ValueError("Grid following has no fixed battery target")
            if mode == "fixed_power" and (
                type(power) is not int or abs(power) > MAX_COMMAND_POWER_W
            ):
                raise ValueError("Battery target requires bounded whole watts")
            result.append(DeviceCommand(identity, "battery", power_w=power, mode=mode))
        elif (
            value.get("kind") == "solar"
            and set(value) == {"kind", "enabled"}
            and type(value["enabled"]) is bool
        ):
            result.append(DeviceCommand(identity, "solar", enabled=value["enabled"]))
        elif value.get("kind") == "ev" and set(value) == {
            "kind",
            "power_w",
            "vehicle_id",
            "session_id",
        }:
            power = value["power_w"]
            if (
                type(power) not in (int, float)
                or not isfinite(power)
                or not 0 <= power <= MAX_COMMAND_POWER_W
            ):
                raise ValueError("EV charging target requires bounded nonnegative watts")
            result.append(
                DeviceCommand(
                    identity,
                    "ev",
                    power_w=power,
                    vehicle_id=identifier(value["vehicle_id"]),
                    session_id=identifier(value["session_id"]),
                )
            )
        elif value.get("kind") == "thermal" and set(value) == {"kind", "power_w"}:
            power = value["power_w"]
            if (
                type(power) not in (int, float)
                or not isfinite(power)
                or not 0 <= power <= MAX_THERMAL_HEATER_W
                or not float(power).is_integer()
            ):
                raise ValueError("Thermal target requires bounded whole nonnegative watts")
            result.append(DeviceCommand(identity, "thermal", power_w=power))
        else:
            raise ValueError("Invalid device capability target")
    return tuple(sorted(result, key=lambda device_command: device_command.device_id))


@dataclass(frozen=True)
class Command:
    """Represent grid activation and the validated commands for each device."""

    grid_active: bool
    devices: tuple[DeviceCommand, ...] = ()

    @property
    def mode(self):
        """Expose fixed-power or grid-following mode without interpreting device-specific control."""
        return "follow_grid_target" if self.grid_active else "fixed_power"


@dataclass(frozen=True)
class Step:
    """Apply one virtual command over an aware half-open interval [start, end)."""

    start: datetime
    end: datetime
    command: Command


def validate_ev_context(context, steps):
    """Bind every EV command to one immutable car/session, time window and physical capability."""
    devices = {device.device_id for device in steps[0].command.devices if device.kind == "ev"}
    if context is None:
        if devices:
            raise ValueError("EV commands require session context")
        return
    if not isinstance(context, dict) or set(context) != {"revision", "work"}:
        raise ValueError("Invalid EV execution context")
    rows = context["work"]
    validate_projection_size(context, MAX_SNAPSHOT_RESPONSE_BYTES)
    if not isinstance(rows, list):
        raise TypeError("Invalid EV execution membership")
    if rows:
        identifier(context["revision"])
    by_point = {}
    identities = {"vehicle_id": set(), "session_id": set()}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "point_id",
            "vehicle_id",
            "session_id",
            "maximum_w",
            "minimum_w",
            "control",
            "ready_at",
            "deadline",
        }:
            raise ValueError("Invalid EV execution fields")
        point_id = identifier(row["point_id"])
        if point_id in by_point:
            raise ValueError("Duplicate EV execution point")
        by_point[point_id] = row
        for key, seen in identities.items():
            value = identifier(row[key])
            if value in seen:
                raise ValueError("Multiply connected EV execution identity")
            seen.add(value)
        if (
            any(
                type(row[key]) not in (int, float)
                or not isfinite(row[key])
                or not float(row[key]).is_integer()
                for key in ("minimum_w", "maximum_w")
            )
            or not 0 <= row["minimum_w"] <= row["maximum_w"] <= MAX_COMMAND_POWER_W
            or row["maximum_w"] <= 0
            or row["control"] not in ("on_off", "variable")
        ):
            raise ValueError("Invalid EV execution power limits")
        if timestamp(row["ready_at"]) >= timestamp(row["deadline"]):
            raise ValueError("Invalid EV execution window")
    if set(by_point) != devices:
        raise ValueError("EV commands and session context disagree")
    for step in steps:
        for command in step.command.devices:
            if command.kind != "ev":
                continue
            work = by_point[command.device_id]
            if command.vehicle_id != work["vehicle_id"] or command.session_id != work["session_id"]:
                raise ValueError("EV command identity changed")
            if command.power_w and (
                step.start < timestamp(work["ready_at"])
                or step.end > timestamp(work["deadline"])
                or not work["minimum_w"] <= command.power_w <= work["maximum_w"]
                or (work["control"] == "on_off" and command.power_w != work["maximum_w"])
            ):
                raise ValueError("EV command exceeds its physical execution window or power")


def validate_thermal_context(context, steps):
    """Bind every heater target to bounded immutable configuration and a unique Energy meter."""
    devices = {device.device_id for device in steps[0].command.devices if device.kind == "thermal"}
    if context is None:
        if devices:
            raise ValueError("Thermal commands require execution context")
        return
    if not isinstance(context, dict) or set(context) != {"revision", "devices"}:
        raise ValueError("Invalid thermal execution context")
    rows = context["devices"]
    validate_projection_size(context, MAX_SNAPSHOT_RESPONSE_BYTES)
    if not isinstance(rows, list):
        raise TypeError("Invalid thermal execution membership")
    if rows and (
        not isinstance(context["revision"], str)
        or not fullmatch(r"[0-9a-f]{64}", context["revision"])
    ):
        raise ValueError("Invalid thermal execution revision")
    fields = {
        "id",
        "energy_entity",
        "temperature_entity",
        "power_entity",
        "volume_litres",
        "minimum_c",
        "maximum_c",
        "ambient_c",
        "loss_w_per_c",
        "heater_w",
        "draw_profile_kwh",
        "enabled",
    }
    by_id, seen = (
        {},
        {key: set() for key in ("energy_entity", "temperature_entity", "power_entity")},
    )
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("Invalid thermal execution fields")
        identity = identifier(row["id"])
        if str(UUID(identity)) != identity or identity in by_id or row["enabled"] is not True:
            raise ValueError("Invalid thermal execution identity or permission")
        by_id[identity] = row
        for field, entities in seen.items():
            value = identifier(row[field])
            pattern = (
                r"(?:sensor|water_heater)\.[a-z0-9_]+"
                if field == "temperature_entity"
                else r"sensor\.[a-z0-9_]+"
            )
            if not fullmatch(pattern, value) or value in entities:
                raise ValueError("Invalid or duplicate thermal source")
            entities.add(value)
        for field, (minimum, maximum) in THERMAL_CONFIGURATION_BOUNDS.items():
            value = row[field]
            if (
                type(value) not in (int, float)
                or not isfinite(value)
                or not minimum <= value <= maximum
            ):
                raise ValueError("Invalid thermal execution quantity")
        if (
            row["minimum_c"] > row["maximum_c"]
            or row["ambient_c"] > row["maximum_c"]
            or not float(row["heater_w"]).is_integer()
        ):
            raise ValueError("Invalid thermal execution bounds")
        profile = row["draw_profile_kwh"]
        if (
            not isinstance(profile, list)
            or len(profile) != THERMAL_PROFILE_HOURS
            or any(
                type(value) not in (int, float)
                or not isfinite(value)
                or not 0 <= value <= MAX_THERMAL_HOURLY_DRAW_KWH
                for value in profile
            )
            or sum(profile) <= 0
        ):
            raise ValueError("Invalid thermal demand profile")
    if set(by_id) != devices:
        raise ValueError("Thermal commands and execution context disagree")
    for step in steps:
        for command in step.command.devices:
            if command.kind == "thermal" and command.power_w not in (
                0,
                by_id[command.device_id]["heater_w"],
            ):
                raise ValueError("Thermal command differs from fixed heater power")


@dataclass(frozen=True)
class Plan:
    """Keep a validated plan's identity, strategy, configuration and time-bounded commands."""

    plan_id: str
    generated_at: datetime
    valid_until: datetime
    config_revision: str
    strategy: str
    steps: tuple[Step, ...]

    @classmethod
    def parse(cls, document, config_revision):
        """Validate release, configuration, solution status, contiguous commands and validity.

        Fixed battery targets and thermal heater targets require bounded whole watts.
        EV targets accept finite bounded nonnegative watts, including fractional values,
        with matching vehicle/session context. Battery grid following requires a None
        fixed target. Availability and applicability are separate projection concerns.
        """
        if not isinstance(document, dict) or document.get("release") != RELEASE:
            raise ValueError("Release mismatch or invalid plan")
        if type(document.get("schema")) is not int or document["schema"] != 2:
            raise ValueError("Unsupported plan schema")
        if document.get("device_commands") is not True:
            raise ValueError("Plan schema does not match command capabilities")
        if document.get("config_revision") != config_revision:
            raise ValueError("Configuration mismatch")
        if document.get("strategy") not in ("minimize cost", "minimize consumption"):
            raise ValueError("Invalid strategy")
        if document.get("solution_status") not in ("optimal", "feasible"):
            raise ValueError("No usable candidate")
        steps = []
        raw_steps = document.get("steps")
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= (MAX_INSTALLATION_STEPS):
            raise ValueError("Invalid step count")
        for raw in raw_steps:
            if not isinstance(raw, dict):
                raise ValueError("Invalid step")  # noqa: TRY004 - Preserve the existing ValueError validation contract.
            start, end = timestamp(raw.get("start")), timestamp(raw.get("end"))
            active = raw.get("grid_active")
            if type(active) is not bool or start >= end:
                raise ValueError("Invalid interval or grid activation")
            if steps and start != steps[-1].end:
                raise ValueError("Plan intervals must be contiguous")
            devices = device_commands(raw.get("devices"))
            if (
                any(
                    device_item.kind == "battery" and device_item.mode == "follow_grid_target"
                    for device_item in devices
                )
                != active
            ):
                raise ValueError("Grid command and battery follower modes disagree")
            if steps and tuple(
                (device_item.device_id, device_item.kind) for device_item in devices
            ) != tuple(
                (device_item.device_id, device_item.kind)
                for device_item in steps[0].command.devices
            ):
                raise ValueError("Plan device capabilities changed within one revision")
            steps.append(Step(start, end, Command(active, devices)))
        validate_ev_context(document.get("ev_context"), steps)
        validate_thermal_context(document.get("thermal_context"), steps)
        made, until = (
            timestamp(document.get("generated_at")),
            timestamp(document.get("valid_until")),
        )
        if not (steps[0].start < until <= steps[-1].end and made < until):
            raise ValueError("Invalid coverage or validity")
        return cls(
            identifier(document.get("plan_id")),
            made,
            until,
            identifier(config_revision),
            document["strategy"],
            tuple(steps),
        )


@dataclass(frozen=True)
class Projection:
    """Pair operational status with a command only when local execution is currently permitted."""

    status: str
    command: Command | None


class Schedule:
    """Execute an accepted plan locally. Keep freshness separate from connection state."""

    def __init__(self, config_revision="demo-1", max_age_seconds=DEFAULT_PLAN_MAX_AGE_SECONDS):
        """Initialize local enable intent and a maximum plan age in seconds for one revision."""
        self.config_revision = config_revision
        self.max_age_seconds = max_age_seconds
        self.plan = None
        self.document = None
        self.invalidated_generation = None
        self.enabled = True
        self.resume_after = None

    def accept(self, document, now):
        """Accept a fresh complete candidate and reject ID reuse, incompatible input or older plans.

        Copy the accepted document so caller mutation cannot alter it. Return False
        for an identical already-accepted plan and True for a new accepted version.
        """
        candidate = Plan.parse(document, self.config_revision)
        if (
            self.invalidated_generation is not None
            and candidate.generated_at <= self.invalidated_generation
        ):
            raise RevokedPlan("Invalidated or older plan cannot be reactivated")
        if candidate.generated_at > now or candidate.valid_until <= now:
            raise ValueError("Future generation or expired candidate")
        if (now - candidate.generated_at).total_seconds() > self.max_age_seconds:
            raise ValueError("Candidate too old")
        if self.plan:
            if candidate.plan_id == self.plan.plan_id:
                if document != self.document:
                    raise ValueError("Plan ID reused with different contents")
                return False
            if candidate.generated_at <= self.plan.generated_at:
                raise ValueError("Older candidate cannot replace current plan")
        self.plan, self.document = candidate, deepcopy(document)
        return True

    def invalidate(self):
        """Revoke commands and require a strictly newer generation, including after recovery."""
        if self.plan and (
            self.invalidated_generation is None
            or self.plan.generated_at > self.invalidated_generation
        ):
            self.invalidated_generation = self.plan.generated_at
        self.plan = self.document = None

    def pause(self, now):
        """Disable projection immediately and record the boundary for a later explicit resume."""
        self.enabled = False
        self.resume_after = now

    def resume(self, now):
        """Enable intent while requiring a newly generated plan after this resume boundary."""
        self.enabled = True
        self.resume_after = now

    def project(self, now):
        """Return the current half-open step only while enabled, fresh and within plan validity."""
        if not self.enabled:
            return Projection("paused", None)
        if self.resume_after and (not self.plan or self.plan.generated_at <= self.resume_after):
            return Projection("waiting_for_new_plan", None)
        if not self.plan:
            return Projection("not_ready", None)
        if now >= self.plan.valid_until:
            return Projection("expired", None)
        if (now - self.plan.generated_at).total_seconds() >= self.max_age_seconds:
            return Projection("stale", None)
        for step in self.plan.steps:
            if step.start <= now < step.end:
                return Projection("active", step.command)
        return Projection("not_ready", None)

    def next_boundary(self, now):
        """Return the next step/freshness/validity boundary, or None without an enabled plan."""
        if not self.plan or not self.enabled:
            return None
        from datetime import timedelta

        values = [
            self.plan.valid_until,
            self.plan.generated_at + timedelta(seconds=self.max_age_seconds),
        ]
        values.extend(step_item.end for step_item in self.plan.steps)
        values.extend(step_item.start for step_item in self.plan.steps)
        return min((value_item for value_item in values if value_item > now), default=None)
