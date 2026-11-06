"""Register demand configuration and normalized history admission under the shared app lock."""

import asyncio
import logging
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.core.units import BYTES_PER_KIB
from open_energy_planner.demand.wire import parse_configuration as parse_demand_configuration
from open_energy_planner.history.wire import parse_history
from open_energy_planner.runtime.diagnostics.logging import request_error_message

PUBLIC_VALIDATION_MESSAGES = (
    "Complete the Home Assistant Energy dashboard first",
    "Timezone is managed in the Home Assistant installation settings",
    "Device membership is managed in the Home Assistant Energy dashboard",
)

_LOGGER = logging.getLogger(__name__)


def register(
    app, demand, demand_store, live, setup_catalogue, lock, persist_then_install, snapshot
):
    """Register demand configuration and normalized history admission under the shared app lock."""

    async def demand_settings(_request):
        """Return cached model/quality/history status without fitting or fetching in this request."""
        async with lock:
            return web.json_response(demand.document(datetime.now(UTC)))

    async def build_demand(job):
        """Drain the worker's archive write before propagating HTTP/lifecycle cancellation."""
        worker = asyncio.create_task(asyncio.to_thread(demand.build, job))
        cancellation = None
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as error:
                if worker.cancelled():
                    raise
                cancellation = error
        if cancellation is not None:
            raise cancellation
        return result

    async def configure_demand(request):
        """Persist changed AC definitions. Identical saves preserve learned models and history."""
        if live is None:
            raise web.HTTPConflict(text="Demand configuration requires Home Assistant mode")
        try:
            body = await request.json()
            candidate = parse_demand_configuration(body)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid household demand configuration") from None
        async with lock:
            try:
                setup_catalogue.validate_membership("demand", candidate)
            except ValueError as error:
                raise web.HTTPBadRequest(
                    text=request_error_message(
                        error,
                        logger=_LOGGER,
                        operation="Demand configuration validation",
                        safe_messages=PUBLIC_VALIDATION_MESSAGES,
                        fallback="Cannot validate household demand settings. Reload the Home Assistant Energy configuration before saving again.",
                    )
                ) from None
            current = demand.configuration.version if demand.configuration else None
            if body.get("expected_version") != current:
                raise web.HTTPConflict(text="Demand configuration changed. Reload before saving")
            if candidate.version != current:
                await persist_then_install(
                    demand_store.save, candidate, demand.configure, contract_input=False
                )
                job = demand.job(datetime.now(UTC))
                result = await build_demand(job)
                demand.install(result, datetime.now(UTC))
                live.refresh(force=True)
        return await demand_settings(request)

    async def demand_history(request):
        """Accept one bounded normalized history response for the current demand request."""
        try:
            body = await request.clone(client_max_size=BYTES_PER_KIB * BYTES_PER_KIB).json()
            if not isinstance(body, dict):
                raise TypeError("Invalid demand history")
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid demand history") from None
        async with lock:
            pending = demand.request(datetime.now(UTC)) or demand.consumers.request(
                demand.configuration, datetime.now(UTC)
            )
            if pending is None or body.get("request_version") != pending["version"]:
                raise web.HTTPConflict(text="Demand history request changed")
            if set(body) == {"request_version", "error"} and body["error"] in (
                "recorder_unavailable",
                "history_unavailable",
                "unsupported_statistics",
            ):
                if pending == demand.request(datetime.now(UTC)):
                    demand.history_issues[pending["entity_id"]] = body["error"]
                else:
                    demand.consumers.receive(pending, error=body["error"])
            else:
                try:
                    if pending == demand.request(datetime.now(UTC)):
                        demand.receive(
                            body["request_version"], parse_history(body), datetime.now(UTC)
                        )
                    else:
                        demand.consumers.receive(pending, parse_history(body))
                except (ValueError, TypeError, KeyError, OverflowError, OSError):
                    raise web.HTTPBadRequest(text="Invalid household energy history") from None
        return await snapshot(request)

    app.router.add_get("/v1/demand", demand_settings)
    app.router.add_put("/v1/demand", configure_demand)
    app.router.add_post("/v1/demand/history", demand_history)
    return build_demand
