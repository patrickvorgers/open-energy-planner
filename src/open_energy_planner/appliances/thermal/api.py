"""Authenticated thermal settings and paired Home Assistant observations."""

from copy import deepcopy
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.storage.writes import complete_write


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
            raise web.HTTPBadRequest(text=str(error)) from None

    app.router.add_get("/v1/thermal", state)
    app.router.add_post("/v1/thermal/{operation}", mutate)
