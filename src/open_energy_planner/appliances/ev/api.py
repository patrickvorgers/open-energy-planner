"""Authenticated EV configuration, live reports and helper-free virtual controls."""

from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.storage.writes import complete_write


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
                    "catalogue": catalogue.value,
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
                        (body["configuration"], catalogue.value),
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
            raise web.HTTPBadRequest(text=str(error)) from None

    app.router.add_get("/v1/ev", state)
    app.router.add_post("/v1/ev/{operation}", mutate)
