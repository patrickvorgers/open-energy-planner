"""Expose authenticated planning-objective settings and retained active-plan evidence."""

import asyncio
from datetime import UTC, datetime

from aiohttp import web

from .domain import Strategy


def register(app, store, repository, live, lock, persist):
    """Expose an authenticated manual choice. Retain current commands until validated replacement."""

    async def state():
        """Read archive evidence off-loop under the caller-held lock. Report unavailable/corrupt data as 503."""
        plan = repository.state["plan"]
        acknowledged = repository.state["acknowledged_plan"]
        try:
            accepted = (
                await asyncio.to_thread(repository.get_plan, acknowledged) if acknowledged else None
            )
            expiry = datetime.fromisoformat(accepted["valid_until"]) if accepted else None
            active = (
                accepted
                if (
                    plan
                    and accepted
                    and repository.state["enabled"]
                    and plan["config_revision"] == accepted["config_revision"]
                    and datetime.now(UTC) < expiry
                )
                else None
            )
            return {
                "requested": store.current.value,
                "published": Strategy(plan["strategy"]).value if plan else None,
                "active": Strategy(active["strategy"]).value if active else None,
                "issue": store.issue,
            }
        except (OSError, ValueError, KeyError, TypeError):
            raise web.HTTPServiceUnavailable(text="Plan archive unavailable or invalid") from None

    async def read(_request):
        """Read the persistent request and published plan metadata under the app lock."""
        async with lock:
            return web.json_response(await state())

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
            return web.json_response(await state())

    app.router.add_get("/v1/strategy", read)
    app.router.add_put("/v1/strategy", write)
