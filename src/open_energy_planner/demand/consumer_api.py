"""Authenticated individual forecast choices. All measurement membership belongs to HA Energy."""

from aiohttp import web

from open_energy_planner.appliances.forecasting import COMPOSITE_MODEL_ID, parse_selection

from ..storage.writes import complete_write


def register(app, demand, lock, ev, thermal):
    """Expose existing runtime status and serialize settings writes with forecast admission."""

    def view(request):
        """Reject unsupported response projections before either reading or changing consumer state."""
        selected = request.query.get("view")
        if selected not in (None, "settings"):
            raise web.HTTPBadRequest(text="Unknown consumer view")
        return selected

    async def state(request):
        """Return source choices and estimated component forecasts without issuing commands."""
        async with lock:
            document = demand.consumers.document(forecasts=view(request) != "settings")
            document["charging_meters"] = sorted(
                point["energy_entity"] for point in ev.state["points"]
            )
            document["thermal_meters"] = sorted(
                store["energy_entity"] for store in thermal.state["stores"]
            )
            document["active_in_planning"] = bool(
                demand.forecast and demand.forecast.model == COMPOSITE_MODEL_ID
            )
            return web.json_response(document)

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

    app.router.add_get("/v1/consumers", state)
    app.router.add_put("/v1/consumers", save)
