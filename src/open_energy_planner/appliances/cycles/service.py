"""Durable application authority for appliance tasks, attempts, feedback and cycle evidence."""

from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
from math import fsum
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from open_energy_planner.appliances.cycles.learning import (
    MAX_RECENT_CYCLES,
    MIN_COMPLETE_CYCLES,
    RECENCY_HALF_LIFE_DAYS,
    detect_cycles,
    history_evidence,
    manual_profile,
    retained_weight,
)
from open_energy_planner.appliances.cycles.model import (
    CONFIRMATION_SECONDS,
    DURATION_ALLOWANCE_SECONDS,
    PROFILE_STEP_SECONDS,
    ApplianceWork,
    CycleProfile,
    average_profiles,
    candidates,
    instant,
    learn_cycle,
)
from open_energy_planner.appliances.sources import validate_membership
from open_energy_planner.demand.calculation.model import EnergyTerm, digest
from open_energy_planner.installation.model import InstallationProblem, slice_interval

STATE_KEY = "appliance-state.json"
PROGRESS_KEY = "appliance-progress.json"
PROGRESS_FIELDS = ("learning_history", "history_import")
MAX_CORRELATION_LENGTH = 64
MAX_CYCLE_OBSERVATIONS = 2048
MAX_FEEDBACK_EVENTS = 1024
MAX_TASK_WINDOW_DAYS = 7
COMPONENT_FORECAST_MAX_AGE_MINUTES = 30
OPEN_STATUSES = (
    "pending",
    "scheduled",
    "requested",
    "start_not_confirmed",
    "running",
    "deadline_not_achievable",
    "forecast_not_ready",
)


