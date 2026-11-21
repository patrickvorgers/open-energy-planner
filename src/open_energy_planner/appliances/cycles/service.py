"""Durable application authority for appliance tasks, attempts, feedback and cycle evidence."""

from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
from math import fsum
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from open_energy_planner.appliances.cycles.learning import (
    MIN_COMPLETE_CYCLES,
    RECENCY_HALF_LIFE_DAYS,
    SAMPLE_SECONDS,
    detect_cycles,
    history_evidence,
    manual_profile,
)
from open_energy_planner.appliances.cycles.model import (
    CONFIRMATION_SECONDS,
    DURATION_ALLOWANCE_SECONDS,
    ApplianceWork,
    candidates,
    instant,
    learn_cycle,
)
from open_energy_planner.appliances.cycles.model import (
    restore_profile as profile,
)
from open_energy_planner.appliances.sources import validate_membership
from open_energy_planner.demand.calculation.model import EnergyTerm, digest
from open_energy_planner.installation.model import InstallationProblem, slice_interval

STATE_KEY = "appliance-state.json"
PROGRESS_KEY = "appliance-progress.json"
PROGRESS_FIELDS = (
    "learning_history",
    "history_import",
    "fine_import",
    "raw_history",
    "report_coverage",
)
MAX_CORRELATION_LENGTH = 64
MAX_CYCLE_OBSERVATIONS = 2048
MAX_FEEDBACK_EVENTS = 1024
MAX_TASK_WINDOW_DAYS = 7
COMPONENT_FORECAST_MAX_AGE_MINUTES = 30
MAX_REFINED_PLAN_INTERVALS = 8192
MAX_PHASE_BOUNDARY_CHECKS = 8 * MAX_REFINED_PLAN_INTERVALS
MAX_PROJECTED_IRREGULAR_PHASES = MAX_REFINED_PLAN_INTERVALS
MAX_APPLIANCE_PROJECTION_BYTES = 4 * 1024 * 1024
RAW_PROJECTION_RESERVE_BYTES = 512 * 1024
OPEN_STATUSES = (
    "pending",
    "scheduled",
    "requested",
    "start_not_confirmed",
    "running",
    "execution_observed",
    "deadline_not_achievable",
    "forecast_not_ready",
)


def separate_progress(state):
    """Separate immutable model/task state from mutable history progress without altering the caller.

    Returns:
        Copied state without progress fields and a devices document holding
        those fields. Models, permissions and tasks remain in the state copy.
    """
    archived = deepcopy(state)
    progress = {}
    for entity, settings in archived["devices"].items():
        fields = {field: settings.pop(field) for field in PROGRESS_FIELDS if field in settings}
        if fields:
            progress[entity] = fields
    return archived, {"devices": progress}


