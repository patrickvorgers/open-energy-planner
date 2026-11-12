"""Authenticated EV configuration, live reports and helper-free virtual controls."""

import logging
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.appliances.sources import (
    SOURCE_ALREADY_ASSIGNED,
    SOURCE_OWNERSHIP_UNAVAILABLE,
    validate_new_assignments,
)
from open_energy_planner.runtime.diagnostics.logging import request_error_message
from open_energy_planner.storage.writes import complete_write

# Only these reviewed owned literals may cross the public error boundary.
PUBLIC_VALIDATION_MESSAGES = (
    "Charging request belongs to a different vehicle",
    "Invalid charging request fields",
    "Charging request ID was already used",
    "Charging departure must be in the next thirty days",
    "Choose distinct repeat weekdays between zero and six",
    "Only a planned charging request can be edited",
    "Charging request changed. Refresh before saving",
    "Charging request no longer exists",
    "This vehicle already has a planned charging request",
    "Charging request changed. Refresh before cancelling",
    "Plan a charging request before changing its values",
    "No valid charging departure in the next two weeks",
    SOURCE_ALREADY_ASSIGNED,
    SOURCE_OWNERSHIP_UNAVAILABLE,
    "A charge point cannot also be a shiftable cycle appliance",
    "A fresh connected charging session is required",
    "A timezone-aware timestamp is required",
    "An EV operation must be an object",
    "Charge-point identities and Energy meters must be unique",
    "Charge-point limits require whole watts",
    "Connection must be explicitly true, false or unknown",
    "EV command limits require whole watts",
    "EV configuration changed. Refresh before reporting",
    "EV reading identity mismatch",
    "EV report is stale or from the future",
    "EV report membership mismatch",
    "EV reports must advance chronologically",
    "EV settings changed. Refresh before saving",
    "Each charge point requires its own power and connected source",
    "Invalid EV charging window",
    "Invalid EV configuration identity",
    "Invalid EV identity",
    "Invalid EV observation report",
    "Invalid EV physical quantity",
    "Invalid EV reading fields",
    "Invalid Home Assistant entity",
    "Invalid SoC sample timestamp attribute",
    "Invalid charge-point control mode",
    "Invalid charge-point fields",
    "Invalid vehicle fields",
    "Maximum charging power requires whole watts",
    "No valid daily departure in the next three local days",
    "One-off departure must be in the next thirty days",
    "Only controllable EV work enters the solver",
    "Select an AC charging power sensor",
    "Configure device power consumption in Home Assistant Energy before saving this charge point",
    "Use the power sensor configured for this device in Home Assistant Energy",
    "Select the charge point's vehicle-connected binary sensor",
    "Select the vehicle's battery percentage sensor",
    "Select the vehicles allowed at this charge point",
    "Specify vehicles and charge points",
    "This local departure time does not exist",
    "This vehicle is already assigned to another connected point",
    "Too many vehicles or charge points",
    "Unknown EV intent field",
    "Unknown SoC cannot have a claimed measurement timestamp",
    "Unknown vehicle",
    "Unsupported EV charging power",
    "Unsupported EV operation or fields",
    "Unsupported irregular EV control grid",
    "Use a daily local departure time with minute precision",
    "Use a local departure time",
    "Vehicle SoC provenance is invalid or its timestamp is in the future",
    "Vehicle identities and SoC sensors must be unique",
    "Vehicle is not allowed at this charge point",
    "Vehicles and charge points require distinct identities",
)


def register(app, service, catalogue, repository, live, lock, thermal):
    """Serialize EV writes with publication and reuse the installation authentication middleware."""

    def timezone():
        """Require HA's authoritative timezone rather than using the browser or host clock zone."""
        value = catalogue.value or {}
        if not value.get("timezone"):
            raise web.HTTPConflict(text="Connect Home Assistant before configuring EV charging")
        return value["timezone"]

    async def state(_request):
        """Expose current evidence and the authoritative selection catalogue."""
        async with lock:
            now = datetime.now(UTC)
            choices = catalogue.choices.catalogue("ev", catalogue.value, now)
            return web.json_response(
                {
                    **service.document(
                        now,
                        timezone(),
                        choices,
                        repository.state["plan"],
                    ),
                    "catalogue": choices,
                    "energy_consumers": list(service.consumers.options),
                }
            )

    async def mutate(request):
        """Finish durable operations before refreshing planning. Malformed writes change nothing."""
        try:
            body = await request.json()
            operation = request.match_info["operation"]
            if not isinstance(body, dict):
                raise TypeError("An EV operation must be an object")
            async with lock:
                now = datetime.now(UTC)
                zone = timezone()
                if operation == "configuration" and set(body) == {
                    "configuration",
                    "expected_version",
                }:
                    if body["expected_version"] != service.configuration_version:
                        raise ValueError("EV settings changed. Refresh before saving")
                    validate_new_assignments(
                        body["configuration"]["points"],
                        service.state["points"],
                        service.consumers.selected,
                        thermal.state["stores"],
                    )
                    function, arguments = (
                        service.configure,
                        (
                            body["configuration"],
                            catalogue.choices.catalogue("ev", catalogue.value, now),
                            now,
                        ),
                    )
                elif operation == "observations":
                    function, arguments = service.receive, (body, now, zone)
                elif operation == "requests":
                    function, arguments = service.save_request, (body, now, zone)
                elif operation == "cancel" and set(body) == {
                    "request_id",
                    "expected_version",
                    "vehicle_id",
                }:
                    function, arguments = (
                        service.cancel_request,
                        (body["request_id"], body["expected_version"], now, body["vehicle_id"]),
                    )
                elif operation == "assign" and set(body) == {
                    "point_id",
                    "vehicle_id",
                    "session_id",
                }:
                    function, arguments = (
                        service.assign,
                        (body["point_id"], body["vehicle_id"], body["session_id"], now),
                    )
                elif operation == "intent" and set(body) == {"vehicle_id", "field", "value"}:
                    function, arguments = (
                        service.intent,
                        (body["vehicle_id"], body["field"], body["value"], now, zone),
                    )
                else:
                    raise ValueError("Unsupported EV operation or fields")
                cancellation = await complete_write(function, *arguments)
                if live:
                    await live.refresh_async()
                if cancellation:
                    raise cancellation
                return web.json_response(
                    service.document(now, zone, catalogue.value, repository.state["plan"])
                )
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise web.HTTPBadRequest(
                text=request_error_message(
                    error,
                    logger=logging.getLogger(__name__),
                    operation="EV request",
                    safe_messages=PUBLIC_VALIDATION_MESSAGES,
                    fallback="EV request could not be completed. Refresh vehicle and charge-point status before retrying.",
                )
            ) from None

    app.router.add_get("/v1/ev", state)

    async def history(request):
        """Read retained request history without changing lifecycle state or planning."""
        async with lock:
            try:
                return web.json_response(
                    service.request_history(catalogue.value, request.query.get("cursor"))
                )
            except ValueError:
                raise web.HTTPBadRequest(text="Unknown charging history cursor") from None

    app.router.add_get("/v1/ev/requests", history)
    app.router.add_post("/v1/ev/{operation}", mutate)
