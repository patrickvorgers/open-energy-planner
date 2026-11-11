"""Persist measured battery calibration, independently of predicted plan outcomes."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite

from open_energy_planner.core.units import FULL_SOC_PERCENT, WATTS_PER_KW

DEFAULT_CALIBRATION_INTERVAL_DAYS = 14
DEFAULT_CALIBRATION_HOLD_HOURS = 3
MIN_CALIBRATION_HOLD_HOURS = 0.25
MAX_CALIBRATION_INTERVAL_DAYS = 365
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
                body = documents.read(CALIBRATION_DOCUMENT)
                if (
                    not isinstance(body, dict)
                    or set(body) != {"schema", "batteries"}
                    or type(body["schema"]) is not int
                    or body["schema"] != 1
                ):
                    raise ValueError("Invalid calibration ledger")
                rows = body["batteries"]
                if not isinstance(rows, dict):
                    raise ValueError("Invalid calibration batteries")
                for row in rows.values():
                    if set(row) != {
                        "definition",
                        "enabled_at",
                        "completed_at",
                        "hold_started_at",
                        "observed_at",
                    } or not isinstance(row["definition"], list):
                        raise ValueError("Invalid calibration progress")
                    policy = row["definition"]
                    if (
                        len(policy) != 6
                        or not all(isinstance(value, str) for value in policy[:2])
                        or type(policy[2]) is not bool
                        or type(policy[3]) not in (int, float)
                        or not isfinite(policy[3])
                        or policy[3] <= 0
                        or type(policy[4]) is not int
                        or not 1 <= policy[4] <= MAX_CALIBRATION_INTERVAL_DAYS
                        or type(policy[5]) not in (int, float)
                        or not isfinite(policy[5])
                        or not MIN_CALIBRATION_HOLD_HOURS <= policy[5] <= MAX_CALIBRATION_HOLD_HOURS
                    ):
                        raise ValueError("Invalid calibration policy")
                    enabled = instant(row["enabled_at"])
                    for key in ("completed_at", "hold_started_at", "observed_at"):
                        if row[key] is not None and instant(row[key]) < enabled:
                            raise ValueError("Calibration evidence precedes enabling")
                    if any(
                        row[key] is not None
                        and (
                            row["observed_at"] is None
                            or instant(row[key]) > instant(row["observed_at"])
                        )
                        for key in ("completed_at", "hold_started_at")
                    ):
                        raise ValueError("Calibration evidence exceeds its observation")
                self.state = rows
                self.persisted = deepcopy(rows)
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
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
        # Round up to a whole hour so small fluctuations cannot postpone a started charge.
        charge_hours = ceil(
            battery.capacity_kwh
            / max(available_charge_w, 1)
            * WATTS_PER_KW
            / battery.charge_efficiency
        )
        start = self.due_at(battery) - timedelta(
            hours=charge_hours + battery.calibration_hold_hours + CALIBRATION_CHARGE_MARGIN_HOURS
        )
        if now < start:
            return False, None
        if row["hold_started_at"]:
            return False, instant(row["hold_started_at"]) + timedelta(
                hours=battery.calibration_hold_hours
            )
        return True, None

    def observe(self, settings, report):
        """Advance only fresh SoC/power captures. Gaps, discharge and excessive sag restart the hold.

        Completion requires a real full-charge observation and continuous captures
        through the hold. Small SoC absorption drift is allowed without positive
        terminal discharge power. Successful natural full-charge holds also count.
        """
        if not any(battery.calibration_enabled for battery in settings.batteries):
            return
        if self.issue:
            raise OSError("Battery calibration progress unavailable")
        rows = self.configured(settings, report.captured_at)
        readings = {reading.key: reading for reading in report.readings}
        force = rows != self.state
        for battery in settings.batteries:
            if not battery.calibration_enabled:
                continue
            row = rows[battery.device_id]
            if report.captured_at < instant(row["enabled_at"]):
                continue
            previous = instant(row["observed_at"]) if row["observed_at"] else None
            if previous and report.captured_at <= previous:
                continue
            soc = readings.get(f"battery.{battery.device_id}.soc")
            power = readings.get(f"battery.{battery.device_id}.power")
            valid = (
                soc is not None
                and power is not None
                and soc.entity_id == battery.soc_entity
                and power.entity_id == battery.power_entity
                and soc.unit == "%"
                and power.unit == "W"
            )
            interrupted = (
                previous is None
                or (report.captured_at - previous).total_seconds()
                > CALIBRATION_MAX_CAPTURE_GAP_SECONDS
            )
            old_hold = row["hold_started_at"]
            if (
                not valid
                or interrupted
                or power.value > 0
                or soc.value < CALIBRATION_MINIMUM_HOLD_SOC
            ):
                row["hold_started_at"] = None
            if valid and power.value <= 0 and soc.value >= FULL_SOC_PERCENT:
                row["hold_started_at"] = row["hold_started_at"] or report.captured_at.isoformat()
            if row["hold_started_at"] and report.captured_at >= instant(
                row["hold_started_at"]
            ) + timedelta(hours=battery.calibration_hold_hours):
                row["completed_at"] = report.captured_at.isoformat()
                row["hold_started_at"] = None
                force = True
            force = force or row["hold_started_at"] != old_hold
            row["observed_at"] = report.captured_at.isoformat()
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
