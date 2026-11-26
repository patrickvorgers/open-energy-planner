"""Durable thermal intent and fresh telemetry, separate from physical Home Assistant control."""

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from logging import getLogger
from uuid import UUID
from zoneinfo import ZoneInfo

from open_energy_planner.appliances.thermal.applications import (
    MAXIMUM_SPACE_TEMPERATURE_C,
    UNKNOWN_MODE,
    ApplicationKind,
    application_documents,
    application_meters,
    attribute_history,
    forecast_selection,
    shared_sources,
    validate_choices,
    validate_shape,
)
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
from open_energy_planner.appliances.thermal.room import (
    HOUR,
    INDOOR_HISTORY_HOURS,
    fit_room,
    room_work,
)
from open_energy_planner.core.units import HOURS_PER_DAY, SECONDS_PER_HOUR
from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.home_assistant.names import source_name
from open_energy_planner.installation.model import InstallationProblem, slice_interval
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES

LOGGER = getLogger(__name__)
STATE_KEY = "thermal-state.json"
SOURCE_MODES_KEY = "thermal-source-modes.json"
SOURCE_MODE_HISTORY_DAYS = 30
MAX_SOURCE_MODE_SAMPLES = 4096
ROOM_HISTORY_KEY = "space-heating-history.json"
ROOM_HOURLY_CAPTURE_SECONDS = 120
ROOM_TARGET_ECHO_TOLERANCE_C = 0.01
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


def _admit_boiler_readings(rows, allowed, reported, captured_at):
    """Validate one complete capture of configured boiler readings."""
    if not isinstance(rows, list):
        raise TypeError("Invalid thermal readings")
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
        admitted[row["id"]] = {**deepcopy(row), "captured_at": captured_at}
    return admitted


def _validate_climate_observation(row, captured):
    """Admit bounded temperatures and reject observations ahead of the capture."""
    for field in ("temperature_c", "target_c"):
        number(row[field], 0, MAXIMUM_SPACE_TEMPERATURE_C)
    observed = instant(row["observed_at"])
    if observed > captured + timedelta(seconds=MAX_THERMAL_CLOCK_SKEW_SECONDS):
        raise ValueError("Thermal observation expired")


def _stored_boiler_plan(plan, revision, identity):
    """Project planned boiler intervals from the plan with the matching Thermal revision."""
    planned = []
    unmet_draw = None
    work = None
    if plan and plan.get("thermal_context", {}).get("revision") == revision:
        unmet_draws = [
            result.get("unmet_draw_kwh", 0)
            for flow in plan.get("expected_flows", [])
            for result in flow.get("thermal", [])
            if result["device_id"] == identity
        ]
        unmet_draw = sum(unmet_draws) if unmet_draws else None
        work = next(
            (
                work
                for work in plan.get("input_snapshot", {})
                .get("problem", {})
                .get("thermal_work", [])
                if work["device_id"] == identity
            ),
            None,
        )
        for step, flow in zip(
            plan.get("input_snapshot", {}).get("problem", {}).get("intervals", []),
            plan.get("expected_flows", []),
            strict=True,
        ):
            for result in flow.get("thermal", []):
                if result["device_id"] == identity:
                    planned.append(
                        {
                            "start": step["start"],
                            "end": (
                                instant(step["start"]) + timedelta(hours=step["duration_h"])
                            ).isoformat(),
                            **{key: result[key] for key in ("power_w", "end_c", "shortfall_kwh")},
                        }
                    )
    return planned, unmet_draw, work


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


def validate_boiler_settings(row):
    """Validate one saved boiler's bounds, permissions and expected draw."""
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


def validate_boiler_bindings(row):
    """Check saved boiler entity IDs independently of their current HA availability."""
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


