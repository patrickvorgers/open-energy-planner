"""Durable EV intent and fresh identified charging sessions. No HA client or physical control."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.ev.configuration import configuration, identity
from open_energy_planner.appliances.ev.model import (
    DELIVERED_POWER_POINTS,
    EV_LOSS_PROFILE_VERSION,
    MAX_EV_POWER_W,
    EVWork,
    number,
    refine,
)
from open_energy_planner.appliances.ev.notifications import (
    EXECUTION_DEVIATION_ADVICE,
    forecast_boundary_issue,
)
from open_energy_planner.appliances.ev.requests import (
    REQUEST_HISTORY_PAGE_SIZE,
    active_request,
    advance_requests,
    cancel_request,
    departure,
    request_document,
    save_request,
)
from open_energy_planner.core.units import FULL_SOC_PERCENT
from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.home_assistant.names import source_names, vehicle_source_names
from open_energy_planner.optimization.adapters.process.worker import DEFAULT_WORKER_TIMEOUT_SECONDS

STATE_KEY = "ev-state.json"
REPORT_MAX_AGE_SECONDS = 120
CLOCK_SKEW_SECONDS = 5
HA_HANDOVER_SECONDS = 5
EV_DISPATCH_LEAD_SECONDS = DEFAULT_WORKER_TIMEOUT_SECONDS + HA_HANDOVER_SECONDS
EV_POWER_DEVIATION_W = 500
EV_POWER_DEVIATION_SECONDS = 60


class EVService:
    """Own configuration and intent. Telemetry is deliberately unavailable after app restart."""

    def __init__(self, documents, consumers, appliances):
        """Restore intent without promoting retained SoC or old vehicle assignments to live facts."""
        self.documents, self.consumers, self.appliances = documents, consumers, appliances
        self.state = (
            documents.read(STATE_KEY)
            if documents.exists(STATE_KEY)
            else {"vehicles": [], "points": [], "assignments": {}}
        )
        self.report = None
        self.issue = None
        self.power_deviations = {}
        self.state.setdefault("requests", {})
        self.state.setdefault("active_requests", {})

    def commit(self, state):
        """Archive a complete intent revision and update its pointer in one database transaction."""
        if state == self.state:
            return
        with self.documents.transaction():
            self.documents.write("ev-state-" + digest(state) + ".json", state)
            self.documents.write(STATE_KEY, state)
        self.state = state

    @property
    def revision(self):
        """Separate hard intent/connection changes from ordinary new SoC reports."""
        connections = [
            {key: row[key] for key in ("id", "session_id", "connected")}
            for row in (self.report or {}).get("points", [])
        ]
        current = {key: self.state[key] for key in ("vehicles", "points", "assignments")}
        current["requests"] = {
            vehicle_id: self.state["requests"][request_id]
            for vehicle_id, request_id in self.state["active_requests"].items()
        }
        return digest([current, connections])

    def configure(self, body, catalogue, now=None):
        """Reuse Energy ownership and automatically request each charger's component forecast."""
        checked, selected = configuration(body, catalogue, self.consumers, self.appliances)
        if tuple(selected) != self.consumers.selected:
            # Forecast selection is idempotent. A failed EV write leaves only a safe extra forecast.
            self.consumers.save(tuple(selected), self.consumers.version)
        state = {**deepcopy(self.state), **checked, "assignments": {}}
        vehicle_ids = {car["id"] for car in checked["vehicles"]}
        for vehicle_id, request_id in tuple(state["active_requests"].items()):
            if vehicle_id not in vehicle_ids:
                state = cancel_request(
                    state,
                    request_id,
                    digest(state["requests"][request_id]),
                    now or datetime.now(UTC),
                )
        self.commit(state)
        self.report = None
        self.power_deviations.clear()
        self.issue = None

    def receive(self, body, now, timezone=None):
        """Accept bounded current HA readings with exact configured provenance and explicit unknowns."""
        if not isinstance(body, dict) or set(body) != {
            "configuration_version",
            "at",
            "vehicles",
            "points",
        }:
            raise ValueError("Invalid EV observation report")
        if body["configuration_version"] != self.configuration_version:
            raise ValueError("EV configuration changed. Refresh before reporting")
        at = instant(body["at"])
        if not -CLOCK_SKEW_SECONDS <= (now - at).total_seconds() < REPORT_MAX_AGE_SECONDS:
            raise ValueError("EV report is stale or from the future")
        if self.report and at <= instant(self.report["at"]):
            raise ValueError("EV reports must advance chronologically")
        for kind, fields in (
            ("vehicles", {"id", "soc", "observed_at", "reported_at", "timestamp_kind"}),
            ("points", {"id", "session_id", "connected", "power_w"}),
        ):
            rows = body[kind]
            expected = {row["id"] for row in self.state[kind]}
            if not isinstance(rows, list) or len(rows) != len(expected):
                raise ValueError("EV report membership mismatch")
            if any(not isinstance(row, dict) or set(row) != fields for row in rows):
                raise ValueError("Invalid EV reading fields")
            if {row["id"] for row in rows} != expected:
                raise ValueError("EV reading identity mismatch")
        for row in body["vehicles"]:
            if row["soc"] is not None:
                number(row["soc"], 0, FULL_SOC_PERCENT)
                observed = instant(row["observed_at"])
                reported = instant(row["reported_at"])
                configured = next(car for car in self.state["vehicles"] if car["id"] == row["id"])
                kinds = (
                    ("device_sample",)
                    if configured.get("soc_timestamp_attribute")
                    else ("ha_report", "ha_state")
                )
                if (
                    row["timestamp_kind"] not in kinds
                    or observed > at + timedelta(seconds=CLOCK_SKEW_SECONDS)
                    or reported > at + timedelta(seconds=CLOCK_SKEW_SECONDS)
                ):
                    raise ValueError(
                        "Vehicle SoC provenance is invalid or its timestamp is in the future"
                    )
            elif any(
                row[key] is not None for key in ("observed_at", "reported_at", "timestamp_kind")
            ):
                raise ValueError("Unknown SoC cannot have a claimed measurement timestamp")
        for row in body["points"]:
            identity(row["session_id"])
            if row["connected"] is not None and type(row["connected"]) is not bool:
                raise ValueError("Connection must be explicitly true, false or unknown")
            if row["power_w"] is not None:
                number(row["power_w"], 0, MAX_EV_POWER_W)
        self.report = deepcopy(body)
        self.commit(advance_requests(self.state, self.report, now, timezone))

    @property
    def configuration_version(self):
        """Correlate HA capture with configuration while allowing separate native intent controls."""
        return digest({key: self.state[key] for key in ("vehicles", "points")})

    def assign(self, point_id, vehicle_id, session_id, now):
        """Bind one explicitly identified car to the current connection only. Never infer the car."""
        rows = self.live(now)
        point = next((row for row in rows if row["id"] == point_id), None)
        configured = next((row for row in self.state["points"] if row["id"] == point_id), None)
        if not point or point["connected"] is not True or point["session_id"] != session_id:
            raise ValueError("A fresh connected charging session is required")
        if vehicle_id is not None and vehicle_id not in configured["vehicle_ids"]:
            raise ValueError("Vehicle is not allowed at this charge point")
        for other in rows:
            assigned = self.state["assignments"].get(other["id"], {})
            if (
                vehicle_id is not None
                and other["id"] != point_id
                and other["connected"] is True
                and assigned.get("session_id") == other["session_id"]
                and assigned.get("vehicle_id") == vehicle_id
            ):
                raise ValueError("This vehicle is already assigned to another connected point")
        state = deepcopy(self.state)
        state["assignments"][point_id] = {"vehicle_id": vehicle_id, "session_id": session_id}
        self.commit(state)

    def intent(self, vehicle_id, field, value, now, timezone):
        """Edit the current request through native controls without changing vehicle defaults."""
        vehicle = next((row for row in self.state["vehicles"] if row["id"] == vehicle_id), None)
        if vehicle is None:
            raise ValueError("Unknown vehicle")
        request = active_request(self.state, vehicle_id)
        if request is None:
            raise ValueError("Plan a charging request before changing its values")
        body = {
            "request_id": request["id"],
            "vehicle_id": vehicle_id,
            "target_soc": request["target_soc"],
            "departure": request["departure"],
            "repeat_weekdays": request["repeat_weekdays"],
            "expected_version": digest(request),
        }
        if field == "target_soc":
            body[field] = value
        elif field == "departure_override":
            body["departure"] = (
                departure(vehicle, now, timezone).isoformat() if value is None else value
            )
        elif field in ("departure_date", "departure_clock", "departure_time"):
            zone = ZoneInfo(timezone)
            previous = instant(request["departure"]).astimezone(zone)
            local_date = date.fromisoformat(value) if field == "departure_date" else previous.date()
            local_time = (
                time.fromisoformat(value)
                if field in ("departure_clock", "departure_time")
                else previous.time()
            )
            if local_time.tzinfo:
                raise ValueError("Use a local departure time")
            local = datetime.combine(local_date, local_time, zone)
            deadline = local.astimezone(UTC)
            if deadline.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                raise ValueError("This local departure time does not exist")
            body["departure"] = deadline.isoformat()
        else:
            raise ValueError("Unknown EV intent field")
        self.save_request(body, now, timezone)

    def save_request(self, body, now, timezone):
        """Persist a complete charging request atomically with immutable state history."""
        self.commit(save_request(self.state, body, now, timezone))

    def cancel_request(self, request_id, expected_version, now, vehicle_id):
        """Cancel the request and repetition without erasing completed or cancelled history."""
        request = self.state["requests"].get(request_id)
        if request and request["vehicle_id"] != vehicle_id:
            raise ValueError("Charging request belongs to a different vehicle")
        self.commit(cancel_request(self.state, request_id, expected_version, now))

    def request_history(self, catalogue=None, cursor=None):
        """Page retained requests independently of bounded live HA device projections."""
        records = self.state["requests"]
        if cursor is not None and cursor not in records:
            raise ValueError("Unknown charging history cursor")
        ordered = sorted(
            records.values(), key=lambda row: (row["created_at"], row["id"]), reverse=True
        )
        if cursor is not None:
            boundary = (records[cursor]["created_at"], cursor)
            ordered = [row for row in ordered if (row["created_at"], row["id"]) < boundary]
        page = ordered[:REQUEST_HISTORY_PAGE_SIZE]
        names = vehicle_source_names(catalogue)
        cars = {car["id"]: car for car in self.state["vehicles"]}
        return {
            "requests": [
                {
                    **request_document(row),
                    "vehicle_name": names.get(
                        cars.get(row["vehicle_id"], {}).get("soc_entity"), "Removed vehicle"
                    ),
                }
                for row in page
            ],
            "next_cursor": page[-1]["id"] if len(ordered) > len(page) else None,
        }

    def live(self, now):
        """Return fresh connection evidence, never retained readings after a transport outage."""
        if (
            not self.report
            or not -CLOCK_SKEW_SECONDS
            <= (now - instant(self.report["at"])).total_seconds()
            < REPORT_MAX_AGE_SECONDS
        ):
            return []
        return self.report["points"]

    def vehicle_readings(self, now):
        """Return available SoC only while its integration report is current, preserving original sensor times."""
        if (
            not self.report
            or not -CLOCK_SKEW_SECONDS
            <= (now - instant(self.report["at"])).total_seconds()
            < REPORT_MAX_AGE_SECONDS
        ):
            return {}
        return {row["id"]: row for row in self.report["vehicles"] if row["soc"] is not None}

    def energy_source_available(self, point):
        """Require the saved measurement pair to match current authoritative Energy configuration."""
        return any(
            source["entity_id"] == point["energy_entity"]
            and source.get("power_entity") == point["power_entity"]
            for source in self.consumers.options
        )

    def work(self, now, timezone):
        """Build solver work only for identified, fresh, controllable, uniquely connected vehicles."""
        telemetry = {row["id"]: row for row in self.live(now)}
        readings = self.vehicle_readings(now)
        vehicles = {row["id"]: row for row in self.state["vehicles"]}
        selected = []
        for point in self.state["points"]:
            if not self.energy_source_available(point):
                continue
            actual = telemetry.get(point["id"])
            assigned = self.state["assignments"].get(point["id"], {})
            if (
                not actual
                or actual["connected"] is not True
                or assigned.get("session_id") != actual["session_id"]
            ):
                continue
            car = vehicles.get(assigned.get("vehicle_id"))
            measured = readings.get(assigned.get("vehicle_id"), {})
            if not car or measured.get("soc") is None or point["control"] == "read_only":
                continue
            request = active_request(self.state, car["id"])
            if request is None or instant(request["departure"]) <= now:
                continue
            maximum = min(point["maximum_w"], car["maximum_w"])
            minimum = maximum if point["control"] == "on_off" else point["minimum_w"]
            if maximum < minimum:
                continue
            deadline = instant(request["departure"])
            ready = min(
                max(
                    now + timedelta(seconds=EV_DISPATCH_LEAD_SECONDS), instant(request["ready_at"])
                ),
                deadline - timedelta(microseconds=1),
            )
            selected.append(
                EVWork(
                    actual["session_id"],
                    point["id"],
                    car["id"],
                    ready,
                    deadline,
                    car["capacity_kwh"],
                    measured["soc"],
                    request["target_soc"],
                    minimum,
                    maximum,
                    point["control"],
                )
            )
        duplicates = {
            work.vehicle_id
            for work in selected
            if sum(other.vehicle_id == work.vehicle_id for other in selected) > 1
        }
        return tuple(work for work in selected if work.vehicle_id not in duplicates)

    def hard_revision(self, now, timezone):
        """Bind availability/identity/departure, allowing ordinary valid SoC readings to finish a round."""
        return digest(
            [
                self.revision,
                [
                    (work.session_id, work.vehicle_id, work.deadline.isoformat())
                    for work in self.work(now, timezone)
                ],
            ]
        )

    def prepare(self, snapshot, demand, now, timezone, *, excluded_entities=()):
        """Remove every configured charger forecast and add only explicit live SOC work."""
        works = self.work(now, timezone)
        entities = {point["energy_entity"] for point in self.state["points"]}
        entities.update(excluded_entities)
        prepared = self.appliances.prepare(snapshot, demand, now, excluded_entities=entities)
        if entities and not prepared.appliance_evidence:
            self.issue = (
                "Charger component forecast unavailable. EV history cannot be removed safely"
                if self.state["points"]
                else None
            )
            works = ()
        else:
            self.issue = None
        return replace(
            prepared,
            problem=refine(prepared.problem, works) if works else prepared.problem,
            ev_revision=self.hard_revision(now, timezone),
            ev_evidence=tuple(
                {
                    "work": asdict(work),
                    "energy_entity": next(
                        point["energy_entity"]
                        for point in self.state["points"]
                        if point["id"] == work.point_id
                    ),
                    "soc_reading": deepcopy(self.vehicle_readings(now)[work.vehicle_id]),
                    "soc_source": {
                        field: next(
                            car for car in self.state["vehicles"] if car["id"] == work.vehicle_id
                        ).get(field)
                        for field in ("soc_entity", "soc_timestamp_attribute")
                    },
                    "loss_profile": {
                        "version": EV_LOSS_PROFILE_VERSION,
                        "delivered_power_points": DELIVERED_POWER_POINTS,
                    },
                }
                for work in works
            ),
        )

    def execution_deviated(self, plan, now):
        """Latch sustained point-power deviations until one replacement round accepts them.

        Missing readings are not zero power. A new session, setpoint or material
        measured-power change starts a new episode. Unchanged refusal remains
        visible without repeatedly requesting the same early calculation.
        """
        active = next(
            (
                step
                for step in plan.get("steps", ())
                if instant(step["start"]) <= now < instant(step["end"])
            ),
            None,
        )
        if active is None or now >= instant(plan["valid_until"]):
            self.power_deviations.clear()
            return False
        live = {row["id"]: row for row in self.live(now)}
        captured_at = instant(self.report["at"]) if live else None
        if captured_at is None or captured_at < instant(active["start"]):
            self.power_deviations.clear()
            return False
        retained = {}
        for point in self.state["points"]:
            actual = live.get(point["id"], {})
            power = actual.get("power_w")
            if power is None:
                continue
            command = active["devices"].get(point["id"], {})
            expected = command.get("power_w", 0)
            if abs(power - expected) < EV_POWER_DEVIATION_W:
                continue
            signature = (actual.get("session_id"), expected)
            previous = self.power_deviations.get(point["id"])
            if (
                previous is None
                or previous["signature"] != signature
                or abs(power - previous["power_w"]) >= EV_POWER_DEVIATION_W
                or (captured_at - previous["last_seen"]).total_seconds() >= REPORT_MAX_AGE_SECONDS
            ):
                previous = {
                    "signature": signature,
                    "power_w": power,
                    "since": captured_at,
                    "acknowledged": False,
                }
            previous["last_seen"] = captured_at
            retained[point["id"]] = previous
        self.power_deviations = retained
        return any(
            not deviation["acknowledged"]
            for deviation in self.confirmed_power_deviations(now).values()
        )

    def confirmed_power_deviations(self, now):
        """Require advancing accepted power captures, not elapsed wall time or SoC sample age."""
        if not self.live(now):
            return {}
        captured_at = instant(self.report["at"])
        return {
            point_id: deviation
            for point_id, deviation in self.power_deviations.items()
            if deviation["last_seen"] == captured_at
            and (deviation["last_seen"] - deviation["since"]).total_seconds()
            >= EV_POWER_DEVIATION_SECONDS
        }

    def accept_execution_deviations(self, now):
        """Coalesce an unchanged deviation after submitting its replacement calculation."""
        for deviation in self.confirmed_power_deviations(now).values():
            deviation["acknowledged"] = True

    def document(self, now, timezone, catalogue=None, plan=None):
        """Expose authoritative configuration, current session issues and explicit predicted shortfall."""
        names = source_names(catalogue)
        vehicle_names = vehicle_source_names(catalogue)
        live = {row["id"]: row for row in self.live(now)}
        readings = self.vehicle_readings(now)
        works = {work.point_id: work for work in self.work(now, timezone)}
        confirmed = self.confirmed_power_deviations(now)
        current_revision = self.hard_revision(now, timezone)
        usable = bool(
            plan
            and instant(plan["valid_until"]) > now
            and plan.get("ev_context", {}).get("revision") == current_revision
        )
        schedules = {row["point_id"]: row for row in plan.get("ev_schedule", [])} if usable else {}
        points = []
        for point in self.state["points"]:
            actual = live.get(point["id"], {})
            assigned = self.state["assignments"].get(point["id"], {})
            vehicle_id = (
                assigned.get("vehicle_id")
                if actual.get("connected") is True
                and assigned.get("session_id") == actual.get("session_id")
                else None
            )
            status = (
                "energy_source_unavailable"
                if not self.energy_source_available(point)
                else "telemetry_unavailable"
                if not actual
                else "disconnected"
                if actual["connected"] is False
                else "connection_unknown"
                if actual["connected"] is None
                else "vehicle_unknown"
                if not vehicle_id
                else "measurement_only"
                if point["control"] == "read_only"
                else "request_missing"
                if active_request(self.state, vehicle_id) is None
                else "ready"
                if point["id"] in works
                else "soc_or_limits_unavailable"
            )
            points.append(
                {
                    **point,
                    "name": names.get(point["energy_entity"], "Charge point"),
                    "actual": actual,
                    "vehicle_id": vehicle_id,
                    "status": status,
                    "execution_issue": (
                        EXECUTION_DEVIATION_ADVICE if usable and point["id"] in confirmed else None
                    ),
                    "schedule": schedules.get(point["id"])
                    if schedules.get(point["id"], {}).get("session_id") == actual.get("session_id")
                    else None,
                }
            )
        return {
            "configuration_version": self.configuration_version,
            "revision": current_revision,
            "observed_at": self.report["at"] if self.report and self.live(now) else None,
            "issue": forecast_boundary_issue(self.state["points"], names) if self.issue else None,
            "timezone": timezone,
            "vehicles": [
                {
                    **car,
                    "name": vehicle_names.get(car["soc_entity"], "Vehicle"),
                    "actual": readings.get(car["id"]),
                    "next_departure": (active_request(self.state, car["id"]) or {}).get(
                        "departure", departure(car, now, timezone).isoformat()
                    ),
                    "default_departure": departure(car, now, timezone).isoformat(),
                    "charging_request": request_document(active_request(self.state, car["id"])),
                }
                for car in self.state["vehicles"]
            ],
            "points": points,
        }