def profile(value):
    """Restore an immutable validated average profile from its durable JSON record."""
    return CycleProfile(tuple(value["power_w"]), value["samples"], value["evidence"])


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
        if candidate == self.state:
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

    def source(self, entity):
        """Bind learning to the authoritative Energy energy/power pair."""
        option = next((row for row in self.consumers.options if row["entity_id"] == entity), {})
        return option.get("power_entity")

    def sync_sources(self):
        """Withdraw learned profiles after a sensor change, retaining manual estimates and archives."""
        data = deepcopy(self.state)
        for entity, settings in data["devices"].items():
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
                settings.pop("learned_profile", None)
                settings.pop("learning_source", None)
                settings.pop("recent_cycles", None)
                settings.pop("last_detected_end", None)
                settings.pop("learning_weight", None)
                settings.pop("first_detected_start", None)
                settings.pop("profile", None)
                if settings.get("manual_profile"):
                    settings["profile"] = asdict(manual_profile(**settings["manual_profile"]))
        self.commit(data)

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

    def weighted_prior(self, entity, power_entity, settings):
        """Recover timestamp weights once from retained automatic-cycle evidence.

        Existing unweighted profiles remain usable until new evidence arrives.
        Reconstruct their exact current-source sample set without dropping older
        cycles or treating an aggregate as if every sample had the same age.

        Returns:
            Prior profile, recency weight at its latest cycle and earliest ISO
            cycle start. With no learned profile, returns None, 0.0 and None.
            Existing weighted models return their saved values. Legacy models
            return an aggregate reconstructed from their current-source archives.

        Raises:
            ValueError: Archives cannot reproduce the saved sample count or
                contain invalid cycle evidence. The stored profile is unchanged.
        """
        previous = settings.get("learned_profile")
        if not previous:
            return None, 0.0, None
        if "learning_weight" in settings:
            return profile(previous), settings["learning_weight"], settings["first_detected_start"]
        records = [
            record
            for record in self.documents.iter_prefix("appliance-detected-")
            if record["entity_id"] == entity
            and record["power_entity"] == power_entity
            and instant(record["completed_at"]) <= instant(settings["last_detected_end"])
        ]
        records.sort(key=lambda record: instant(record["completed_at"]))
        records = records[-previous["samples"] :]
        if len(records) != previous["samples"]:
            raise ValueError("Archived cycle evidence is incomplete. Retained profile is unchanged")
        aggregate, weight, last_end = None, 0.0, None
        for record in records:
            completed = instant(record["completed_at"])
            learned = learn_cycle(
                record["points"],
                instant(record["started_at"]),
                completed,
                synthetic=record["synthetic"],
            )
            if last_end:
                weight = retained_weight(weight, last_end, completed)
            aggregate = average_profiles(aggregate, learned, previous_weight=weight)
            weight, last_end = weight + 1, completed
        return aggregate, weight, records[0]["started_at"]

    def learn_history(self, entity, power_entity, rows, synthetic, now):
        """Learn complete detected cycles once from the current Energy pair.

        Two complete cycles admit the learned estimate. The manual fallback stays
        archived and accepted tasks retain their frozen profile. Detection never
        grants readiness or reports an execution outcome.
        """
        self.member(entity)
        if type(synthetic) is not bool or not power_entity or power_entity != self.source(entity):
            raise ValueError("History must use the current Home Assistant Energy power sensor")
        cycles = detect_cycles(rows, now)
        self.sync_sources()
        for started_at, completed_at, points in cycles:
            cycle_id = digest(
                [entity, power_entity, started_at.isoformat(), completed_at.isoformat()]
            )
            key = "appliance-detected-" + cycle_id + ".json"
            if self.documents.exists(key):
                continue
            current = self.state["devices"].get(entity, {})
            if current.get("last_detected_end") and started_at < instant(
                current["last_detected_end"]
            ):
                continue
            try:
                learned = learn_cycle(points, started_at, completed_at, synthetic=synthetic)
            except ValueError:
                # Invalid counter resolution in one cycle must not block later valid evidence.
                continue
            energy_kwh = learned.energy_kwh
            data = deepcopy(self.state)
            settings = data["devices"].setdefault(entity, {"shiftable": False})
            previous, weight, first_start = self.weighted_prior(entity, power_entity, settings)
            if previous:
                weight = retained_weight(
                    weight, instant(settings["last_detected_end"]), completed_at
                )
            learned = average_profiles(previous, learned, previous_weight=weight)
            settings["learning_weight"] = weight + 1
            settings["first_detected_start"] = first_start or started_at.isoformat()
            settings["learning_source"] = power_entity
            settings["last_detected_end"] = completed_at.isoformat()
            settings["learned_profile"] = asdict(learned)
            record = {
                "id": cycle_id,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "energy_kwh": energy_kwh,
                "synthetic": synthetic,
            }
            settings["recent_cycles"] = [*settings.get("recent_cycles", []), record][
                -MAX_RECENT_CYCLES:
            ]
            if learned.samples >= MIN_COMPLETE_CYCLES:
                settings["profile"] = asdict(learned)
            self.commit(
                data,
                extra=(
                    (
                        key,
                        {
                            **record,
                            "entity_id": entity,
                            "power_entity": power_entity,
                            "points": points,
                        },
                    ),
                ),
            )
        data = deepcopy(self.state)
        summary = history_evidence(rows, len(cycles))
        summary["power_entity"] = power_entity
        summary["synthetic"] = synthetic
        data["devices"].setdefault(entity, {"shiftable": False})["learning_history"] = summary
        self.commit(data)

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
        learned = learn_cycle(points, started_at, completed_at, synthetic=synthetic)
        data = deepcopy(self.state)
        settings = data["devices"].setdefault(entity, {"shiftable": False})
        previous = settings.get("profile")
        old = (
            profile(previous)
            if previous and previous["evidence"] in ("measured", "synthetic")
            else None
        )
        learned = average_profiles(old, learned)
        settings["profile"] = asdict(learned)
        self.commit(data, extra=((key, body),))

    def submit(self, entity, request_id, ready_at, deadline, now):
        """Create one idempotent runnable task only on explicit readiness, never historical usage timing."""
        self.member(entity)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= MAX_CORRELATION_LENGTH:
            raise ValueError("A stable readiness request identity is required")
        request_key = "appliance-request-" + digest([entity, request_id]) + ".json"
        request = {
            "entity_id": entity,
            "request_id": request_id,
            "ready_at": ready_at.isoformat(),
            "deadline": deadline.isoformat(),
        }
        if self.documents.exists(request_key):
            saved = self.documents.read(request_key)
            if saved["request"] != request:
                raise ValueError("Readiness identity reused with different times")
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
        old = self.state["tasks"].get(entity)
        if old and old["status"] in OPEN_STATUSES:
            raise ValueError("This appliance already has an unresolved task")
        task = {
            **request,
            "id": str(uuid4()),
            "status": "pending",
            "profile": settings["profile"],
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

        problem = snapshot.problem
        self.issue = None
        works, task_windows, entities = [], [], set(excluded_entities)
        for entity, task in self.state["tasks"].items():
            if task["status"] not in OPEN_STATUSES:
                continue
            try:
                self.member(entity)
            except ValueError:
                continue
            attempt = task["attempt"]
            fixed = (
                instant(attempt.get("started_at") or attempt["start"])
                if attempt and attempt["status"] != "not_started"
                else None
            )
            work = ApplianceWork(
                task["id"],
                entity,
                instant(task["ready_at"]),
                instant(task["deadline"]),
                profile(task["profile"]),
                fixed,
            )
            starts = candidates(work, problem.intervals)
            if starts:
                works.append(work)
                entities.add(entity)
                task_windows.append(
                    (
                        work,
                        min(starts),
                        max(starts) + timedelta(seconds=work.profile.duration_seconds),
                    )
                )
        if not works and not entities and not self.consumers.selected:
            return replace(snapshot, appliance_revision=self.revision)
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
                for work, first, last in task_windows:
                    if end <= first or step.start >= last:
                        continue
                    boundary = (
                        int(step.start.timestamp()) // PROFILE_STEP_SECONDS + 1
                    ) * PROFILE_STEP_SECONDS
                    while boundary < end.timestamp():
                        boundaries.add(step.start.fromtimestamp(boundary, step.start.tzinfo))
                        boundary += PROFILE_STEP_SECONDS
                    if work.fixed_start:
                        boundaries.update(
                            work.fixed_start + timedelta(seconds=index * PROFILE_STEP_SECONDS)
                            for index in range(len(work.profile.power_w) + 1)
                            if step.start
                            < work.fixed_start + timedelta(seconds=index * PROFILE_STEP_SECONDS)
                            < end
                        )
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
                    status="forecast_not_ready" if self.issue else "deadline_not_achievable",
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
                task["status"] = "start_not_confirmed"
            if task["status"] != "scheduled" or not enabled or not plan or self.issue:
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
            task["status"] == "cancelled"
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
        if task["status"] == "cancelled":
            changed["status"] = "cancelled"
        if historical:
            with self.documents.transaction():
                self.documents.write(
                    "appliance-feedback-" + digest([task_id, evidence]) + ".json", changed
                )
                self.documents.write("appliance-task-" + task_id + ".json", changed)
        else:
            self.commit(data)
        return changed

    def document(self, now=None, enabled=False, plan=None):
        """Expose current work and learned profile evidence. Archived transitions remain in the database."""
        from .history import request as history_request

        names = {row["entity_id"]: row["name"] for row in self.consumers.options}
        active = {row["entity_id"] for row in self.consumers.selected}
        devices = []
        for entity, settings in self.state["devices"].items():
            if entity not in active:
                continue
            task = self.state["tasks"].get(entity)
            attempt = task["attempt"] if task else None
            request = None
            if (
                not self.issue
                and now
                and enabled
                and plan
                and attempt
                and attempt["status"] == "requested"
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
                    **settings,
                    "power_entity": self.source(entity),
                    "history_request": history_request(settings),
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
