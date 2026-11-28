"""Persist measured battery calibration, independently of predicted plan outcomes."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite

from open_energy_planner.core.units import FULL_SOC_PERCENT, SECONDS_PER_HOUR, WATTS_PER_KW

DEFAULT_CALIBRATION_INTERVAL_DAYS = 14
DEFAULT_CALIBRATION_HOLD_HOURS = 3
MIN_CALIBRATION_HOLD_HOURS = 0.25
MAX_CALIBRATION_INTERVAL_DAYS = 180
MAX_CALIBRATION_HOLD_HOURS = 24
CALIBRATION_MINIMUM_HOLD_SOC = 98.0
CALIBRATION_MAX_CAPTURE_GAP_SECONDS = 300
CALIBRATION_CHECKPOINT_SECONDS = 60
CALIBRATION_DOCUMENT = "battery-calibration.json"
CALIBRATION_CHARGE_MARGIN_HOURS = 0.25


def instant(value):
    """Decode an aware ledger timestamp without accepting naive calendar times."""
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Calibration timestamps require a timezone")
    return parsed.astimezone(UTC)


def definition(battery):
    """Bind progress to its physical meter, capacity and enabled calibration policy."""
    return [
        battery.soc_entity,
        battery.power_entity,
        battery.power_inverted,
        battery.capacity_kwh,
        battery.calibration_interval_days,
        battery.calibration_hold_hours,
    ]


def available_charge_w(settings, battery, converter):
    """Bound deadline preparation by terminal, converter and shared connection power in watts."""
    return min(
        battery.charge_limit_w,
        converter.ac_limit_w
        * converter.efficiency
        * battery.dc_efficiency
        / sum(unit.inverter_id == battery.inverter_id for unit in settings.batteries),
        settings.import_limit_w
        * converter.efficiency
        * battery.dc_efficiency
        / max(1, len(settings.batteries)),
    )


def _restore_ledger(body):
    """Check the durable envelope, then validate every row before making it available."""
    error_message = "Invalid calibration ledger"
    if not isinstance(body, dict) or set(body) != {"schema", "batteries"}:
        raise ValueError(error_message)
    if type(body["schema"]) is not int or body["schema"] != 1:
        raise ValueError(error_message)
    rows = body["batteries"]
    if not isinstance(rows, dict):
        raise TypeError("Invalid calibration batteries")
    for row in rows.values():
        _validate_ledger_row(row)
    return rows


def _validate_ledger_row(row):
    """Require the stored fields and validate the frozen policy before reading its evidence."""
    if set(row) != {
        "definition",
        "enabled_at",
        "completed_at",
        "hold_started_at",
        "observed_at",
    } or not isinstance(row["definition"], list):
        raise ValueError("Invalid calibration progress")
    interval_days, hold_hours = _validate_ledger_policy(row["definition"])
    _validate_ledger_evidence(row, interval_days, hold_hours)


def _validate_ledger_policy(policy):
    """Validate the positional policy in its persisted shape and return its durations."""
    error_message = "Invalid calibration policy"
    if len(policy) != 6 or not all(isinstance(value, str) for value in policy[:2]):
        raise ValueError(error_message)
    if type(policy[2]) is not bool:
        raise ValueError(error_message)
    capacity, interval_days, hold_hours = policy[3:]
    if type(capacity) not in (int, float) or not isfinite(capacity) or capacity <= 0:
        raise ValueError(error_message)
    if type(interval_days) is not int or not 1 <= interval_days <= MAX_CALIBRATION_INTERVAL_DAYS:
        raise ValueError(error_message)
    if (
        type(hold_hours) not in (int, float)
        or not isfinite(hold_hours)
        or not MIN_CALIBRATION_HOLD_HOURS <= hold_hours <= MAX_CALIBRATION_HOLD_HOURS
    ):
        raise ValueError(error_message)
    return interval_days, hold_hours


def _validate_ledger_evidence(row, interval_days, hold_hours):
    """Require ordered aware evidence and deadlines representable after a restart."""
    enabled = instant(row["enabled_at"])
    enabled + timedelta(days=interval_days)
    times = {}
    for field in ("completed_at", "hold_started_at", "observed_at"):
        if row[field] is None:
            continue
        value = instant(row[field])
        if value < enabled:
            raise ValueError("Calibration evidence precedes enabling")
        times[field] = value

    completed = times.get("completed_at")
    started = times.get("hold_started_at")
    observed = times.get("observed_at")
    # Check future deadline arithmetic before the planner or progress display can use it.
    if completed is not None:
        completed + timedelta(days=interval_days)
    if started is not None:
        started + timedelta(hours=hold_hours)
    if any(
        transition is not None and (observed is None or transition > observed)
        for transition in (completed, started)
    ):
        raise ValueError("Calibration evidence exceeds its observation")


def _matching_capture_pair(battery, soc, power, captured_at):
    """Require bound readings in the expected units and fresh explicit device samples."""
    if soc is None or power is None:
        return False
    if (soc.entity_id, power.entity_id) != (battery.soc_entity, battery.power_entity):
        return False
    if (soc.unit, power.unit) != ("%", "W"):
        return False
    return all(
        reading.timestamp_kind != "device_sample"
        or 0
        <= (captured_at - reading.observed_at).total_seconds()
        <= CALIBRATION_MAX_CAPTURE_GAP_SECONDS
        for reading in (soc, power)
    )


def _hold_interrupted(soc, power, previous, captured_at):
    """A missing capture, actual discharge or large SoC sag invalidates an existing hold."""
    if previous is None:
        return True
    if (captured_at - previous).total_seconds() > CALIBRATION_MAX_CAPTURE_GAP_SECONDS:
        return True
    return power.value > 0 or soc.value < CALIBRATION_MINIMUM_HOLD_SOC


def _complete_hold(row, captured_at, hold_hours):
    """Record completion only when the current capture reaches the continuous hold deadline."""
    started = row["hold_started_at"]
    if not started or captured_at < instant(started) + timedelta(hours=hold_hours):
        return False
    row["completed_at"] = captured_at.isoformat()
    row["hold_started_at"] = None
    return True


class BatteryCalibration:
    """Own one bounded active-database ledger, with durable transitions and minute checkpoints.

    The application state lock serializes callers. Observation writes run through
    the app's cancellation-draining worker. A missing interval never establishes
    completion, including across restart. Predicted full charge is not evidence.
    """

    def __init__(self, documents):
        """Restore finite, explicitly versioned progress or expose corruption as a blocking issue."""
        self.documents, self.state, self.issue = documents, {}, None
        self.persisted = {}
        try:
            if documents.exists(CALIBRATION_DOCUMENT):
                self.state = _restore_ledger(documents.read(CALIBRATION_DOCUMENT))
                self.persisted = deepcopy(self.state)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
            self.issue = "battery_calibration_unreadable"

    def configured(self, settings, now):
        """Return reconciled progress, preserving unrelated edits and clearing disabled policies."""
        rows = {}
        for battery in settings.batteries:
            if battery.calibration_enabled:
                previous = self.state.get(battery.device_id)
                rows[battery.device_id] = (
                    deepcopy(previous)
                    if previous and previous["definition"] == definition(battery)
                    else {
                        "definition": definition(battery),
                        "enabled_at": now.isoformat(),
                        "completed_at": None,
                        "hold_started_at": None,
                        "observed_at": None,
                    }
                )
        return rows

    def write(self, rows):
        """Write a finite replacement inside the caller's existing transaction when present."""
        self.documents.write(CALIBRATION_DOCUMENT, {"schema": 1, "batteries": rows})

    def install(self, rows):
        """Publish progress only after the complete persistence transaction succeeds."""
        self.state, self.persisted = rows, deepcopy(rows)

    def due_at(self, battery):
        """Return the maximum elapsed-time interval, without inventing historical success."""
        row = self.state[battery.device_id]
        return instant(row["completed_at"] or row["enabled_at"]) + timedelta(
            days=battery.calibration_interval_days
        )

    @property
    def command_revision(self):
        """Identify hold/completion transitions, excluding routine capture checkpoints."""
        return tuple(
            (identity, row["hold_started_at"], row["completed_at"])
            for identity, row in sorted(self.state.items())
        )

    def directive(self, battery, now, available_charge_w):
        """Start early enough for rated charging and the hold, retaining overdue work until measured completion."""
        if not battery.calibration_enabled:
            return False, None
        if self.issue or battery.device_id not in self.state:
            raise ValueError("Battery calibration progress unavailable")
        row = self.state[battery.device_id]
        if row["hold_started_at"]:
            return False, instant(row["hold_started_at"]) + timedelta(
                hours=battery.calibration_hold_hours
            )
        # Round up to a whole hour so small fluctuations cannot postpone a started charge.
        charge_hours = (
            battery.capacity_kwh
            / max(available_charge_w, 1)
            * WATTS_PER_KW
            / battery.charge_efficiency
        )
        remaining_charge_hours = (
            (self.due_at(battery) - now).total_seconds() / SECONDS_PER_HOUR
            - battery.calibration_hold_hours
            - CALIBRATION_CHARGE_MARGIN_HOURS
        )
        if charge_hours >= remaining_charge_hours:
            return True, None
        return ceil(charge_hours) >= remaining_charge_hours, None

    def observe(self, settings, report):
        """Advance only fresh SoC/power captures. Gaps, discharge and excessive sag restart the hold.

        Completion requires a real full-charge observation and continuous captures
        through the hold. Small SoC absorption drift is allowed without positive
        terminal discharge power. Explicit device samples must be at most 300 seconds
        old. Ordinary HA state/report timestamps remain provenance rather than a
        heartbeat. Successful natural full-charge holds also count.
        """
        if not any(battery.calibration_enabled for battery in settings.batteries):
            return
        if self.issue:
            raise OSError("Battery calibration progress unavailable")
        rows = self.configured(settings, report.captured_at)
        readings = {reading.key: reading for reading in report.readings}
        converters = {converter.inverter_id: converter for converter in settings.inverters}

        def charging_power(battery):
            """Read rated capacity only when evaluating the battery's next charging window."""
            return available_charge_w(settings, battery, converters[battery.inverter_id])

        force = rows != self.state
        for battery in settings.batteries:
            if not battery.calibration_enabled:
                continue
            if self._observe_battery(
                battery, rows[battery.device_id], readings, report.captured_at, charging_power
            ):
                force = True
        checkpoint = any(
            not self.persisted.get(key, {}).get("observed_at")
            or (report.captured_at - instant(self.persisted[key]["observed_at"])).total_seconds()
            >= CALIBRATION_CHECKPOINT_SECONDS
            for key in rows
        )
        if force or checkpoint:
            self.write(rows)
            self.install(rows)
        else:
            self.state = rows

    def _observe_battery(self, battery, row, readings, captured_at, charging_power):
        """Update one detached row and report whether its hold or completion changed."""
        if captured_at < instant(row["enabled_at"]):
            return False
        previous = instant(row["observed_at"]) if row["observed_at"] else None
        if previous and captured_at <= previous:
            return False
        soc = readings.get(f"battery.{battery.device_id}.soc")
        power = readings.get(f"battery.{battery.device_id}.power")
        valid = _matching_capture_pair(battery, soc, power, captured_at)
        old_hold = row["hold_started_at"]
        if not valid or _hold_interrupted(soc, power, previous, captured_at):
            row["hold_started_at"] = None

        start_allowed = (
            row["completed_at"] is None
            or old_hold is not None
            or self.directive(battery, captured_at, charging_power(battery))[0]
        )
        full_capture = valid and power.value <= 0 and soc.value >= FULL_SOC_PERCENT
        if full_capture and start_allowed:
            row["hold_started_at"] = row["hold_started_at"] or captured_at.isoformat()
        completed = _complete_hold(row, captured_at, battery.calibration_hold_hours)
        row["observed_at"] = captured_at.isoformat()
        return completed or row["hold_started_at"] != old_hold

    def document(self, battery, now):
        """Expose readable progress separately from editable installation definitions."""
        if not battery.calibration_enabled:
            return {"status": "disabled"}
        row = self.state.get(battery.device_id)
        if self.issue or row is None:
            return {"status": "unavailable"}
        hold_end = (
            instant(row["hold_started_at"]) + timedelta(hours=battery.calibration_hold_hours)
            if row["hold_started_at"]
            else None
        )
        return {
            "status": "overdue"
            if now >= self.due_at(battery)
            else "holding"
            if hold_end
            else "scheduled",
            "due_at": self.due_at(battery).isoformat(),
            "completed_at": row["completed_at"],
            "hold_until": hold_end.isoformat() if hold_end else None,
        }