class ApplianceService:
    """Keep hardware authority in HA while persisting exact task/attempt transitions in the app."""

    def __init__(self, documents, consumers):
        """Restore authoritative work and source-bound progress. Never replay a claimed start."""
        self.documents, self.consumers = documents, consumers
        self.state = (
            documents.read(STATE_KEY)
            if documents.exists(STATE_KEY)
            else {"devices": {}, "tasks": {}}
        )
        if documents.exists(PROGRESS_KEY):
            for entity, progress in documents.read(PROGRESS_KEY)["devices"].items():
                self.state["devices"][entity].update(progress)
        self.issue = None
        self._cycle_records = {}
        self._phase_limited_entities = set()
        self._report_pairs = set()
        self._refresh_hourly_profiles()

    def _refresh_hourly_profiles(self):
        """Rebuild prior hourly-only estimates once from the original recorded cycles."""
        from .evidence import HOURLY_PROFILE_POLICY, refine

        candidate = {**self.state, "devices": self.state["devices"].copy()}
        for entity, settings in self.state["devices"].items():
            power_entity = settings.get("learning_source")
            if (
                not power_entity
                or settings.get("profile", {}).get("evidence") != "hourly_estimate"
                or settings.get("hourly_profile_policy") == HOURLY_PROFILE_POLICY
            ):
                continue
            try:
                records, _, _ = self.collect_cycles(entity, power_entity, (), False, hourly=True)
                rebuilt = deepcopy(settings)
                refine(rebuilt, list(records.values()))
            except ValueError:
                # An incomplete archive cannot safely replace a retained estimate.
                continue
            candidate["devices"][entity] = rebuilt
        self.commit(candidate)

    @property
    def revision(self):
        """Identify all task/attempt changes that can invalidate an in-flight optimization."""
        return digest(
            [
                {
                    entity: {
                        key: value
                        for key, value in settings.items()
                        if key
                        not in (
                            "finish_before",
                            "finish_date",
                            "finish_time",
                            "finish_time_zone",
                            "learning_history",
                            "history_import",
                            "fine_import",
                            "raw_history",
                            "report_coverage",
                        )
                        and not key.startswith("hourly_")
                    }
                    for entity, settings in self.state["devices"].items()
                },
                [
                    {
                        key: task[key]
                        for key in ("id", "entity_id", "ready_at", "deadline", "profile")
                    }
                    | {
                        "status": task["status"]
                        if task["attempt"] or task["status"] not in OPEN_STATUSES
                        else "waiting",
                        "attempt": task["attempt"],
                    }
                    for task in self.state["tasks"].values()
                ],
            ]
        )

    def commit(self, candidate, *, extra=()):
        """Commit cycles, changed task/model state and mutable progress atomically before publishing live state.

        Progress-only refreshes update one small document without creating full
        immutable state versions or rewriting unchanged task/model pointers.
        Startup combines that document with the authoritative current state.
        """
        if candidate == self.state and not extra:
            return
        archived, progress = separate_progress(candidate)
        previous, previous_progress = separate_progress(self.state)
        with self.documents.transaction():
            for key, value in extra:
                self.documents.write(key, value)
            if progress != previous_progress:
                self.documents.write(PROGRESS_KEY, progress)
            if archived != previous:
                for task in archived["tasks"].values():
                    self.documents.write("appliance-task-" + task["id"] + ".json", task)
                self.documents.write("appliance-state-" + digest(archived) + ".json", archived)
                self.documents.write(STATE_KEY, archived)
        self.state = candidate

    def member(self, entity):
        """Require a selected, still Energy-owned, disjoint measured device."""
        EnergyTerm(entity, 1)
        selected = self.consumers.selected
        validate_membership(selected, self.consumers.options)
        if entity not in {item["entity_id"] for item in selected}:
            raise ValueError("Select this consumer in Appliances settings first")

    def configure(self, entity, enabled, expected_version=None):
        """Grant explicit shift permission without changing Energy membership or measurement links."""
        self.member(entity)
        if expected_version is not None and expected_version != self.revision:
            raise ValueError("Appliance settings changed. Reload before saving")
        if type(enabled) is not bool:
            raise ValueError("Shift permission must be boolean")
        task = self.state["tasks"].get(entity)
        if task and task["status"] in OPEN_STATUSES:
            raise ValueError("Resolve the open task before changing shift permission")
        data = deepcopy(self.state)
        data["devices"].setdefault(entity, {})["shiftable"] = enabled
        self.commit(data)

    def configure_additional_profile(self, entity, enabled, expected_version):
        """Persist a per-device opt-in and rebuild it from the existing source-bound archive."""
        from .evidence import refine

        self.member(entity)
        if expected_version != self.revision:
            raise ValueError("Appliance settings changed. Reload before saving")
        if type(enabled) is not bool:
            raise ValueError("Additional profile learning must be boolean")
        data = deepcopy(self.state)
        settings = data["devices"].setdefault(entity, {})
        previously_enabled = settings.get("learn_additional_profile", False)
        if enabled == previously_enabled:
            return
        settings["learn_additional_profile"] = enabled
        power_entity = self.source(entity)
        if power_entity:
            records, _, _ = self.collect_cycles(entity, power_entity, (), False)
            if records:
                refine(settings, list(records.values()))
        self.commit(data)

    def source(self, entity):
        """Bind learning to the authoritative Energy energy/power pair."""
        option = next((row for row in self.consumers.options if row["entity_id"] == entity), {})
        return option.get("power_entity")

    def sync_sources(self):
        """Withdraw learned profiles after a sensor change, retaining manual estimates and archives."""
        from .evidence import completion, refine

        data = deepcopy(self.state)
        withdrawn = set()
        for entity, settings in data["devices"].items():
            for field in ("fine_import", "observed_run", "raw_history", "report_coverage"):
                if settings.get(field, {}).get("power_entity") not in (None, self.source(entity)):
                    settings.pop(field)
            progress = settings.get("history_import")
            if progress and progress.get("power_entity") != self.source(entity):
                settings.pop("history_import", None)
                for key in tuple(settings):
                    if key.startswith("hourly_"):
                        settings.pop(key)
                if settings.get("profile", {}).get("evidence") == "hourly_estimate":
                    settings.pop("profile")
            history = settings.get("learning_history")
            if history and history.get("power_entity") != self.source(entity):
                settings.pop("learning_history", None)
            if settings.get("learning_source") and settings["learning_source"] != self.source(
                entity
            ):
                withdrawn.add(entity)
                for key in tuple(settings):
                    if key.startswith("hourly_"):
                        settings.pop(key)
                settings.pop("learned_profile", None)
                settings.pop("learning_source", None)
                settings.pop("recent_cycles", None)
                settings.pop("profile_sources", None)
                settings.pop("profile_basis", None)
                settings.pop("hourly_profile_policy", None)
                settings.pop("additional_profile", None)
                settings.pop("last_detected_end", None)
                settings.pop("learning_weight", None)
                settings.pop("first_detected_start", None)
                settings.pop("profile", None)
                if settings.get("manual_profile"):
                    settings["profile"] = asdict(manual_profile(**settings["manual_profile"]))
                explicit = [
                    completion(record)
                    for record in self.documents.iter_prefix("appliance-cycle-")
                    if record["entity_id"] == entity
                ]
                if explicit:
                    refine(settings, explicit)
        self.commit(data)
        for pair in tuple(self._cycle_records):
            if pair[0] in withdrawn:
                self._cycle_records.pop(pair)
        self._report_pairs.intersection_update(
            (entity, self.source(entity)) for entity in data["devices"]
        )

    def set_manual_profile(self, entity, duration_minutes, energy_kwh, expected_version):
        """Retain an explicit estimate without replacing a ready learned profile or accepted task."""
        self.member(entity)
        if expected_version != self.revision:
            raise ValueError("Appliance settings changed. Reload before saving")
        estimate = manual_profile(duration_minutes, energy_kwh)
        data = deepcopy(self.state)
        settings = data["devices"].setdefault(entity, {"shiftable": False})
        settings["manual_profile"] = {
            "duration_minutes": duration_minutes,
            "energy_kwh": energy_kwh,
        }
        if (
            not settings.get("learned_profile")
            or settings["learned_profile"]["samples"] < MIN_COMPLETE_CYCLES
        ):
            settings["profile"] = asdict(estimate)
        self.commit(data)

    def collect_cycles(self, entity, power_entity, cycles, synthetic, *, hourly=False, rows=None):
        """Prepare immutable evidence without publishing cache or state before a successful commit.

        Archives are read once for each sensor pair. Fine and hourly observations
        retain their original counters. The cache contains derived profiles only
        in memory and is updated by the caller after its durable transaction.
        """
        from .evidence import completion, measured

        pair = (entity, power_entity)
        if pair not in self._cycle_records:
            restored = {
                prefix + record["id"]: record
                for prefix in ("appliance-detected-", "appliance-hourly-", "appliance-raw-")
                for record in self.documents.iter_prefix(prefix)
                if record["entity_id"] == entity
                and record["power_entity"] == power_entity
                and record.get("training_usable", True)
            }
            for record in self.documents.iter_prefix("appliance-cycle-"):
                if record["entity_id"] == entity:
                    explicit = completion(record)
                    restored["appliance-cycle-" + explicit["id"]] = explicit
            settings = self.state["devices"].get(entity, {})
            saved = settings.get("profile", {})
            if saved.get("evidence") != "manual" and saved.get("samples", 0) > len(restored):
                raise ValueError(
                    "Archived cycle evidence is incomplete. Retained profile is unchanged"
                )
            for field, is_hourly in (("learned_profile", False), ("hourly_profile", True)):
                available = sum(
                    (record.get("quality") == "hourly_estimate") == is_hourly
                    for record in restored.values()
                )
                if settings.get(field, {}).get("samples", 0) > available:
                    raise ValueError(
                        "Archived cycle evidence is incomplete. Retained profile is unchanged"
                    )
            records = restored
        else:
            records = dict(self._cycle_records[pair])
        extra = []
        prefix = "appliance-hourly-" if hourly else "appliance-detected-"
        for started, completed, points in cycles:
            statistical = None
            if not hourly and rows is not None:
                from .learning import statistics_profile

                try:
                    statistical = statistics_profile(
                        rows, started, completed, points, synthetic=synthetic
                    )
                except ValueError:
                    continue
            identity = digest(
                [
                    entity,
                    power_entity,
                    started.isoformat(),
                    completed.isoformat(),
                    *([asdict(statistical)] if statistical else []),
                ]
            )
            key = prefix + identity + ".json"
            # Resolution is part of archive identity even when both time ranges agree.
            record_key = prefix + identity
            if record_key in records or self.documents.exists(key):
                continue
            body = {
                "id": identity,
                "entity_id": entity,
                "power_entity": power_entity,
                "started_at": started.isoformat(),
                "completed_at": completed.isoformat(),
                "points": points,
                "synthetic": synthetic,
                **({"quality": "hourly_estimate"} if hourly else {}),
                **(
                    {
                        "quality": "five_minute_statistics",
                        "profile": asdict(statistical),
                        "mean_power": [
                            row for row in rows if started <= instant(row["at"]) < completed
                        ],
                    }
                    if statistical
                    else {}
                ),
            }
            cached = dict(body)
            try:
                learned = measured(cached)
            except ValueError:
                continue
            if not hourly:
                body["energy_kwh"] = learned.energy_kwh
            records[record_key] = cached
            extra.append((key, body))
        changed = bool(extra) or pair not in self._cycle_records
        return records, extra, changed

    def learn_history(
        self,
        entity,
        power_entity,
        rows,
        synthetic,
        now,
        *,
        sample_seconds=SAMPLE_SECONDS,
        processing_started=None,
    ):
        """Use the first complete cycle and refine all distinct source-bound observations.

        Fine observations replace overlapping hourly estimates. Both raw views
        stay archived, and accepted tasks keep their frozen profile. Historical
        evidence never grants readiness or reports a physical execution outcome.

        now is the explicit UTC operation-entry time. processing_started is its
        process-local monotonic timestamp, captured before HTTP executor dispatch
        or at direct-call entry. Only observation admission adds elapsed work.
        """
        from .evidence import refine
        from .learning import HOURLY_SAMPLE_SECONDS

        processing_started = monotonic() if processing_started is None else processing_started
        self.member(entity)
        if type(synthetic) is not bool or not power_entity or power_entity != self.source(entity):
            raise ValueError("History must use the current Home Assistant Energy power sensor")
        activity = {}
        cycles = detect_cycles(rows, now, sample_seconds=sample_seconds, activity=activity)
        self.sync_sources()
        records, extra, changed = self.collect_cycles(
            entity,
            power_entity,
            cycles,
            synthetic,
            hourly=sample_seconds == HOURLY_SAMPLE_SECONDS,
            rows=rows if sample_seconds == SAMPLE_SECONDS else None,
        )
        data = deepcopy(self.state)
        settings = data["devices"].setdefault(entity, {"shiftable": False})
        if changed:
            refine(settings, list(records.values()))
        if sample_seconds == SAMPLE_SECONDS:
            self.observe_history(
                data,
                entity,
                rows,
                cycles,
                activity,
                power_entity,
                now,
                processing_started=processing_started,
            )
        summary = history_evidence(rows, len(cycles), sample_seconds=sample_seconds)
        summary["power_entity"], summary["synthetic"] = power_entity, synthetic
        settings[
            "hourly_history" if sample_seconds == HOURLY_SAMPLE_SECONDS else "learning_history"
        ] = summary
        self.commit(data, extra=extra)
        self._cycle_records[(entity, power_entity)] = records

    def observe_history(
        self,
        data,
        entity,
        rows,
        cycles,
        activity,
        power_entity,
        now,
        *,
        observed_until=None,
        processing_started=None,
        raw_power=(),
    ):
        """Admit recent running evidence and refine its remaining curve without changing accepted work."""
        from .observed import observe
        from .programs import ADDITIONAL_PROGRAM, recognize

        settings, task = data["devices"][entity], data["tasks"].get(entity)
        if processing_started is not None:
            now += timedelta(seconds=max(0, monotonic() - processing_started))
        previous_run = settings.get("observed_run")
        accepted_running = observe(
            settings, rows, cycles, activity, power_entity, now, observed_until=observed_until
        )
        if not accepted_running:
            return
        run = settings["observed_run"]
        attempted_task = (
            task
            and task["status"] in OPEN_STATUSES
            and (task.get("attempt") and task["attempt"]["status"] != "not_started")
        )
        same_run = previous_run and (
            run["started_at"] == previous_run["started_at"]
            or (
                settings.get("learn_additional_profile")
                and previous_run["status"] == "observed"
                and abs(instant(run["started_at"]) - instant(previous_run["started_at"]))
                <= timedelta(seconds=SAMPLE_SECONDS)
            )
        )
        if same_run:
            for key in ("profile", "additional_profile", "program"):
                if key in previous_run:
                    run[key] = previous_run[key]
            if attempted_task and not run.get("program"):
                run["profile"] = deepcopy(task["profile"])
        else:
            source = task if attempted_task else settings
            if attempted_task:
                run["profile"] = deepcopy(task["profile"])
            if source.get("program"):
                run["program"] = source["program"]
            elif source.get("additional_profile") and run.get("profile"):
                run["additional_profile"] = deepcopy(source["additional_profile"])
        if settings.get("learn_additional_profile") and run.get("additional_profile"):
            program = recognize(run, rows, raw_power)
            if program:
                run["program"] = program
                additional = run.pop("additional_profile")
                if program == ADDITIONAL_PROGRAM:
                    run["profile"] = additional

        if (
            task
            and task["status"] in OPEN_STATUSES
            and (
                task["status"] == "execution_observed"
                or not task.get("attempt")
                or task["attempt"]["status"] not in ("started", "completed")
            )
        ):
            task.update(status="execution_observed", scheduled_start=None, plan_id=None)

    def learn_raw_history(self, body, now, *, processing_started=None):
        """Atomically retain original Recorder frames, conserved cycles and bounded current curves.

        Capture-age validation uses the explicit UTC operation-entry now. Running
        observation admission additionally includes monotonic elapsed executor,
        archive and refinement work. Expiring running evidence cannot discard
        the accepted curve or conserved training history. Direct callers start
        timing here unless the HTTP owner supplied processing_started.
        """
        import json

        from .evidence import refine
        from .model import MAX_PROFILE_STEPS, MAX_VARIABLE_PROFILE_STEPS
        from .raw import MAX_RAW_BODY_BYTES, analyze
        from .report_coverage import advance, covers

        processing_started = monotonic() if processing_started is None else processing_started
        entity, power_entity = body["entity_id"], body["power_entity"]
        self.member(entity)
        if not power_entity or power_entity != self.source(entity):
            raise ValueError("History must use the current Home Assistant Energy power sensor")
        if len(json.dumps(body, allow_nan=False).encode()) > MAX_RAW_BODY_BYTES:
            raise ValueError("Raw appliance history exceeds bounded response size")
        end = instant(body["end"])
        if not end <= now or now - end > timedelta(seconds=2 * SAMPLE_SECONDS):
            raise ValueError("Raw appliance history capture is stale or future dated")
        result = analyze(body)
        self.sync_sources()
        records, _, changed = self.collect_cycles(entity, power_entity, (), body["synthetic"])
        data = deepcopy(self.state)
        settings = data["devices"].setdefault(entity, {"shiftable": False})
        pair = (entity, power_entity)
        previous_capture = settings.get("report_coverage", {}).get("captured_at")
        coverage = advance(
            settings.get("report_coverage"), body, continuous=pair in self._report_pairs
        )
        settings["report_coverage"] = coverage
        if changed:
            refine(settings, list(records.values()))
        extra = []
        frame = "appliance-state-history-" + digest(body) + ".json"
        for cycle in result["runs"]:
            followed = covers(
                coverage,
                instant(cycle["started_at"]) - timedelta(minutes=30),
                instant(cycle["completed_at"]) + timedelta(minutes=30),
            )
            if cycle["profile"] is not None and not followed:
                result["status"] = "not_observed"
            identity = digest(
                [
                    entity,
                    power_entity,
                    cycle["started_at"],
                    cycle["completed_at"],
                    asdict(cycle["profile"]) if cycle["profile"] else None,
                    followed,
                ]
            )
            key = "appliance-raw-" + identity + ".json"
            if self.documents.exists(key):
                retained = self.documents.read(key)
                if not retained["training_usable"]:
                    result["status"] = retained.get("rejection", "unverified")
                continue
            record = {
                "id": identity,
                "entity_id": entity,
                "power_entity": power_entity,
                "started_at": cycle["started_at"],
                "completed_at": cycle["completed_at"],
                "quality": "recorder_state",
                "synthetic": body["synthetic"],
                "history_ref": frame,
                "training_usable": cycle["profile"] is not None and followed,
                "observation_coverage": followed,
                "profile": asdict(cycle["profile"]) if cycle["profile"] else None,
                "points": [
                    row
                    for row in result["power_curve"]
                    if instant(cycle["started_at"])
                    <= instant(row["at"])
                    <= instant(cycle["completed_at"])
                ],
            }
            if cycle["profile"] is not None and not followed:
                record["rejection"] = "not_observed"
            candidate_records = {**records, "appliance-raw-" + identity: dict(record)}
            if record["training_usable"]:
                candidate = deepcopy(settings)
                try:
                    refine(candidate, list(candidate_records.values()))
                    projected = {**data["devices"], entity: candidate}
                    active = {row["entity_id"] for row in self.consumers.selected}
                    projected_phases = sum(
                        len(value["power_w"])
                        for identity, device in projected.items()
                        if identity in active
                        for value in (
                            device.get("profile"),
                            device.get("learned_profile"),
                            device.get("observed_run", {}).get("profile"),
                        )
                        if value and value.get("phase_durations_seconds") is not None
                    ) + sum(
                        len(task["profile"]["power_w"])
                        for identity, task in data["tasks"].items()
                        if identity in active
                        and task["profile"].get("phase_durations_seconds") is not None
                    )
                    if projected_phases > MAX_PROJECTED_IRREGULAR_PHASES:
                        raise ValueError("Cycle profile exceeds bounded phase complexity")
                    projected_bytes = len(
                        json.dumps(
                            self._projection({**data, "devices": projected}), allow_nan=False
                        ).encode()
                    )
                    if (
                        projected_bytes
                        > MAX_APPLIANCE_PROJECTION_BYTES - RAW_PROJECTION_RESERVE_BYTES
                    ):
                        raise ValueError("Cycle profile exceeds bounded phase complexity")
                    if (
                        len(candidate.get("learned_profile", {}).get("power_w", ()))
                        > MAX_VARIABLE_PROFILE_STEPS - MAX_PROFILE_STEPS
                    ):
                        raise ValueError("Cycle profile exceeds bounded phase complexity")
                except ValueError as error:
                    if str(error) != "Cycle profile exceeds bounded phase complexity":
                        raise
                    record["training_usable"] = False
                    record["rejection"] = "complexity_limited"
                    result["status"] = "complexity_limited"
                else:
                    records, settings = candidate_records, candidate
                    data["devices"][entity] = settings
            extra.append((key, record))
        if extra:
            extra.insert(0, (frame, body))
        if result["running"] and not covers(
            coverage,
            instant(result["running"]["started_at"]) - timedelta(minutes=30),
            instant(result["running"]["observed_until"]),
        ):
            result["running"] = None
            result["status"] = "not_observed"
        result["cycles"] = [
            cycle
            for cycle in result["cycles"]
            if covers(
                coverage,
                instant(cycle["started_at"]) - timedelta(minutes=30),
                instant(cycle["completed_at"]) + timedelta(minutes=30),
            )
        ]
        settings["raw_history"] = {
            "power_entity": power_entity,
            "start": body["start"],
            "end": body["end"],
            "limited": body["limited"],
            "synthetic": body["synthetic"],
            "status": result["status"],
            "power_curve": result["power_curve"],
            "energy_curve": result["energy_curve"],
            "cycle_started_at": result["running"]["started_at"]
            if result["running"]
            else result["runs"][-1]["started_at"]
            if result["runs"]
            else None,
            "cycle_completed_at": result["runs"][-1]["completed_at"] if result["runs"] else None,
        }
        if result["running"]:
            settings["raw_history"]["cycle_completed_at"] = None
        settings["raw_history"]["curve_revision"] = digest(settings["raw_history"])
        observed_until = instant(
            result["running"]["observed_until"] if result["running"] else result["observed_until"]
        )
        self.observe_history(
            data,
            entity,
            [],
            [
                (instant(cycle["started_at"]), instant(cycle["completed_at"]), ())
                for cycle in result["cycles"]
            ],
            {"running": result["running"], "quiet": result["quiet"]},
            power_entity,
            now,
            observed_until=observed_until,
            processing_started=processing_started,
            raw_power=result["power_curve"],
        )
        self.commit(data, extra=extra)
        if previous_capture is None or end > instant(previous_capture):
            self._report_pairs.add(pair)
        self._cycle_records[(entity, power_entity)] = records

    def observed(self, entity):
        """Return unresolved source-bound measured running evidence, independently of task feedback."""
        run = self.state["devices"].get(entity, {}).get("observed_run")
        return (
            run
            if run
            and run["status"] not in ("complete", "idle")
            and run["power_entity"] == self.source(entity)
            else None
        )

    def set_deadline(self, entity, deadline, now):
        """Persist a dashboard's draft deadline without submitting or changing an accepted task."""
        self.member(entity)
        settings = self.state["devices"].get(entity, {})
        if not settings.get("shiftable"):
            raise ValueError("Enable shifting first")
        task = self.state["tasks"].get(entity)
        if task and task["status"] in OPEN_STATUSES:
            raise ValueError("Resolve the open task before changing its deadline")
        if deadline.utcoffset() is None or not now < deadline <= now + timedelta(
            days=MAX_TASK_WINDOW_DAYS
        ):
            raise ValueError("Finish-before deadline must be in the next seven days")
        data = deepcopy(self.state)
        data["devices"][entity]["finish_before"] = deadline.isoformat()
        for key in ("finish_date", "finish_time", "finish_time_zone"):
            data["devices"][entity].pop(key, None)
        self.commit(data)

    def set_deadline_field(self, entity, field, value, time_zone, now):
        """Persist independent local date/time input. Only a complete valid future draft enables readiness."""
        self.member(entity)
        settings = self.state["devices"].get(entity, {})
        task = self.state["tasks"].get(entity)
        if not settings.get("shiftable") or (task and task["status"] in OPEN_STATUSES):
            raise ValueError("Enable shifting and resolve the open task before editing")
        if (
            field not in ("date", "time")
            or not isinstance(value, str)
            or not isinstance(time_zone, str)
        ):
            raise ValueError("Invalid deadline field")
        try:
            zone = ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid deadline timezone") from None
        parsed = date.fromisoformat(value) if field == "date" else time.fromisoformat(value)
        if field == "time" and parsed.tzinfo is not None:
            raise ValueError("Use local time without an offset")
        data = deepcopy(self.state)
        changed = data["devices"][entity]
        if settings.get("finish_before"):
            previous = instant(settings["finish_before"]).astimezone(zone)
            changed.setdefault("finish_date", previous.date().isoformat())
            changed.setdefault("finish_time", previous.time().isoformat())
        changed["finish_" + field] = parsed.isoformat()
        changed["finish_time_zone"] = time_zone
        changed["finish_before"] = None
        if changed.get("finish_date") and changed.get("finish_time"):
            local = datetime.combine(
                date.fromisoformat(changed["finish_date"]),
                time.fromisoformat(changed["finish_time"]),
                zone,
            )
            # A repeated clock hour selects the first future occurrence. Nonexistent local times remain incomplete.
            for fold in (0, 1):
                candidate = local.replace(fold=fold).astimezone(UTC)
                if candidate.astimezone(zone).replace(tzinfo=None) == local.replace(
                    tzinfo=None
                ) and now < candidate <= now + timedelta(days=MAX_TASK_WINDOW_DAYS):
                    changed["finish_before"] = candidate.isoformat()
                    break
        self.commit(data)

    def record_cycle(self, entity, cycle_id, started_at, completed_at, points, *, synthetic=False):
        """Learn one explicitly completed measured cycle once. Never treat an idle pause as completion."""
        self.member(entity)
        if not isinstance(cycle_id, str) or not 1 <= len(cycle_id) <= MAX_CORRELATION_LENGTH:
            raise ValueError("A stable completed cycle identity is required")
        if not isinstance(points, list) or len(points) > MAX_CYCLE_OBSERVATIONS:
            raise ValueError("Too many cycle observations")
        key = "appliance-cycle-" + digest([entity, cycle_id]) + ".json"
        body = {
            "entity_id": entity,
            "cycle_id": cycle_id,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "points": points,
            "synthetic": synthetic,
        }
        if self.documents.exists(key):
            if self.documents.read(key) != body:
                raise ValueError("Completed cycle identity reused with different evidence")
            return
        from .evidence import completion, refine

        learned = learn_cycle(points, started_at, completed_at, synthetic=synthetic)
        self.sync_sources()
        power_entity = self.source(entity)
        records, _, _ = self.collect_cycles(entity, power_entity, (), synthetic)
        explicit = completion(body)
        explicit["_profile"] = learned
        records["appliance-cycle-" + explicit["id"]] = explicit
        data = deepcopy(self.state)
        settings = data["devices"].setdefault(entity, {"shiftable": False})
        refine(settings, list(records.values()))
        self.commit(data, extra=((key, body),))
        for pair in tuple(self._cycle_records):
            if pair[0] == entity:
                self._cycle_records.pop(pair)
        self._cycle_records[(entity, power_entity)] = records

    def submit(self, entity, request_id, ready_at, deadline, now, *, program=None):
        """Accept explicit readiness and freeze the selected or initial default profile."""
        from .programs import ADDITIONAL_PROGRAM, DEFAULT_PROGRAM

        self.member(entity)
        if program not in (None, DEFAULT_PROGRAM, ADDITIONAL_PROGRAM):
            raise ValueError("Choose the default or additional program")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= MAX_CORRELATION_LENGTH:
            raise ValueError("A stable readiness request identity is required")
        request_key = "appliance-request-" + digest([entity, request_id]) + ".json"
        request = {
            "entity_id": entity,
            "request_id": request_id,
            "ready_at": ready_at.isoformat(),
            "deadline": deadline.isoformat(),
            **({"program": program} if program else {}),
        }
        if self.documents.exists(request_key):
            saved = self.documents.read(request_key)
            if saved["request"] != request:
                raise ValueError("Readiness identity reused with different times or program")
            return self.documents.read("appliance-task-" + saved["task_id"] + ".json")
        settings = self.state["devices"].get(entity, {})
        if not settings.get("shiftable") or not settings.get("profile"):
            raise ValueError(
                "Enable shifting and wait for automatic learning or save a manual cycle profile first"
            )
        if (
            ready_at > deadline
            or deadline <= now
            or deadline - now > timedelta(days=MAX_TASK_WINDOW_DAYS)
        ):
            raise ValueError("Finish-before deadline must be in the next seven days")
        if self.observed(entity):
            raise ValueError(
                "Appliance activity is unresolved. Wait for quiet measurements before submitting readiness"
            )
        old = self.state["tasks"].get(entity)
        if old and old["status"] in OPEN_STATUSES:
            raise ValueError("This appliance already has an unresolved task")
        additional = settings.get("additional_profile")
        if program == ADDITIONAL_PROGRAM and not additional:
            raise ValueError("No additional program has been learned for this appliance")
        task = {
            **request,
            "id": str(uuid4()),
            "status": "pending",
            "profile": additional if program == ADDITIONAL_PROGRAM else settings["profile"],
            **({"additional_profile": additional} if additional and program is None else {}),
            "attempt": None,
            "events": [],
            "scheduled_start": None,
            "plan_id": None,
        }
        ApplianceWork(task["id"], entity, ready_at, deadline, profile(task["profile"]))
        data = deepcopy(self.state)
        data["tasks"][entity] = task
        self.commit(data, extra=((request_key, {"request": request, "task_id": task["id"]}),))
        return task

    def prepare(self, snapshot, demand, now, *, excluded_entities=(), replacement_windows=()):
        """Replace controlled baselines and refine task windows for physical peaks.

        Args:
            snapshot: Frozen installation inputs with AC interval energy in kWh.
            demand: Current demand model used to choose matching component forecasts.
            now: Aware calculation time used to check forecast freshness.
            excluded_entities: Meters replaced by separately modeled service even
                without an appliance task.
            replacement_windows: Tuples of (entity_id, left, right), with aware
                datetimes delimiting service replacement. Excluded meters without
                a task use these windows, defaulting to the full planning horizon.

        Returns:
            A copied snapshot with refined task intervals and load_components
            retaining original and remaining AC kWh. A task replaces a proportional
            expected share within its ready/deadline window, capped by remaining
            cycle energy. Other expectation remains. Original demand_evidence is
            retained and matching forecasts are copied into appliance_evidence.

        Side Effects:
            Clears self.issue on valid preparation. Missing or incompatible device
            forecasts set a not-ready issue. Expired, incomplete or invalid coverage
            sets a boundary-unavailable issue. Both retain the original household
            problem and update only the appliance revision. Unsupported problem
            types return unchanged. No task is started or persisted here.
        """
        if not isinstance(snapshot.problem, InstallationProblem):
            return snapshot
        from open_energy_planner.demand.calculation.composition import (
            LoadComponent,
            LoadStep,
            integrals,
            replace_cycle,
            scale_window,
            validate_components,
        )

        from .observed import work as observed_work
        from .programs import ADDITIONAL_PROGRAM

        problem = snapshot.problem
        self.issue = None
        self._phase_limited_entities = set()
        works, task_windows, entities = [], [], set(excluded_entities)
        origins_by_entity = {}
        for entity, task in self.state["tasks"].items():
            if task["status"] == "execution_observed" or (
                self.observed(entity) and not task["attempt"]
            ):
                continue
            if task["status"] not in OPEN_STATUSES:
                continue
            try:
                self.member(entity)
            except ValueError:
                continue
            attempt = task["attempt"]
            run = self.observed(entity)
            observed = (
                observed_work(entity, run, now) if run and run["status"] == "observed" else None
            )
            fixed = None
            if attempt and attempt["status"] != "not_started":
                fixed = instant(attempt.get("started_at") or attempt["start"])
                if not attempt.get("started_at") and observed:
                    fixed = observed.fixed_start
            detected_additional = (
                observed and not task.get("program") and (run.get("program") == ADDITIONAL_PROGRAM)
            )
            work = ApplianceWork(
                task["id"],
                entity,
                instant(task["ready_at"]),
                instant(task["deadline"]),
                observed.profile if detected_additional else profile(task["profile"]),
                observed.fixed_start if detected_additional else fixed,
            )
            starts = candidates(work, problem.intervals)
            if starts:
                origins_by_entity[entity] = starts
                works.append(work)
                entities.add(entity)
                task_windows.append(
                    (
                        work,
                        min(starts),
                        max(starts) + timedelta(seconds=work.profile.duration_seconds),
                    )
                )
        for entity in self.state["devices"]:
            if entity in entities:
                continue
            run = self.observed(entity)
            if not run or run["status"] != "observed":
                continue
            try:
                self.member(entity)
            except ValueError:
                continue
            observed = observed_work(entity, run, now)
            if observed:
                origins_by_entity[entity] = (observed.fixed_start,)
                works.append(observed)
                entities.add(entity)
                task_windows.append((observed, observed.fixed_start, observed.deadline))
        if not works and not entities and not self.consumers.selected:
            return replace(snapshot, appliance_revision=self.revision)
        horizon_seconds = sum(step.duration_h for step in problem.intervals) * 3600
        remaining_edges = (
            MAX_REFINED_PLAN_INTERVALS
            - len(problem.intervals)
            - int(horizon_seconds / SAMPLE_SECONDS)
        )
        remaining_checks = MAX_PHASE_BOUNDARY_CHECKS
        admitted = []
        uniform_boundaries = set()
        parent_boundaries = {
            boundary
            for step in problem.intervals
            for boundary in (step.start, step.start + timedelta(hours=step.duration_h))
        }

        def uniform_phase_boundaries(work, step):
            """Yield the existing absolute grid plus actual fixed or partial phase edges within one parent interval."""
            end = step.start + timedelta(hours=step.duration_h)
            phase_seconds = work.profile.phase_seconds
            boundary = (int(step.start.timestamp()) // phase_seconds + 1) * phase_seconds
            while boundary < end.timestamp():
                yield datetime.fromtimestamp(boundary, step.start.tzinfo)
                boundary += phase_seconds
            if work.fixed_start:
                for offset in work.profile.phase_boundaries:
                    boundary = work.fixed_start + timedelta(seconds=offset)
                    if step.start < boundary < end:
                        yield boundary
            elif work.profile.duration_seconds % phase_seconds:
                remainder = work.profile.duration_seconds % phase_seconds
                boundary = int(step.start.timestamp()) // phase_seconds * phase_seconds + remainder
                if boundary <= step.start.timestamp():
                    boundary += phase_seconds
                while boundary < end.timestamp():
                    yield datetime.fromtimestamp(boundary, step.start.tzinfo)
                    boundary += phase_seconds

        for work, first, last in task_windows:
            if work.profile.phase_durations_seconds is not None:
                edge_count = len(work.profile.power_w) + 1
                origin_count = len(origins_by_entity[work.entity_id])
                checks = (
                    edge_count
                    * origin_count
                    * sum(
                        step.start < last and step.start + timedelta(hours=step.duration_h) > first
                        for step in problem.intervals
                    )
                )
                if edge_count * origin_count > remaining_edges or checks > remaining_checks:
                    self._phase_limited_entities.add(work.entity_id)
                    if work.entity_id not in excluded_entities:
                        entities.discard(work.entity_id)
                    continue
                remaining_edges -= edge_count * origin_count
                remaining_checks -= checks
            else:
                additions, checks, extra_edges = set(), 0, 0
                fits = True
                for step in problem.intervals:
                    if step.start >= last or step.start + timedelta(hours=step.duration_h) <= first:
                        continue
                    for boundary in uniform_phase_boundaries(work, step):
                        checks += 1
                        if checks > remaining_checks:
                            fits = False
                            break
                        if boundary not in uniform_boundaries and boundary not in additions:
                            additions.add(boundary)
                            if (
                                boundary not in parent_boundaries
                                and boundary.timestamp() % SAMPLE_SECONDS
                            ):
                                extra_edges += 1
                            if (
                                extra_edges > remaining_edges
                                or len(uniform_boundaries) + len(additions)
                                > MAX_REFINED_PLAN_INTERVALS
                            ):
                                fits = False
                                break
                    if not fits:
                        break
                remaining_checks -= min(checks, remaining_checks)
                if not fits:
                    self._phase_limited_entities.add(work.entity_id)
                    if work.entity_id not in excluded_entities:
                        entities.discard(work.entity_id)
                    continue
                remaining_edges -= extra_edges
                uniform_boundaries.update(additions)
            admitted.append((work, first, last))
        task_windows = admitted
        uniform_boundaries = sorted(uniform_boundaries)
        works = [work for work in works if work.entity_id not in self._phase_limited_entities]
        result = self.consumers.result or {}
        forecasts = {row["entity_id"]: row["forecast"] for row in result.get("devices", [])}
        if (
            not forecasts
            or not entities <= set(forecasts)
            or (
                "separated_entities" in result
                and set(result["separated_entities"]) != set(forecasts)
            )
            or (
                "configuration_version" in result
                and result["configuration_version"] != self.consumers.version
            )
        ):
            self.issue = "Appliance forecasts not ready. Retaining household forecast"
            return replace(snapshot, appliance_revision=self.revision)

        def covered_series(forecast):
            """Require fresh complete evidence even for components removed from the future load."""
            issued_at = instant(forecast["issued_at"])
            if (
                not issued_at
                <= now
                < issued_at + timedelta(minutes=COMPONENT_FORECAST_MAX_AGE_MINUTES)
            ):
                raise ValueError("Appliance forecast expired")
            energy = tuple(
                LoadStep(
                    start=instant(row["start"]),
                    duration_h=row["duration_h"],
                    kwh=row["kwh"],
                )
                for row in forecast["steps"]
            )
            integrals(
                energy,
                tuple(
                    (step.start, step.start + timedelta(hours=step.duration_h))
                    for step in problem.intervals
                ),
            )
            return energy

        try:
            # Keep the active component models and their original measurement
            # boundary. A concrete task replaces expectation, not training data.
            horizon_start = problem.intervals[0].start
            horizon_end = problem.intervals[-1].start + timedelta(
                hours=problem.intervals[-1].duration_h
            )
            baseline_parts = [
                result["residual"]
                if demand is not None
                and getattr(getattr(demand, "forecast", None), "model", None)
                == "consumer-composite-v1"
                else result["residual_baseline"]
            ]
            base = covered_series(baseline_parts[0])
            components = [
                LoadComponent(
                    None,
                    baseline_parts[0].get("version") or digest(baseline_parts[0]),
                    base,
                    base,
                    tuple(
                        sorted(
                            {row["entity_id"] for row in self.consumers.selected} - set(forecasts)
                        )
                    ),
                )
            ]
            work_by_entity = {work.entity_id: work for work in works}
            windows = {entity: (left, right) for entity, left, right in replacement_windows}
            for row in result["devices"]:
                if row["entity_id"] in entities:
                    covered_series(row["forecast"])
                    covered_series(row["baseline_forecast"])
                forecast = (
                    row["forecast"]
                    if demand is not None
                    and getattr(getattr(demand, "forecast", None), "model", None)
                    == "consumer-composite-v1"
                    else row["baseline_forecast"]
                )
                expected = covered_series(forecast)
                entity = row["entity_id"]
                if entity in work_by_entity:
                    remaining = replace_cycle(
                        expected, work_by_entity[entity], horizon_start, horizon_end
                    )
                elif entity in entities:
                    remaining = scale_window(
                        expected, *windows.get(entity, (horizon_start, horizon_end)), 1
                    )
                else:
                    remaining = expected
                components.append(
                    LoadComponent(
                        entity, forecast.get("version") or digest(forecast), expected, remaining
                    )
                )
                if entity not in entities:
                    baseline_parts.append(forecast)
            component_boundaries = sorted(
                {part.start for component in components for part in component.steps}
            )
            intervals, parents = [], []
            for parent_index, step in enumerate(problem.intervals):
                end = step.start + timedelta(hours=step.duration_h)
                boundaries = {step.start, end}
                boundaries.update(
                    component_boundaries[
                        bisect_right(component_boundaries, step.start) : bisect_left(
                            component_boundaries, end
                        )
                    ]
                )
                boundaries.update(
                    uniform_boundaries[
                        bisect_right(uniform_boundaries, step.start) : bisect_left(
                            uniform_boundaries, end
                        )
                    ]
                )
                for work, first, last in task_windows:
                    if end <= first or step.start >= last:
                        continue
                    if work.profile.phase_durations_seconds is not None:
                        for origin in origins_by_entity[work.entity_id]:
                            for offset in work.profile.phase_boundaries:
                                instant_boundary = origin + timedelta(seconds=offset)
                                if step.start < instant_boundary < end:
                                    boundaries.add(instant_boundary)
                        continue
                from itertools import pairwise

                for left, right in pairwise(sorted(boundaries)):
                    # Preserve forecast means. Refine only task windows to retain actual phase peaks.
                    part = (
                        step
                        if left == step.start and right == end
                        else slice_interval(step, left, right)
                    )
                    intervals.append(part)
                    parents.append(parent_index)
                    if len(intervals) > MAX_REFINED_PLAN_INTERVALS:
                        raise ValueError(
                            "Appliance phase refinement exceeds bounded planning complexity"
                        )
            windows = tuple(
                (part.start, part.start + timedelta(hours=part.duration_h)) for part in intervals
            )
            contributions = [integrals(component.steps, windows) for component in components]
            from open_energy_planner.appliances.cycles.model import ENERGY_TOLERANCE_KWH

            for index, part in enumerate(intervals):
                residual = fsum(series[index] for series in contributions)
                if residual < -ENERGY_TOLERANCE_KWH:
                    raise ValueError("Appliance forecast exceeds household boundary")
                intervals[index] = replace(part, load_kwh=max(0, residual))
            refined = replace(
                problem,
                intervals=tuple(intervals),
                appliances=tuple(works),
                export_bonuses=tuple(
                    replace(
                        rule,
                        rates=tuple(rule.rates[index] for index in parents),
                        caps_kwh=tuple(rule.caps_kwh[index] for index in parents),
                        periods=tuple(rule.periods[index] for index in parents),
                        prevent_battery_export=tuple(
                            rule.prevent_battery_export[index] for index in parents
                        )
                        if rule.prevent_battery_export
                        else (),
                    )
                    for rule in problem.export_bonuses
                ),
            )
            validate_components(refined, components)
        except (ValueError, KeyError):
            self.issue = "Appliance forecast boundary unavailable. Retaining household forecast"
            return replace(snapshot, appliance_revision=self.revision)
        self.issue = None
        # appliance_evidence copies baseline inputs. Keep the original demand archive references.
        return replace(
            snapshot,
            problem=refined,
            appliance_revision=self.revision,
            appliance_evidence=tuple(deepcopy(baseline_parts)),
            load_components=tuple(components),
        )

    def accept_plan(self, document):
        """Retain a validated schedule. No start attempt is authorized just by calculating it."""
        data = deepcopy(self.state)
        rows = {row["task_id"]: row for row in document.get("appliance_schedule", [])}
        for task in data["tasks"].values():
            if task["status"] == "execution_observed" or self.observed(task["entity_id"]):
                continue
            if task["status"] not in OPEN_STATUSES:
                continue
            row = rows.get(task["id"])
            if task["attempt"] and task["attempt"]["status"] != "not_started":
                if row:
                    task["plan_id"] = document["plan_id"]
                continue
            if row:
                task.update(
                    status="scheduled", scheduled_start=row["start"], plan_id=document["plan_id"]
                )
            else:
                task.update(
                    status="forecast_not_ready"
                    if self.issue or task["entity_id"] in self._phase_limited_entities
                    else "deadline_not_achievable",
                    scheduled_start=None,
                    plan_id=None,
                )
        self.commit(data)

    def tick(self, now, enabled, plan):
        """Expire unconfirmed attempts durably and issue only still-valid scheduled requests."""
        data = deepcopy(self.state)
        for entity, task in data["tasks"].items():
            if task["status"] == "cancelled":
                continue
            attempt = task["attempt"]
            if (
                attempt
                and attempt["status"] in ("requested", "claimed")
                and now >= instant(attempt["expires_at"])
            ):
                attempt["status"] = "unknown"
                if task["status"] != "execution_observed":
                    task["status"] = "start_not_confirmed"
            if (
                self.observed(entity)
                or task["status"] != "scheduled"
                or not enabled
                or not plan
                or self.issue
            ):
                continue
            try:
                self.member(entity)
            except ValueError:
                continue
            start = instant(task["scheduled_start"])
            if (
                task["plan_id"] != plan["plan_id"]
                or now >= instant(plan["valid_until"])
                or now < start
            ):
                continue
            latest = instant(task["deadline"]) - timedelta(
                seconds=profile(task["profile"]).duration_seconds + DURATION_ALLOWANCE_SECONDS
            )
            expires = min(
                now + timedelta(seconds=CONFIRMATION_SECONDS), latest, instant(plan["valid_until"])
            )
            if now >= expires:
                task.update(status="deadline_not_achievable", scheduled_start=None)
                continue
            attempt = {
                "id": str(uuid4()),
                "status": "requested",
                "start": start.isoformat(),
                "expires_at": expires.isoformat(),
                "issued_at": now.isoformat(),
            }
            task.update(attempt=attempt, status="requested")
        self.commit(data)

    def claim(self, entity, task_id, attempt_id, now, enabled, plan):
        """Claim once and return the effective lease deadline for admission after response delivery."""
        task = self.match(entity, task_id, attempt_id)
        attempt = task["attempt"]
        self.member(entity)
        if not plan:
            raise ValueError("Start request expired, superseded, paused or already claimed")
        expires_at = min(instant(plan["valid_until"]), instant(attempt["expires_at"]))
        if (
            task["status"] in ("cancelled", "execution_observed")
            or self.observed(entity)
            or self.issue
            or not enabled
            or task["plan_id"] != plan["plan_id"]
            or now >= expires_at
            or attempt["status"] != "requested"
        ):
            raise ValueError("Start request expired, superseded, paused or already claimed")
        data = deepcopy(self.state)
        data["tasks"][entity]["attempt"]["status"] = "claimed"
        self.commit(data)
        return {
            "authorized": True,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "expires_at": expires_at.isoformat(),
        }

    def cancel_task(self, entity, task_id):
        """Withdraw planning at any stage. Retain physical evidence and leave equipment decisions to HA."""
        task = self.state["tasks"].get(entity)
        if not task or task["id"] != task_id:
            raise ValueError("Unknown appliance task")
        if task["status"] == "cancelled":
            return task
        if task["status"] == "completed":
            raise ValueError("Completed work cannot be cancelled")
        data = deepcopy(self.state)
        data["tasks"][entity].update(status="cancelled", scheduled_start=None, plan_id=None)
        self.commit(data)
        return data["tasks"][entity]

    def match(self, entity, task_id, attempt_id, *, archived=False):
        """Return exactly correlated current work or an explicitly allowed cancelled archive.

        Args:
            entity: Energy entity that owns the task.
            task_id: Exact task identity supplied with the observation or claim.
            attempt_id: Exact physical start-attempt identity.
            archived: Allow a cancelled task's durable record when current work
                has a different identity. The returned record may be historical.
                It must never be treated as the current planning task. Claims
                deliberately use the default current-task-only lookup.

        Raises:
            ValueError: An identity is malformed or no exact source/task/attempt matches.
        """
        if not all(
            isinstance(value, str) and 1 <= len(value) <= MAX_CORRELATION_LENGTH
            for value in (task_id, attempt_id)
        ):
            raise ValueError("Invalid task/attempt identity")
        task = self.state["tasks"].get(entity)
        if archived and (not task or task["id"] != task_id):
            key = "appliance-task-" + task_id + ".json"
            task = self.documents.read(key) if self.documents.exists(key) else None
            if task and (task.get("entity_id") != entity or task.get("status") != "cancelled"):
                task = None
        if (
            not task
            or task["id"] != task_id
            or not task["attempt"]
            or task["attempt"]["id"] != attempt_id
        ):
            raise ValueError("Unknown or stale appliance task/attempt")
        return task

    def feedback(self, entity, task_id, attempt_id, event, at, now, *, cancelled=False):
        """Accept physical outcomes idempotently without reopening cancelled planning.

        An automatic retry still requires confirmed nonexecution. Exact feedback
        for superseded cancelled work updates only its canonical historical task
        and an immutable feedback snapshot in one transaction. It does not change
        self.state or the replacement task's planner revision. The returned task
        may therefore differ from current planning work.
        """
        task = self.match(entity, task_id, attempt_id, archived=True)
        attempt = task["attempt"]
        evidence = {
            "attempt_id": attempt_id,
            "event": event,
            "at": at.isoformat(),
            "cancelled": cancelled,
        }
        if evidence in task["events"]:
            return task
        if event not in ("started", "not_started", "completed") or type(cancelled) is not bool:
            raise ValueError("Unsupported execution feedback")
        if at > now + timedelta(seconds=1) or at < instant(attempt["issued_at"]):
            raise ValueError("Execution feedback has an invalid event time")
        if len(task["events"]) >= MAX_FEEDBACK_EVENTS:
            raise ValueError("Too many execution events")
        status = attempt["status"]
        if event == "started" and status not in ("requested", "claimed", "unknown"):
            raise ValueError("Start feedback contradicts execution state")
        if event == "not_started" and (
            not cancelled or status not in ("requested", "claimed", "unknown")
        ):
            raise ValueError("Confirm that this attempt cannot execute later")
        if event == "completed" and (status != "started" or at < instant(attempt["started_at"])):
            raise ValueError("Completion requires confirmed actual start")
        data = deepcopy(self.state)
        historical = self.state["tasks"].get(entity, {}).get("id") != task_id
        changed = deepcopy(task) if historical else data["tasks"][entity]
        changed["events"].append(evidence)
        changed["attempt"]["status"] = event
        if event == "started":
            changed["attempt"]["started_at"] = at.isoformat()
            changed["status"] = "running"
        elif event == "completed":
            changed["attempt"]["completed_at"] = at.isoformat()
            changed["status"] = "completed"
        else:
            changed.update(status="pending", scheduled_start=None, plan_id=None)
        if task["status"] in ("cancelled", "execution_observed"):
            changed["status"] = task["status"]
        if historical:
            with self.documents.transaction():
                self.documents.write(
                    "appliance-feedback-" + digest([task_id, evidence]) + ".json", changed
                )
                self.documents.write("appliance-task-" + task_id + ".json", changed)
        else:
            self.commit(data)
        return changed

    def document(self, now=None, enabled=False, plan=None, *, raw_entity=None):
        """Expose current work and learned profile evidence. Archived transitions remain in the database."""
        return self._projection(self.state, now, enabled, plan, raw_entity=raw_entity)

    def _projection(self, state, now=None, enabled=False, plan=None, *, raw_entity=None):
        """Keep ordinary all-device reads small and bind full raw detail to one explicitly selected entity."""
        from .history import request as history_request

        names = {row["entity_id"]: row["name"] for row in self.consumers.options}
        active = {row["entity_id"] for row in self.consumers.selected}
        devices = []
        for entity, settings in state["devices"].items():
            if entity not in active or raw_entity is not None and entity != raw_entity:
                continue
            projection = settings
            if settings.get("raw_history", {}).get("power_entity") not in (
                None,
                self.source(entity),
            ):
                projection = {key: value for key, value in settings.items() if key != "raw_history"}
            elif entity != raw_entity and settings.get("raw_history"):
                projection = {
                    **settings,
                    "raw_history": {
                        key: value
                        for key, value in settings["raw_history"].items()
                        if key not in ("power_curve", "energy_curve")
                    },
                }
            task = state["tasks"].get(entity)
            attempt = task["attempt"] if task else None
            request = None
            if (
                not self.issue
                and not self.observed(entity)
                and now
                and enabled
                and plan
                and attempt
                and attempt["status"] == "requested"
                and task["status"] != "execution_observed"
                and task["plan_id"] == plan["plan_id"]
                and now < min(instant(attempt["expires_at"]), instant(plan["valid_until"]))
            ):
                request = {
                    "task_id": task["id"],
                    "attempt_id": attempt["id"],
                    "expires_at": attempt["expires_at"],
                }
            devices.append(
                {
                    "entity_id": entity,
                    "name": names.get(entity, entity),
                    **projection,
                    "power_entity": self.source(entity),
                    "planning_issue": "This cycle cannot currently be scheduled. Its measured curve is retained and ordinary consumption forecasts remain in use."
                    if entity in self._phase_limited_entities
                    else None,
                    "history_request": history_request(settings),
                    "fine_history_request": history_request(settings, resolution="5minute"),
                    "learning_half_life_days": RECENCY_HALF_LIFE_DAYS
                    if "learning_weight" in settings or "hourly_weight" in settings
                    else None,
                    "profile_kind": "Manual"
                    if settings.get("profile", {}).get("evidence") == "manual"
                    else "Historical estimate"
                    if settings.get("profile", {}).get("evidence") == "hourly_estimate"
                    else "Learned"
                    if settings.get("profile")
                    else None,
                    "learning_status": "missing_power_sensor"
                    if not self.source(entity)
                    else "ready"
                    if settings.get("learned_profile", {}).get("samples", 0) >= MIN_COMPLETE_CYCLES
                    else "provisional"
                    if settings.get("profile", {}).get("evidence") == "hourly_estimate"
                    else "waiting_for_complete_cycles",
                    "task": task,
                    "request": request,
                }
            )
        return {"revision": self.revision, "issue": self.issue, "devices": devices}
