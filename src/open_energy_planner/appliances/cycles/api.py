"""Authenticated task transport. The application service owns lifecycle and physical execution stays in HA."""

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial
from time import monotonic

from aiohttp import web

from open_energy_planner.appliances.cycles import history as historical
from open_energy_planner.appliances.cycles.learning import HOURLY_SAMPLE_SECONDS, SAMPLE_SECONDS
from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.cycles.raw import MAX_RAW_BODY_BYTES
from open_energy_planner.appliances.sources import SOURCE_ALREADY_ASSIGNED
from open_energy_planner.runtime.diagnostics.logging import request_error_message
from open_energy_planner.storage.writes import complete_write

# Only these reviewed owned literals may cross the public error boundary.
PUBLIC_VALIDATION_MESSAGES = (
    SOURCE_ALREADY_ASSIGNED,
    "Appliance activity is unresolved. Wait for quiet measurements before submitting readiness",
    "A complete measured cycle profile is required",
    "Additional profile learning must be boolean",
    "A stable completed cycle identity is required",
    "A stable readiness request identity is required",
    "A timezone-aware timestamp is required",
    "An appliance operation must be an object",
    "An immutable cycle profile is required",
    "Appliance forecast exceeds household boundary",
    "Appliance forecast expired",
    "Appliance settings changed. Reload before saving",
    "Archived cycle evidence is incomplete. Retained profile is unchanged",
    "Choose the default or additional program",
    "Completed cycle identity reused with different evidence",
    "Completed work cannot be cancelled",
    "Completion requires confirmed actual start",
    "Confirm that this attempt cannot execute later",
    "Cycle duration needs at least one quarter and at most twelve hours",
    "Cycle endpoint missing",
    "Cycle evidence must describe a completed measured run",
    "Cycle history does not cover the complete run",
    "Cycle history has gaps, resets or coarse observations",
    "Cycle weighting requires chronological evidence",
    "Enable shifting and resolve the open task before editing",
    "Enable shifting and wait for automatic learning or save a manual cycle profile first",
    "Enable shifting first",
    "Enter a duration of 15 to 720 minutes and positive energy in kWh",
    "Execution feedback has an invalid event time",
    "Fine cumulative energy history is required",
    "Finish-before deadline must be in the next seven days",
    "History must use the current Home Assistant Energy power sensor",
    "History needs ordered completed intervals at the declared resolution",
    "History progress changed. Refresh before continuing",
    "History scan must be initialized for this source",
    "History source range changed. Retained learning is unchanged",
    "History target cannot move backwards",
    "Hourly estimate has a counter reset",
    "Hourly estimate has a history gap",
    "Hourly estimate needs complete finite hourly counter evidence",
    "Hourly samples exceed the requested batch",
    "Invalid appliance history quantity type",
    "Invalid appliance history sample",
    "Invalid appliance task identity",
    "Invalid appliance time window",
    "Invalid deadline field",
    "Invalid deadline timezone",
    "Invalid history provenance",
    "Invalid history scan phase",
    "Invalid hourly cycle duration",
    "Invalid hourly history bounds",
    "Invalid retained cycle weight",
    "Invalid task/attempt identity",
    "Manual average power exceeds 20000 W",
    "Only processing accepts history bounds",
    "No additional program has been learned for this appliance",
    "Readiness identity reused with different times or program",
    "Resolve the open task before changing its deadline",
    "Resolve the open task before changing shift permission",
    "Select this consumer in Appliances settings first",
    "Shift permission must be boolean",
    "Start feedback contradicts execution state",
    "Start request expired, superseded, paused or already claimed",
    "This appliance already has an unresolved task",
    "Too many appliance history samples",
    "Too many cycle observations",
    "Too many execution events",
    "Too many shiftable appliances",
    "Unknown appliance task",
    "Unknown or stale appliance task/attempt",
    "Unsupported appliance operation or fields",
    "Unsupported cycle history resolution",
    "Unsupported execution feedback",
    "Use local time without an offset",
)


