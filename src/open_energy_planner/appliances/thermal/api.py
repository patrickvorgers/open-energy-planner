"""Authenticated thermal settings and paired Home Assistant observations."""

import logging
from copy import deepcopy
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.runtime.diagnostics.logging import request_error_message
from open_energy_planner.storage.writes import complete_write

# Only these reviewed owned literals may cross the public error boundary.
PUBLIC_VALIDATION_MESSAGES = (
    "A thermal meter cannot also control an EV or appliance cycle",
    "A thermal operation must be an object",
    "Continuing heat requires a current state and immediate dispatch boundary",
    "Declare expected hot-water heat demand before enabling",
    "Heater power requires whole watts",
    "Invalid Home Assistant entity",
    "Invalid thermal identity",
    "Invalid thermal observations",
    "Invalid thermal physical quantity",
    "Invalid thermal reading identity",
    "Invalid thermal readings",
    "Invalid thermal settings",
    "Invalid thermal store identity",
    "Select a Home Assistant entity",
    "Select a temperature sensor or water heater",
    "Select the heater's electrical power sensor",
    "Specify 24 hourly thermal draw rates",
    "Specify thermal stores",
    "Thermal capture went backwards",
    "Thermal configuration unreadable. Restore the saved database before editing",
    "Thermal demand must be immutable",
    "Thermal devices and measurement bindings must be unique",
    "Thermal dispatch needs an aware timestamp",
    "Thermal enabled must be boolean",
    "Thermal heater ratings require whole watts",
    "Thermal heating evidence must be boolean",
    "Thermal observation expired",
    "Thermal report expired",
    "Thermal settings changed. Refresh before saving",
    "Thermal settings changed. Refresh observations",
    "Thermal state needs an aware initialization no later than dispatch",
    "Thermal temperatures exceed the supported planning maximum",
    "Thermal timestamps require a timezone",
    "Too many thermal stores",
    "Unknown thermal control",
    "Unsupported thermal operation or fields",
)


def register(app, service, catalogue, repository, live, lock):
    """Reuse app authorization, publication locking and cancellation-safe durable writes."""

    def document():
        """Expose original planned temperatures alongside current physical reports."""
        return service.document(datetime.now(UTC), catalogue.value, repository.state["plan"])

    async def state(_request):
        """Return bounded source selectors without requiring a browser to know entity IDs."""
        async with lock:
            return web.json_response(
                {
                    **document(),
                    "catalogue": catalogue.value,
                    "energy_consumers": list(service.consumers.options),
                }
            )

    async def mutate(request):
        """Reject partial or stale writes before they can change live planning intent."""
        try:
            body = await request.json()
            operation = request.match_info["operation"]
            if not isinstance(body, dict):
                raise TypeError("A thermal operation must be an object")
            async with lock:
                if operation == "configuration" and set(body) == {
                    "configuration",
                    "expected_version",
                }:
                    if body["expected_version"] != service.configuration_version:
                        raise ValueError("Thermal settings changed. Refresh before saving")
                    function, arguments = (
                        service.configure,
                        (body["configuration"], catalogue.value),
                    )
                elif operation == "observations":
                    function, arguments = service.receive, (body, datetime.now(UTC))
                elif operation == "intent" and set(body) == {"device_id", "field", "value"}:
                    configuration = deepcopy(service.state)
                    row = next(
                        (row for row in configuration["stores"] if row["id"] == body["device_id"]),
                        None,
                    )
                    if row is None or body["field"] not in ("enabled", "minimum_c"):
                        raise ValueError("Unknown thermal control")
                    row[body["field"]] = body["value"]
                    function, arguments = service.configure, (configuration, catalogue.value)
                else:
                    raise ValueError("Unsupported thermal operation or fields")
                cancelled = await complete_write(function, *arguments)
                if live:
                    live.refresh()
                if cancelled:
                    raise cancelled
                return web.json_response(document())
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise web.HTTPBadRequest(
                text=request_error_message(
                    error,
                    logger=logging.getLogger(__name__),
                    operation="Thermal request",
                    safe_messages=PUBLIC_VALIDATION_MESSAGES,
                    fallback="Thermal request could not be completed. Refresh thermal device status before retrying.",
                )
            ) from None

    app.router.add_get("/v1/thermal", state)
    app.router.add_post("/v1/thermal/{operation}", mutate)
