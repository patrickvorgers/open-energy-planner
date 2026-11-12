"""Expose authenticated planning-objective settings independently of plan archives."""

from aiohttp import web

from .domain import Strategy


def register(app, store, live, lock, persist):
    """Expose an authenticated manual choice. Retain current commands until validated replacement."""

    def state():
        """Return saved intent and its configuration issue without reading historical plans."""
        return {"requested": store.current.value, "issue": store.issue}

    async def read(_request):
        """Read the saved preference consistently with configuration writes."""
        async with lock:
            return web.json_response(state())

    async def write(request):
        """Compare-and-set manual intent without resetting measurements, reference or history."""
        if live is None:
            raise web.HTTPConflict(text="Strategy settings require Home Assistant mode")
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"strategy", "expected_strategy"}:
                raise ValueError("Strategy and expected strategy required")
            candidate = Strategy(body["strategy"])
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text="Invalid planning strategy") from None
        async with lock:
            if body["expected_strategy"] != store.current:
                raise web.HTTPConflict(text="Strategy changed. Reload before saving")
            if candidate != store.current or store.issue:
                await persist(store.save, candidate, contract_input=False)
            return web.json_response(state())

    app.router.add_get("/v1/strategy", read)
    app.router.add_put("/v1/strategy", write)
