"""Durable thermal intent and fresh telemetry, separate from physical Home Assistant control."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from uuid import UUID
from zoneinfo import ZoneInfo

from open_energy_planner.appliances.sources import replacement_selection, validate_membership
from open_energy_planner.appliances.thermal.model import (
    MAX_THERMAL_CLOCK_SKEW_SECONDS,
    MAX_THERMAL_ENTITY_ID_LENGTH,
    MAX_THERMAL_HOURLY_DRAW_KWH,
    TEMPERATURE_MAX_AGE_SECONDS,
    THERMAL_CONFIGURATION_BOUNDS,
    THERMAL_DISPATCH_LEAD_SECONDS,
    THERMAL_RUNNING_POWER_FRACTION,
    WATER_KWH_PER_LITRE_C,
    ThermalWork,
    number,
)
from open_energy_planner.core.units import HOURS_PER_DAY, SECONDS_PER_HOUR
from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.home_assistant.names import source_name
from open_energy_planner.installation.model import InstallationProblem, slice_interval
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES

STATE_KEY = "thermal-state.json"
CONFIGURATION_FIELDS = {
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


def instant(value):
    """Parse only aware ISO timestamps and normalize comparison to UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Thermal timestamps require a timezone")
    return parsed.astimezone(UTC)


def refine(problem, ready_at):
    """Give heaters quarter controls without releasing existing battery or appliance groups."""
    intervals, parents = [], []
    for parent_index, step in enumerate(problem.intervals):
        end = step.start + timedelta(hours=step.duration_h)
        boundaries = {step.start, end}
        if step.start < ready_at < end:
            boundaries.add(ready_at)
        quarter = step.start.replace(
            minute=step.start.minute // FINE_INTERVAL_MINUTES * FINE_INTERVAL_MINUTES,
            second=0,
            microsecond=0,
        )
        while quarter < end:
            if quarter > step.start:
                boundaries.add(quarter)
            quarter += timedelta(minutes=FINE_INTERVAL_MINUTES)
        for left, right in pairwise(sorted(boundaries)):
            intervals.append(
                slice_interval(
                    replace(
                        step, control_group=step.control_group or f"thermal-parent:{parent_index}"
                    ),
                    left,
                    right,
                )
            )
            parents.append(parent_index)
    return replace(
        problem,
        intervals=tuple(intervals),
        reserve_checkpoints=problem.reserve_checkpoints
        or tuple(step.start + timedelta(hours=step.duration_h) for step in problem.intervals),
        export_bonuses=tuple(
            replace(
                bonus,
                rates=tuple(bonus.rates[index] for index in parents),
                caps_kwh=tuple(bonus.caps_kwh[index] for index in parents),
                periods=tuple(bonus.periods[index] for index in parents),
                prevent_battery_export=tuple(
                    bonus.prevent_battery_export[index] for index in parents
                )
                if bonus.prevent_battery_export
                else (),
            )
            for bonus in problem.export_bonuses
        ),
    )