def _configuration_action(operation, body, service, ev, thermal):
    """Decode settings edits and enforce exclusive ownership of shiftable sources."""
    if operation == "device" and set(body) in (
        {"entity_id", "shiftable"},
        {"entity_id", "shiftable", "expected_version"},
    ):
        if body["shiftable"] and body["entity_id"] in (
            {row["energy_entity"] for row in ev.state["points"]} | thermal.meters
        ):
            raise ValueError(SOURCE_ALREADY_ASSIGNED)
        return partial(
            service.configure,
            body["entity_id"],
            body["shiftable"],
            expected_version=body.get("expected_version"),
        )
    if operation == "profile_options" and set(body) == {
        "entity_id",
        "learn_additional_profile",
        "expected_version",
    }:
        return partial(
            service.configure_additional_profile,
            body["entity_id"],
            body["learn_additional_profile"],
            body["expected_version"],
        )
    if operation == "manual_profile" and set(body) == {
        "entity_id",
        "duration_minutes",
        "energy_kwh",
        "expected_version",
    }:
        return partial(
            service.set_manual_profile,
            body["entity_id"],
            body["duration_minutes"],
            body["energy_kwh"],
            body["expected_version"],
        )
    return None


def _learning_action(operation, body, service, now, processing_started):
    """Decode current or explicit completed-cycle evidence at its original timing."""
    if operation == "raw_history" and set(body) - {"observer_session"} == {
        "entity_id",
        "power_entity",
        "start",
        "end",
        "energy_states",
        "power_states",
        "energy_reported_at",
        "power_reported_at",
        "limited",
        "synthetic",
    }:
        return partial(service.learn_raw_history, body, now, processing_started=processing_started)
    if operation == "history" and set(body) - {"resolution"} == {
        "entity_id",
        "power_entity",
        "rows",
        "synthetic",
    }:
        return partial(
            service.learn_history,
            body["entity_id"],
            body["power_entity"],
            body["rows"],
            body["synthetic"],
            now,
            sample_seconds={"hour": HOURLY_SAMPLE_SECONDS, "5minute": SAMPLE_SECONDS}[
                body.get("resolution", "5minute")
            ],
            processing_started=processing_started,
        )
    if operation == "cycles" and set(body) == {
        "entity_id",
        "cycle_id",
        "started_at",
        "completed_at",
        "points",
        "synthetic",
    }:
        if type(body["synthetic"]) is not bool or instant(body["completed_at"]) > now:
            raise ValueError("Cycle evidence must describe a completed measured run")
        return partial(
            service.record_cycle,
            body["entity_id"],
            body["cycle_id"],
            instant(body["started_at"]),
            instant(body["completed_at"]),
            body["points"],
            synthetic=body["synthetic"],
        )
    return None


def _scan_action(operation, body, service, now):
    """Decode resumable Recorder scan and historical batch operations."""
    if operation == "history_scan" and set(body) - {"resolution"} == {
        "entity_id",
        "power_entity",
        "phase",
        "start",
        "end",
    }:
        return partial(
            historical.scan,
            service,
            body["entity_id"],
            body["power_entity"],
            body["phase"],
            body["start"],
            body["end"],
            now=now,
            resolution=body.get("resolution", "hour"),
        )
    if operation in ("hourly_history", "fine_history") and set(body) == {
        "entity_id",
        "power_entity",
        "rows",
        "synthetic",
        "start",
        "end",
        "read_start",
        "read_end",
    }:
        return partial(
            historical.batch,
            service,
            body["entity_id"],
            body["power_entity"],
            body["rows"],
            body["synthetic"],
            body["start"],
            body["end"],
            body["read_start"],
            body["read_end"],
            now=now,
            resolution="5minute" if operation == "fine_history" else "hour",
        )
    return None


