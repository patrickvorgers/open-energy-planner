"""Authenticated individual forecast choices. All measurement membership belongs to HA Energy."""

import asyncio
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.appliances.forecasting import parse_selection

from ..storage.writes import complete_write


def register(app, demand, lock, ev, thermal, *, mark_command_free):
    """Expose existing runtime status and serialize settings writes with forecast admission."""

    def view(request):
        """Reject unsupported response projections before either reading or changing consumer state."""
        selected = request.query.get("view")
        if selected not in (None, "settings"):
            raise web.HTTPBadRequest(text="Unknown consumer view")
        return selected

    async def state(request):
        """Project committed choices and forecasts independently of an in-flight save."""
        forecasts = view(request) != "settings"
        published = lock.settings
        now = datetime.now(UTC)

        def respond():
            """Encode only the owned settings read view."""
            return web.json_response(published.consumer_document(forecasts=forecasts, now=now))

        return await asyncio.to_thread(respond)

    async def read(request):
        """Consumer settings do not expose commands or wait for plan revocation persistence."""
        mark_command_free(request)
        return await state(request)

    async def save(request):
        """Validate selected sources against current Energy metadata, retaining historical versions."""
        view(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"selected", "expected_version"}:
                raise ValueError("Select sources and supply the displayed settings version")
            selected = parse_selection(body["selected"])
            async with lock:
                if body["expected_version"] != demand.consumers.version:
                    raise web.HTTPConflict(
                        text="Consumer configuration changed. Reload before saving"
                    )
                reserved = {
                    row["energy_entity"] for row in (*ev.state["points"], *thermal.state["stores"])
                }
                if not reserved <= {row["entity_id"] for row in selected}:
                    raise ValueError("Retain meters assigned to charge points and electric boilers")
                cancellation = await complete_write(
                    demand.consumers.save, selected, body["expected_version"]
                )
                demand.select_learned(False)
                demand.candidate = None
                demand.candidate_features = "consumers:" + demand.consumers.version
                demand.last_key = None
                if cancellation:
                    raise cancellation
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(
                text="Select disjoint consumers from Home Assistant Energy"
            ) from None
        return await state(request)

    app.router.add_get("/v1/consumers", read)
    app.router.add_put("/v1/consumers", save)