class ThermalService:
    """Own versioned heat-store configuration and unpersisted current observations."""

    def __init__(self, documents, consumers, appliances, ev):
        """Restore settings conservatively. Restart never restores telemetry as current evidence."""
        self.documents, self.consumers, self.appliances, self.ev = (
            documents,
            consumers,
            appliances,
            ev,
        )
        self.state = {"stores": []}
        self.issue = None
        self.configuration_unreadable = False
        self.readings, self.active_ids = {}, ()
        self.captured_at = None
        try:
            if documents.exists(STATE_KEY):
                candidate = documents.read(STATE_KEY)
                self._shape(candidate)
                self.state = candidate
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            self.configuration_unreadable = True
            self.issue = "Thermal configuration unreadable. Control is unavailable"

    @property
    def configuration_version(self):
        """Version immutable settings independently of ordinary temperature updates."""
        return digest(self.state)

    @property
    def revision(self):
        """Withdraw commands when settings or the admitted device set changes."""
        return digest([self.configuration_version, self.active_ids])

    @staticmethod
    def _shape(configuration):
        """Validate durable physical settings without needing an online HA catalogue."""
        if not isinstance(configuration, dict) or set(configuration) != {"stores"}:
            raise ValueError("Specify thermal stores")
        stores = configuration["stores"]
        if not isinstance(stores, list):
            raise TypeError("Expected thermal stores")
        for row in stores:
            if not isinstance(row, dict) or set(row) != CONFIGURATION_FIELDS:
                raise ValueError("Invalid thermal settings")
            if not isinstance(row["id"], str) or str(UUID(row["id"])) != row["id"]:
                raise ValueError("Invalid thermal identity")
            if type(row["enabled"]) is not bool:
                raise ValueError("Thermal enabled must be boolean")
            for field, (minimum, maximum) in THERMAL_CONFIGURATION_BOUNDS.items():
                number(row[field], minimum, maximum)
            if row["minimum_c"] > row["maximum_c"] or row["ambient_c"] > row["maximum_c"]:
                raise ValueError("Thermal temperatures exceed the supported planning maximum")
            if row["heater_w"] != round(row["heater_w"]):
                raise ValueError("Heater power requires whole watts")
            profile = row["draw_profile_kwh"]
            if not isinstance(profile, list) or len(profile) != HOURS_PER_DAY:
                raise ValueError("Specify 24 hourly thermal draw rates")
            for rate in profile:
                number(rate, 0, MAX_THERMAL_HOURLY_DRAW_KWH)
            if row["enabled"] and sum(profile) <= 0:
                raise ValueError("Declare expected hot-water heat demand before enabling")
            for field, domains in (
                ("energy_entity", ("sensor",)),
                ("power_entity", ("sensor",)),
                ("temperature_entity", ("sensor", "water_heater")),
            ):
                value = row[field]
                if (
                    not isinstance(value, str)
                    or len(value) > MAX_THERMAL_ENTITY_ID_LENGTH
                    or "." not in value
                ):
                    raise ValueError("Select a Home Assistant entity")
                domain, identifier = value.split(".", 1)
                if (
                    domain not in domains
                    or not identifier
                    or not identifier.isascii()
                    or not identifier.replace("_", "").isalnum()
                ):
                    raise ValueError("Invalid Home Assistant entity")
        for field in ("id", "energy_entity", "temperature_entity", "power_entity"):
            if len({row[field] for row in stores}) != len(stores):
                raise ValueError("Thermal devices and measurement bindings must be unique")

    def configure(self, configuration, catalogue):
        """Validate HA-owned bindings and disjoint energy ownership before any configuration write."""
        if self.configuration_unreadable:
            raise ValueError(
                "Thermal configuration unreadable. Restore the saved database before editing"
            )
        self._shape(configuration)
        metadata = {row["entity_id"]: row for row in (catalogue or {}).get("entities", [])}
        selected = replacement_selection(
            self.consumers.selected, self.state["stores"], configuration["stores"]
        )
        used = {row["energy_entity"] for row in self.ev.state["points"]}
        used.update(
            entity
            for entity, row in self.appliances.state["devices"].items()
            if row.get("shiftable")
        )
        for row in configuration["stores"]:
            temperature, power = (
                metadata.get(row["temperature_entity"], {}),
                metadata.get(row["power_entity"], {}),
            )
            if temperature.get("device_class") != "temperature" or temperature.get("unit") not in (
                "°C",
                "°F",
            ):
                raise ValueError("Select a temperature sensor or water heater")
            if power.get("device_class") != "power" or power.get("unit") not in ("W", "kW"):
                raise ValueError("Select the heater's electrical power sensor")
            if row["energy_entity"] in used:
                raise ValueError("A thermal meter cannot also control an EV or appliance cycle")
        validate_membership(selected, self.consumers.options)
        self.commit(configuration, selected)

    def commit(self, configuration, selected=None):
        """Archive settings and optional forecast membership before installing either in memory."""
        checked = deepcopy(configuration)
        transaction = (
            self.documents.transaction()
            if selected is None
            else self.consumers.saving_selection(
                tuple(selected), self.consumers.version, self.documents
            )
        )
        with transaction:
            self.documents.write("thermal-state-" + digest(checked) + ".json", checked)
            self.documents.write(STATE_KEY, checked)
        self.state, self.issue = checked, None
        self.configuration_unreadable = False
        self.readings, self.active_ids = {}, ()
        self.captured_at = None

    def receive(self, body, now):
        """Admit a complete bounded HA report. Missing or stale values never become zero."""
        if not isinstance(body, dict) or set(body) != {"configuration_version", "at", "readings"}:
            raise ValueError("Invalid thermal observations")
        if body["configuration_version"] != self.configuration_version:
            raise ValueError("Thermal settings changed. Refresh observations")
        reported = instant(body["at"])
        if (
            not -MAX_THERMAL_CLOCK_SKEW_SECONDS
            <= (now - reported).total_seconds()
            < TEMPERATURE_MAX_AGE_SECONDS
        ):
            raise ValueError("Thermal report expired")
        if self.captured_at is not None and reported < self.captured_at:
            raise ValueError("Thermal capture went backwards")
        rows = body["readings"]
        if not isinstance(rows, list):
            raise TypeError("Invalid thermal readings")
        allowed = {row["id"] for row in self.state["stores"]}
        admitted = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "temperature_c", "power_w", "observed_at"}
                or row["id"] not in allowed
                or row["id"] in admitted
            ):
                raise ValueError("Invalid thermal reading identity")
            number(row["temperature_c"], *THERMAL_CONFIGURATION_BOUNDS["minimum_c"])
            number(row["power_w"], 0, THERMAL_CONFIGURATION_BOUNDS["heater_w"][1])
            observed = instant(row["observed_at"])
            if observed > reported + timedelta(seconds=MAX_THERMAL_CLOCK_SKEW_SECONDS):
                raise ValueError("Thermal observation expired")
            admitted[row["id"]] = {**deepcopy(row), "captured_at": body["at"]}
        self.readings = admitted
        self.captured_at = reported

    def readiness_issue(self, row, now):
        """Explain the same physical and ownership admission rule used by the planner and UI."""
        available = {row["entity_id"] for row in self.consumers.options}
        owned = {row["energy_entity"] for row in self.ev.state["points"]}
        owned.update(
            entity
            for entity, row in self.appliances.state["devices"].items()
            if row.get("shiftable")
        )
        if row["energy_entity"] not in available:
            return (
                "Energy consumer missing. Configure this electricity meter in Home Assistant Energy"
            )
        if row["energy_entity"] in owned:
            return "This electricity meter is already controlled by an EV or appliance"
        reading = self.readings.get(row["id"])
        if (
            not reading
            or not 0
            <= (now - instant(reading["captured_at"])).total_seconds()
            < TEMPERATURE_MAX_AGE_SECONDS
        ):
            return "Fresh temperature and power required"
        if reading["temperature_c"] > row["maximum_c"]:
            return "Measured temperature exceeds the maximum planning temperature"
        return None

    def eligible(self, now):
        """Require enabled, disjoint Energy membership and fresh physical readings."""
        return [
            row
            for row in self.state["stores"]
            if row["enabled"] and self.readiness_issue(row, now) is None
        ]

    def prepare(self, snapshot, demand, now, timezone):
        """Replace controlled baselines once, then attach quarter-refined heat balances and provenance."""
        rows = self.eligible(now)
        entities = {row["energy_entity"] for row in rows}
        prepared = self.ev.prepare(snapshot, demand, now, timezone, excluded_entities=entities)
        if not isinstance(prepared.problem, InstallationProblem) or (
            entities and not prepared.appliance_evidence
        ):
            self.issue = (
                "Thermal component forecast unavailable. Retaining household baseline"
                if entities
                else self.issue
            )
            if entities:
                prepared = self.ev.prepare(snapshot, demand, now, timezone)
            rows = []
        self.active_ids = tuple(row["id"] for row in rows)
        if not rows:
            return replace(prepared, thermal_revision=self.revision)
        self.issue = None
        ready = now + timedelta(seconds=THERMAL_DISPATCH_LEAD_SECONDS)
        ready = ready.replace(second=0, microsecond=0) + (
            timedelta(minutes=1) if ready.second or ready.microsecond else timedelta()
        )
        problem = refine(prepared.problem, ready)
        zone = ZoneInfo(timezone)
        works = []
        for row in rows:
            reading = self.readings[row["id"]]
            draws = tuple(
                row["draw_profile_kwh"][step.start.astimezone(zone).hour]
                * max(
                    0,
                    (
                        step.start + timedelta(hours=step.duration_h) - max(step.start, now)
                    ).total_seconds(),
                )
                / SECONDS_PER_HOUR
                for step in problem.intervals
            )
            running = reading["power_w"] >= row["heater_w"] * THERMAL_RUNNING_POWER_FRACTION
            works.append(
                ThermalWork(
                    row["id"],
                    now if running else ready,
                    reading["temperature_c"],
                    row["minimum_c"],
                    row["maximum_c"],
                    row["ambient_c"],
                    row["volume_litres"] * WATER_KWH_PER_LITRE_C,
                    row["loss_w_per_c"],
                    row["heater_w"],
                    1,
                    draws,
                    max(row["minimum_c"], reading["temperature_c"]),
                    state_at=now,
                    initially_heating=running,
                )
            )
        return replace(
            prepared,
            problem=replace(problem, thermal_work=tuple(works)),
            thermal_revision=self.revision,
            thermal_evidence=tuple(
                {
                    "configuration": deepcopy(row),
                    "reading": deepcopy(self.readings[row["id"]]),
                    "demand_source": "configured_heat_draw_profile",
                }
                for row in rows
            ),
        )

    def document(self, now, catalogue=None, plan=None):
        """Project settings, actuals and original planned temperatures without claiming execution."""
        metadata = {row["entity_id"]: row for row in (catalogue or {}).get("entities", [])}
        devices = []
        for row in self.state["stores"]:
            reading = self.readings.get(row["id"])
            fresh = (
                reading
                and 0
                <= (now - instant(reading["captured_at"])).total_seconds()
                < TEMPERATURE_MAX_AGE_SECONDS
            )
            planned = []
            unmet_draw = None
            work = None
            if plan and plan.get("thermal_context", {}).get("revision") == self.revision:
                unmet_draws = [
                    result.get("unmet_draw_kwh", 0)
                    for flow in plan.get("expected_flows", [])
                    for result in flow.get("thermal", [])
                    if result["device_id"] == row["id"]
                ]
                unmet_draw = sum(unmet_draws) if unmet_draws else None
                work = next(
                    (
                        work
                        for work in plan.get("input_snapshot", {})
                        .get("problem", {})
                        .get("thermal_work", [])
                        if work["device_id"] == row["id"]
                    ),
                    None,
                )
                for step, flow in zip(
                    plan.get("input_snapshot", {}).get("problem", {}).get("intervals", []),
                    plan.get("expected_flows", []),
                    strict=True,
                ):
                    for result in flow.get("thermal", []):
                        if result["device_id"] == row["id"]:
                            planned.append(
                                {
                                    "start": step["start"],
                                    "end": (
                                        instant(step["start"]) + timedelta(hours=step["duration_h"])
                                    ).isoformat(),
                                    **{
                                        key: result[key]
                                        for key in ("power_w", "end_c", "shortfall_kwh")
                                    },
                                }
                            )
            source = metadata.get(row["energy_entity"], {})
            devices.append(
                {
                    **deepcopy(row),
                    "name": source_name(source, "Thermal storage"),
                    "actual": {
                        field: reading[field]
                        for field in ("id", "temperature_c", "power_w", "observed_at")
                    }
                    if fresh
                    else None,
                    "issue": self.readiness_issue(row, now),
                    "planned": planned,
                    "unmet_draw_kwh": unmet_draw,
                    "ending_target_c": work["terminal_c"] if work else None,
                    "ending_stock_shortfall_kwh": max(0, work["terminal_c"] - planned[-1]["end_c"])
                    * work["capacity_kwh_per_c"]
                    if work and planned
                    else None,
                    "comfort_shortfall_kwh": max(
                        (step["shortfall_kwh"] for step in planned), default=None
                    ),
                }
            )
        return {
            "configuration": deepcopy(self.state),
            "configuration_version": self.configuration_version,
            "revision": self.revision,
            "devices": devices,
            "issue": self.issue,
        }