def boiler_work(row, reading, intervals, now, ready, zone):
    """Build one boiler's inherited dispatch state and remaining hourly draw."""
    draws = tuple(
        row["draw_profile_kwh"][step.start.astimezone(zone).hour]
        * max(
            0,
            (step.start + timedelta(hours=step.duration_h) - max(step.start, now)).total_seconds(),
        )
        / SECONDS_PER_HOUR
        for step in intervals
    )
    running = reading["power_w"] >= row["heater_w"] * THERMAL_RUNNING_POWER_FRACTION
    return ThermalWork(
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
        self.climate_readings = {}
        self.active_rooms = ()
        self.echoed_targets = {}
        self.room_history = {}
        self.captured_at = None
        self.source_modes = {}
        self.source_mode_history = {}
        try:
            if documents.exists(ROOM_HISTORY_KEY):
                self.room_history = self.restore_room_history(documents.read(ROOM_HISTORY_KEY))
        except (OSError, TypeError, ValueError, OverflowError):
            LOGGER.warning("Space heating history unreadable. Rebuilding from new observations")
        try:
            if documents.exists(STATE_KEY):
                candidate = documents.read(STATE_KEY)
                self._shape(candidate)
                self.state = candidate
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            self.configuration_unreadable = True
            self.issue = "Thermal configuration unreadable. Control is unavailable"
        try:
            if documents.exists(SOURCE_MODES_KEY):
                history = documents.read(SOURCE_MODES_KEY)
                if isinstance(history, dict) and all(
                    isinstance(rows, list) for rows in history.values()
                ):
                    self.source_mode_history = history
        except (OSError, ValueError, TypeError):
            pass

    def report_mode(self, meter, application_id, now):
        """Commit observed mode without changing thermal permission or scheduling intent."""
        if not isinstance(meter, str):
            raise TypeError(UNKNOWN_MODE)
        applications = application_documents(self.state, {})
        source = shared_sources(applications).get(meter)
        if source is None or (
            application_id is not None
            and application_id not in {row["id"] for row in source["options"]}
        ):
            raise ValueError(UNKNOWN_MODE)
        mode = {"application_id": application_id, "observed_at": now.isoformat()}
        oldest = (now - timedelta(days=SOURCE_MODE_HISTORY_DAYS)).isoformat()
        history = {
            identity: [
                sample
                for sample in samples
                if isinstance(sample, dict)
                and isinstance(sample.get("observed_at"), str)
                and sample["observed_at"] >= oldest
            ]
            for identity, samples in self.source_mode_history.items()
        }
        history[meter] = [*history.get(meter, []), mode][-MAX_SOURCE_MODE_SAMPLES:]
        history = {identity: rows for identity, rows in history.items() if rows}
        self.documents.write(SOURCE_MODES_KEY, history)
        self.source_mode_history = history
        self.source_modes = {**self.source_modes, meter: mode}

    def mode_attribution(self, sources, now):
        """Summarize metered energy separately from forecasting and current mode reporting."""
        result = {}
        start = now - timedelta(days=SOURCE_MODE_HISTORY_DAYS)
        validity = timedelta(seconds=TEMPERATURE_MAX_AGE_SECONDS)
        for meter, source in sources.items():
            if (history := self.consumers.histories.get(meter)) is None:
                continue
            samples = self.source_mode_history.get(meter, ())
            result[meter] = attribute_history(
                history,
                samples,
                {option["id"] for option in source["options"]},
                start,
                validity,
                limit_to_retained=len(samples) >= MAX_SOURCE_MODE_SAMPLES,
            )
        return result

    @property
    def configuration_version(self):
        """Version immutable settings independently of ordinary temperature updates."""
        return digest(self.state)

    @property
    def revision(self):
        """Withdraw commands when settings or the admitted device set changes."""
        if self.active_rooms:
            return digest(
                [
                    self.configuration_version,
                    self.active_ids,
                    [
                        (identity, self.climate_readings.get(identity, {}).get("target_c"))
                        for identity in self.active_rooms
                    ],
                ]
            )
        return digest([self.configuration_version, self.active_ids])

    @property
    def meters(self):
        """Return the Energy identities reserved by applications and configured water heaters."""
        return application_meters(self.state) | {
            row["energy_entity"] for row in self.state["stores"]
        }

    @property
    def occupied_meters(self):
        """Return Energy meters already assigned to EV charging or a shiftable cycle."""
        return {row["energy_entity"] for row in self.ev.state["points"]} | {
            entity
            for entity, row in self.appliances.state["devices"].items()
            if row.get("shiftable")
        }

    @staticmethod
    def _shape(configuration):
        """Validate durable physical settings without needing an online HA catalogue."""
        if not isinstance(configuration, dict) or set(configuration) not in (
            {"stores"},
            {"stores", "applications"},
        ):
            raise ValueError("Specify thermal stores")
        validate_shape(configuration.get("applications", []))
        stores = configuration["stores"]
        if not isinstance(stores, list):
            raise TypeError("Expected thermal stores")
        for row in stores:
            validate_boiler_settings(row)
            validate_boiler_bindings(row)
        for field in ("id", "energy_entity", "temperature_entity", "power_entity"):
            if len({row[field] for row in stores}) != len(stores):
                raise ValueError("Thermal devices and measurement bindings must be unique")
        if {row["id"] for row in stores} & {
            row["id"] for row in configuration.get("applications", [])
        }:
            raise ValueError("Thermal devices and measurement bindings must be unique")

    def configure(self, configuration, catalogue):
        """Validate HA-owned bindings and disjoint energy ownership before any configuration write."""
        if self.configuration_unreadable:
            raise ValueError(
                "Thermal configuration unreadable. Restore the saved database before editing"
            )
        self._shape(configuration)
        catalogue = catalogue or {}
        validate_choices(configuration, self.state, catalogue)
        if configuration.get("applications"):
            configuration = deepcopy(configuration)
            area_names = {area["id"]: area["name"] for area in catalogue.get("areas", [])}
            for application in configuration["applications"]:
                application["name"] = area_names.get(application["area_id"], application["name"])
        metadata = {row["entity_id"]: row for row in catalogue.get("entities", [])}
        used = self.occupied_meters
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
        selected = forecast_selection(
            configuration, self.state, self.consumers.selected, self.consumers.options, used
        )
        self.commit(configuration, selected)

    def commit(self, configuration, selected=None):
        """Archive settings and optional forecast membership before installing either in memory."""
        checked = deepcopy(configuration)
        before = shared_sources(application_documents(self.state, {}))
        after = shared_sources(application_documents(checked, {}))
        current_modes = {
            meter: mode
            for meter, mode in self.source_modes.items()
            if meter in before
            and meter in after
            and {option["id"] for option in before[meter]["options"]}
            == {option["id"] for option in after[meter]["options"]}
        }
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
        self.climate_readings = {}
        self.active_rooms = ()
        self.echoed_targets = {}
        self.captured_at = None
        self.source_modes = current_modes

    def receive(self, body, now, plan=None):
        """Admit a complete bounded HA report. Missing or stale values never become zero."""
        if not isinstance(body, dict) or set(body) - {"climate"} != {
            "configuration_version",
            "at",
            "readings",
        }:
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
        admitted = _admit_boiler_readings(
            body["readings"], {row["id"] for row in self.state["stores"]}, reported, body["at"]
        )
        climate = self.receive_climate(body.get("climate", []), reported)
        self.record_room_history(climate, reported)
        self.discard_echoed_targets(climate, plan)
        self.readings, self.climate_readings = admitted, climate
        self.captured_at = reported

    def receive_climate(self, rows, captured):
        """Admit current thermostat values from an already validated integration capture."""
        if not isinstance(rows, list):
            raise TypeError("Invalid thermal readings")
        allowed = {
            application["id"]: application["climate_entity"]
            for application in self.state.get("applications", [])
            if application["kind"] == ApplicationKind.SPACE_HEATING
            and application["climate_entity"]
        }
        admitted = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) - {"heat_mode"}
                != {"id", "entity_id", "temperature_c", "target_c", "observed_at"}
                or type(row.get("heat_mode", False)) is not bool
                or row["id"] not in allowed
                or row["id"] in admitted
                or row["entity_id"] != allowed[row["id"]]
            ):
                raise ValueError("Invalid thermal reading identity")
            _validate_climate_observation(row, captured)
            admitted[row["id"]] = {**row, "captured_at": captured.isoformat()}
        return admitted

    def discard_echoed_targets(self, climate, plan):
        """Withhold repeated preheat when an automation feeds a virtual target into its reference."""
        settings = (plan or {}).get("thermal_context", {}).get("devices", [])
        produced = {
            item["id"]: item["target_c"] + item["maximum_preheat_c"]
            for item in settings
            if item.get("kind") == "space_heating"
        }
        for identity, reading in tuple(climate.items()):
            measured = reading["target_c"]
            if any(
                target is not None and abs(target - measured) < ROOM_TARGET_ECHO_TOLERANCE_C
                for target in (produced.get(identity), self.echoed_targets.get(identity))
            ):
                self.echoed_targets[identity] = measured
                climate.pop(identity)
            else:
                self.echoed_targets.pop(identity, None)

    @staticmethod
    def restore_room_history(document):
        """Restore historical hour-boundary samples without creating current telemetry."""
        if not isinstance(document, dict):
            raise TypeError("Invalid room history")
        history = {}
        for identity, samples in document.items():
            if not isinstance(samples, dict):
                raise TypeError("Invalid indoor temperature samples")
            history[identity] = {
                instant(stamp): number(value, 0, MAXIMUM_SPACE_TEMPERATURE_C)
                for stamp, value in samples.items()
            }
        return history

    @staticmethod
    def room_history_id(application):
        """Keep calibration evidence scoped to the original room, climate and source links."""
        return digest(
            [
                application["id"],
                application["area_id"],
                application["climate_entity"],
                sorted(row["energy_entity"] for row in application["sources"]),
            ]
        )

    def record_room_history(self, readings, captured):
        """Save at most one trustworthy indoor sample per UTC hour and physical binding."""
        hour = captured.replace(minute=0, second=0, microsecond=0)
        if not readings or (captured - hour).total_seconds() > ROOM_HOURLY_CAPTURE_SECONDS:
            return
        cutoff = hour - INDOOR_HISTORY_HOURS * HOUR
        history = {
            identity: {stamp: value for stamp, value in samples.items() if stamp >= cutoff}
            for identity, samples in self.room_history.items()
        }
        updated = False
        for application in self.state.get("applications", []):
            reading = readings.get(application["id"])
            if (
                not reading
                or abs((instant(reading["observed_at"]) - hour).total_seconds())
                > ROOM_HOURLY_CAPTURE_SECONDS
            ):
                continue
            samples = history.setdefault(self.room_history_id(application), {})
            if hour not in samples:
                samples[hour] = reading["temperature_c"]
                updated = True
        if updated:
            self.documents.write(
                ROOM_HISTORY_KEY,
                {
                    identity: {stamp.isoformat(): value for stamp, value in samples.items()}
                    for identity, samples in history.items()
                    if samples
                },
            )
            self.room_history = history

    def fresh_climate(self, identity, now):
        """Read only current reported temperatures and setpoints for control admission."""
        reading = self.climate_readings.get(identity)
        if (
            reading
            and 0
            <= (now - instant(reading["captured_at"])).total_seconds()
            < TEMPERATURE_MAX_AGE_SECONDS
        ):
            return reading
        return None

    def space_heating_status(self, application, now):
        """Explain forecast-only operation and expose the effective cap from a fresh HA target."""
        if application["kind"] != ApplicationKind.SPACE_HEATING:
            return {}
        reading = self.fresh_climate(application["id"], now)
        allows = any(source["allow_scheduling"] for source in application["sources"])
        return {
            "actual": deepcopy(reading),
            "preheat_limit_c": reading["target_c"] + application["maximum_preheat_c"]
            if reading and allows
            else None,
            "planning_status": (
                "Forecast only"
                if not allows
                else "Forecast only. Active planning requires one independent heat source."
                if len(application["sources"]) != 1
                else "Forecast only. Enter the heat source's electrical input at full load."
                if "full_load_power_w" not in application["sources"][0]
                else "Waiting for a current thermostat target"
                if not reading
                else "Included in planning"
                if application["id"] in self.active_rooms
                else "Waiting for validated thermal evidence or an unshared source"
            ),
        }

    def readiness_issue(self, row, now):
        """Explain the same physical and ownership admission rule used by the planner and UI."""
        available = {row["entity_id"] for row in self.consumers.options}
        if row["energy_entity"] not in available:
            return (
                "Energy consumer missing. Configure this electricity meter in Home Assistant Energy"
            )
        if row["energy_entity"] in self.occupied_meters:
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

    def _exclusive_room_meters(self, applications):
        """Select Energy meters with a single Thermal binding and no other owner."""
        options = {row["entity_id"] for row in self.consumers.options}
        bindings = Counter(
            source["energy_entity"] for app in applications for source in app["sources"]
        )
        reserved = self.occupied_meters | {row["energy_entity"] for row in self.state["stores"]}
        return {meter for meter in options - reserved if bindings[meter] == 1}

    def room_candidates(self, problem, demand, now, ready):
        """Admit a single identified heater per room, with an exclusive physical meter."""
        if (
            not isinstance(problem, InstallationProblem)
            or not getattr(demand, "live_weather_until", None)
            or now >= demand.live_weather_until
        ):
            return (), {}
        applications = self.state.get("applications", [])
        exclusive_meters = self._exclusive_room_meters(applications)
        outdoor = {row.start: row.celsius for row in demand.live_temperatures}
        intervals = refine(problem, ready).intervals
        candidates = []
        for app in applications:
            if app["kind"] != ApplicationKind.SPACE_HEATING or len(app["sources"]) != 1:
                continue
            source = app["sources"][0]
            meter = source["energy_entity"]
            if not source["allow_scheduling"] or meter not in exclusive_meters:
                continue
            reading = self.fresh_climate(app["id"], now)
            if not reading or not reading.get("heat_mode"):
                continue
            model = fit_room(
                self.room_history.get(self.room_history_id(app), {}),
                demand.temperatures,
                self.consumers.histories.get(meter),
                now,
                full_load_power_w=source.get("full_load_power_w"),
            )
            if model and room_work(app, reading, model, outdoor, intervals, now, ready):
                candidates.append((app, reading, model, meter))
        return tuple(candidates), outdoor

    def prepare(self, snapshot, demand, now, timezone):
        """Replace controlled baselines once, then attach quarter-refined heat balances and provenance."""
        rows = self.eligible(now)
        dispatch = now + timedelta(seconds=THERMAL_DISPATCH_LEAD_SECONDS)
        ready = dispatch.replace(second=0, microsecond=0) + (
            timedelta(minutes=1) if dispatch.second or dispatch.microsecond else timedelta()
        )
        rooms, weather = self.room_candidates(snapshot.problem, demand, now, ready)
        entities = {row["energy_entity"] for row in rows} | {row[3] for row in rooms}
        windows = ()
        if rooms:
            final = snapshot.problem.intervals[-1]
            end = final.start + timedelta(hours=final.duration_h)
            windows = tuple((room[3], ready, end) for room in rooms)
        prepared = self.ev.prepare(
            snapshot, demand, now, timezone, excluded_entities=entities, replacement_windows=windows
        )
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
            rooms = ()
        self.active_ids = tuple(row["id"] for row in rows)
        self.active_rooms = tuple(app["id"] for app, *_ in rooms)
        if not rows and not rooms:
            return replace(prepared, thermal_revision=self.revision)
        self.issue = None
        problem = refine(prepared.problem, ready)
        zone = ZoneInfo(timezone)
        works = [
            boiler_work(row, self.readings[row["id"]], problem.intervals, now, ready, zone)
            for row in rows
        ]
        evidence = [
            {
                "configuration": deepcopy(row),
                "reading": deepcopy(self.readings[row["id"]]),
                "demand_source": "configured_heat_draw_profile",
            }
            for row in rows
        ]
        for app, reading, model, meter in rooms:
            work = room_work(app, reading, model, weather, problem.intervals, now, ready)
            if work is None:
                # Keep the full forecast if refinement cannot preserve the calibrated load.
                self.active_ids = self.active_rooms = ()
                return replace(
                    self.ev.prepare(snapshot, demand, now, timezone), thermal_revision=self.revision
                )
            works.append(work)
            evidence.append(
                {
                    "configuration": {
                        "id": app["id"],
                        "kind": "space_heating",
                        "energy_entity": meter,
                        "climate_entity": app["climate_entity"],
                        "heater_w": model.heater_w,
                        "target_c": reading["target_c"],
                        "maximum_preheat_c": app["maximum_preheat_c"],
                        "ready_at": ready.isoformat(),
                    },
                    "reading": deepcopy(reading),
                    "demand_source": "calibrated_room",
                    "model": asdict(model),
                }
            )
        return replace(
            prepared,
            problem=replace(problem, thermal_work=tuple(works)),
            thermal_revision=self.revision,
            thermal_evidence=tuple(evidence),
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
            planned, unmet_draw, work = _stored_boiler_plan(plan, self.revision, row["id"])
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
        applications = application_documents(deepcopy(self.state), catalogue or {})
        sources = shared_sources(applications)
        for application in applications:
            application.update(self.space_heating_status(application, now))
        source_modes = {
            meter: self.source_modes[meter]
            for meter, source in sources.items()
            if meter in self.source_modes
            and self.source_modes[meter]["application_id"]
            in {option["id"] for option in source["options"]}
            and (now - instant(self.source_modes[meter]["observed_at"])).total_seconds()
            < TEMPERATURE_MAX_AGE_SECONDS
        }
        return {
            "configuration": deepcopy(self.state),
            "configuration_version": self.configuration_version,
            "revision": self.revision,
            "devices": devices,
            "applications": applications,
            "shared_sources": sources,
            "source_modes": source_modes,
            "issue": self.issue,
        }
