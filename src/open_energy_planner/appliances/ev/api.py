"""Authenticated EV configuration, live reports and helper-free virtual controls."""

import logging
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.runtime.diagnostics.logging import request_error_message
from open_energy_planner.storage.writes import complete_write

# Only these reviewed owned literals may cross the public error boundary.
PUBLIC_VALIDATION_MESSAGES = (
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


def register(app, service, catalogue, repository, live, lock):
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
            return web.json_response(
                {
                    **service.document(
                        datetime.now(UTC), timezone(), catalogue.value, repository.state["plan"]
                    ),
                    "catalogue": catalogue.choices.catalogue(
                        "ev", catalogue.value, datetime.now(UTC)
                    ),
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
                    function, arguments = (
                        service.configure,
                        (
                            body["configuration"],
                            catalogue.choices.catalogue("ev", catalogue.value, now),
                        ),
                    )
                elif operation == "observations":
                    function, arguments = service.receive, (body, now)
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
                    live.refresh()
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
    app.router.add_post("/v1/ev/{operation}", mutate)