def _task_action(operation, body, service, now, repository):
    """Decode readiness, deadline and execution events with their original authority."""
    if operation == "deadline" and set(body) == {"entity_id", "deadline"}:
        return partial(service.set_deadline, body["entity_id"], instant(body["deadline"]), now)
    if operation == "deadline_field" and set(body) == {
        "entity_id",
        "field",
        "value",
        "time_zone",
    }:
        return partial(
            service.set_deadline_field,
            body["entity_id"],
            body["field"],
            body["value"],
            body["time_zone"],
            now,
        )
    if operation == "tasks" and set(body) - {"program"} == {
        "entity_id",
        "request_id",
        "ready_at",
        "deadline",
    }:
        return partial(
            service.submit,
            body["entity_id"],
            body["request_id"],
            instant(body["ready_at"]),
            instant(body["deadline"]),
            now,
            program=body.get("program"),
        )
    if operation == "cancel" and set(body) == {"entity_id", "task_id"}:
        return partial(service.cancel_task, body["entity_id"], body["task_id"])
    if operation == "claim" and set(body) == {"entity_id", "task_id", "attempt_id"}:

        def claim_current():
            """Check expiry when the operation runs, after lock and worker queue waits."""
            return service.claim(
                body["entity_id"],
                body["task_id"],
                body["attempt_id"],
                datetime.now(UTC),
                repository.state["enabled"],
                repository.state["plan"],
            )

        return claim_current
    if operation == "feedback" and set(body) == {
        "entity_id",
        "task_id",
        "attempt_id",
        "event",
        "at",
        "cancelled",
    }:
        return partial(
            service.feedback,
            body["entity_id"],
            body["task_id"],
            body["attempt_id"],
            body["event"],
            instant(body["at"]),
            now,
            cancelled=body["cancelled"],
        )
    return None


async def _write_action(app, service, operation, body, apply):
    """Complete the durable write and record bounded learning activity when applicable."""
    from open_energy_planner.activities.api import EVENTS
    from open_energy_planner.activities.events import Outcome

    events = app.get(EVENTS)
    if not events or operation not in ("history", "hourly_history", "fine_history", "cycles"):
        return await complete_write(apply)

    entity_id = body["entity_id"]
    async with events.attempt(
        "appliances",
        "history",
        "Learn appliance cycles",
        context={
            "label": next(
                (
                    option["name"]
                    for option in service.consumers.options
                    if option["entity_id"] == entity_id
                ),
                "Appliance",
            )
        },
        failure="Cycle learning failed. Previously accepted profiles are retained.",
    ) as activity:
        progress_field = "fine_import" if operation == "fine_history" else "history_import"
        previous = service.state["devices"].get(entity_id, {}).get(progress_field, {})
        previous_checkpoint = _history_frontier(previous)
        cancellation = await complete_write(apply)
        accepted, usable = _learning_outcome(
            service, body, operation, progress_field, previous_checkpoint
        )
        await events.finish(
            activity,
            Outcome.COMPLETED if usable else Outcome.SKIPPED,
            "Appliance history was processed. Detected cycles and usable profiles are shown in Appliance settings."
            if usable
            else "Appliance history was processed with no usable paired measurements. Retained profiles remain available."
            if accepted
            else "This appliance history batch was already processed.",
            usable=usable,
        )
        return cancellation


def _history_frontier(progress):
    """Read both directions of the resumable scan's progress."""
    return tuple(progress.get(frontier) for frontier in ("cursor", "forward_cursor"))


def _learning_outcome(service, body, operation, progress_field, previous_checkpoint):
    """Separate replayed or empty history batches from usable learning evidence."""
    if operation == "cycles":
        return True, True
    settings = service.state["devices"][body["entity_id"]]
    if operation == "history":
        summary = settings[
            "hourly_history" if body.get("resolution", "5minute") == "hour" else "learning_history"
        ]
        return True, summary["usable_intervals"] > 0
    progress = settings[progress_field]
    accepted = previous_checkpoint != _history_frontier(progress)
    summary = progress["last_batch"]
    return accepted, accepted and summary["usable_intervals"] > 0


