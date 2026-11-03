"""Authenticated task transport. The application service owns lifecycle and physical execution stays in HA."""

from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.appliances.cycles import history as historical
from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.storage.writes import complete_write


def register(app, service, repository, live, lock):
    """Serialize readiness, measurements and execution events with plan admission."""

    async def state(_request):
        """Return durable task/profile evidence and only current, unclaimed start requests."""
        async with lock:
            return web.json_response(
                service.document(
                    datetime.now(UTC), repository.state["enabled"], repository.state["plan"]
                )
            )

    async def mutate(request):
        """Validate exact operation schemas and finish each durable mutation before refreshing planning."""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("An appliance operation must be an object")
            operation = request.match_info["operation"]
            async with lock:
                now = datetime.now(UTC)
                previous_revision = service.revision
                if operation == "device" and set(body) in (
                    {"entity_id", "shiftable"},
                    {"entity_id", "shiftable", "expected_version"},
                ):
                    function, arguments, keywords = (
                        service.configure,
                        (body["entity_id"], body["shiftable"]),
                        {"expected_version": body.get("expected_version")},
                    )
                elif operation == "manual_profile" and set(body) == {
                    "entity_id",
                    "duration_minutes",
                    "energy_kwh",
                    "expected_version",
                }:
                    function, arguments, keywords = (
                        service.set_manual_profile,
                        (
                            body["entity_id"],
                            body["duration_minutes"],
                            body["energy_kwh"],
                            body["expected_version"],
                        ),
                        {},
                    )
                elif operation == "history_scan" and set(body) == {
                    "entity_id",
                    "power_entity",
                    "phase",
                    "start",
                    "end",
                }:
                    function, arguments, keywords = (
                        historical.scan,
                        (
                            service,
                            body["entity_id"],
                            body["power_entity"],
                            body["phase"],
                            body["start"],
                            body["end"],
                        ),
                        {"now": now},
                    )
                elif operation == "hourly_history" and set(body) == {
                    "entity_id",
                    "power_entity",
                    "rows",
                    "synthetic",
                    "start",
                    "end",
                    "read_start",
                    "read_end",
                }:
                    function, arguments, keywords = (
                        historical.batch,
                        (
                            service,
                            body["entity_id"],
                            body["power_entity"],
                            body["rows"],
                            body["synthetic"],
                            body["start"],
                            body["end"],
                            body["read_start"],
                            body["read_end"],
                        ),
                        {"now": now},
                    )
                elif operation == "history" and set(body) == {
                    "entity_id",
                    "power_entity",
                    "rows",
                    "synthetic",
                }:
                    function, arguments, keywords = (
                        service.learn_history,
                        (
                            body["entity_id"],
                            body["power_entity"],
                            body["rows"],
                            body["synthetic"],
                            now,
                        ),
                        {},
                    )
                elif operation == "deadline" and set(body) == {"entity_id", "deadline"}:
                    function, arguments, keywords = (
                        service.set_deadline,
                        (body["entity_id"], instant(body["deadline"]), now),
                        {},
                    )
                elif operation == "deadline_field" and set(body) == {
                    "entity_id",
                    "field",
                    "value",
                    "time_zone",
                }:
                    function, arguments, keywords = (
                        service.set_deadline_field,
                        (body["entity_id"], body["field"], body["value"], body["time_zone"], now),
                        {},
                    )
                elif operation == "cycles" and set(body) == {
                    "entity_id",
                    "cycle_id",
                    "started_at",
                    "completed_at",
                    "points",
                    "synthetic",
                }:
                    if type(body["synthetic"]) is not bool or instant(body["completed_at"]) > now:
                        raise ValueError("Cycle evidence must describe a completed measured run")
                    function, arguments, keywords = (
                        service.record_cycle,
                        (
                            body["entity_id"],
                            body["cycle_id"],
                            instant(body["started_at"]),
                            instant(body["completed_at"]),
                            body["points"],
                        ),
                        {"synthetic": body["synthetic"]},
                    )
                elif operation == "tasks" and set(body) == {
                    "entity_id",
                    "request_id",
                    "ready_at",
                    "deadline",
                }:
                    function, arguments, keywords = (
                        service.submit,
                        (
                            body["entity_id"],
                            body["request_id"],
                            instant(body["ready_at"]),
                            instant(body["deadline"]),
                            now,
                        ),
                        {},
                    )
                elif operation == "cancel" and set(body) == {"entity_id", "task_id"}:
                    function, arguments, keywords = (
                        service.cancel_task,
                        (body["entity_id"], body["task_id"]),
                        {},
                    )
                elif operation == "claim" and set(body) == {"entity_id", "task_id", "attempt_id"}:

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

                    function, arguments, keywords = claim_current, (), {}
                elif operation == "feedback" and set(body) == {
                    "entity_id",
                    "task_id",
                    "attempt_id",
                    "event",
                    "at",
                    "cancelled",
                }:
                    function, arguments, keywords = (
                        service.feedback,
                        (
                            body["entity_id"],
                            body["task_id"],
                            body["attempt_id"],
                            body["event"],
                            instant(body["at"]),
                            now,
                        ),
                        {"cancelled": body["cancelled"]},
                    )
                else:
                    raise ValueError("Unsupported appliance operation or fields")
                result = []

                def apply():
                    """Capture the durable operation result without retrying an ambiguous write."""
                    result.append(function(*arguments, **keywords))

                cancellation = await complete_write(apply)
                if (
                    live
                    and service.revision != previous_revision
                    and operation not in ("claim", "deadline", "deadline_field")
                ):
                    live.refresh(force=True)
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
            raise web.HTTPBadRequest(text=str(error)) from None

    app.router.add_get("/v1/appliances", state)
    app.router.add_post("/v1/appliances/{operation}", mutate)