def _start_authority_is_current(repository, lock, view, plan, first_expiry):
    """Recheck mutable command authority after the response leaves the worker thread."""
    if lock.locked() or not repository.state["enabled"]:
        return False
    if repository.state["plan"] is not plan or repository.pending_invalidation:
        return False
    return view.same_inputs(lock.settings.appliances) and datetime.now(UTC) < min(
        first_expiry, instant(plan["valid_until"])
    )


def register(app, service, repository, live, lock, ev, thermal, *, mark_command_free):
    """Serialize readiness, measurements and execution events with plan admission."""

    async def state(request):
        """Project committed evidence and only still-current start requests."""
        view = lock.settings.appliances
        raw_entity = request.query.get("raw_entity")
        if raw_entity is not None:
            try:
                view.member(raw_entity)
            except ValueError:
                raise web.HTTPBadRequest(
                    text="Select this consumer in Appliances settings first"
                ) from None
        plan = (
            repository.state["plan"] if not lock.locked() and repository.state["enabled"] else None
        )

        def respond(authorized_plan):
            """Render without borrowing any mutable appliance or repository service."""
            document = view.document(
                datetime.now(UTC),
                authorized_plan is not None,
                authorized_plan,
                raw_entity=raw_entity,
            )
            first_expiry = min(
                (
                    instant(row["request"]["expires_at"])
                    for row in document["devices"]
                    if row["request"]
                ),
                default=None,
            )
            return web.json_response(document), first_expiry

        response, first_expiry = await asyncio.to_thread(respond, plan)
        if first_expiry is not None and not _start_authority_is_current(
            repository, lock, view, plan, first_expiry
        ):
            response, first_expiry = await asyncio.to_thread(respond, None)
        if first_expiry is None:
            mark_command_free(request)
        return response

    async def mutate(request):
        """Validate exact operation schemas and finish each durable mutation before refreshing planning."""
        try:
            operation = request.match_info["operation"]
            reader = (
                request.clone(client_max_size=MAX_RAW_BODY_BYTES)
                if operation == "raw_history"
                else request
            )
            body = await reader.json()
            if not isinstance(body, dict):
                raise TypeError("An appliance operation must be an object")
            async with lock:
                now = datetime.now(UTC)
                processing_started = monotonic()
                previous_revision = service.revision
                action = _configuration_action(operation, body, service, ev, thermal)
                if action is None:
                    action = _learning_action(operation, body, service, now, processing_started)
                if action is None:
                    action = _scan_action(operation, body, service, now)
                if action is None:
                    action = _task_action(operation, body, service, now, repository)
                if action is None:
                    raise ValueError("Unsupported appliance operation or fields")
                result = []

                def apply():
                    """Capture the durable operation result without retrying an ambiguous write."""
                    result.append(action())

                cancellation = await _write_action(app, service, operation, body, apply)
                if (
                    live
                    and service.revision != previous_revision
                    and operation not in ("claim", "deadline", "deadline_field")
                ):
                    await live.refresh_async(
                        force=operation in ("device", "profile_options", "manual_profile")
                    )
                if cancellation:
                    raise cancellation
                response = {
                    "result": result[0],
                    "appliances": service.document(
                        now, repository.state["enabled"], repository.state["plan"]
                    ),
                }
                return web.json_response(response)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise web.HTTPBadRequest(
                text=request_error_message(
                    error,
                    logger=logging.getLogger(__name__),
                    operation="Appliance request",
                    safe_messages=PUBLIC_VALIDATION_MESSAGES,
                    fallback="Appliance request could not be completed. Refresh appliance status before retrying.",
                )
            ) from None

    app.router.add_get("/v1/appliances", state)
    app.router.add_post("/v1/appliances/{operation}", mutate)
