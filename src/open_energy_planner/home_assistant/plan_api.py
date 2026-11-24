"""Expose the committed command identity and materialize its commands only on demand."""

import asyncio
import json
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.build_info import RELEASE
from open_energy_planner.home_assistant.observation_wire import configuration_document
from open_energy_planner.installation.commands import execution_document
from open_energy_planner.runtime.diagnostics.http import phase, timed_lock

NO_STORE = {"Cache-Control": "no-store"}


def command_mode(repository, live):
    """Capture the currently authorized committed plan and its measurement revision."""
    state = repository.committed_control_state()
    plan = state["plan"]
    revision = live.configuration.revision if live and live.configuration else "demo-1"
    if (
        plan is not None
        and state["enabled"]
        and plan["config_revision"] == revision
        and datetime.fromisoformat(plan["valid_until"]) > datetime.now(UTC)
    ):
        return state, revision, plan
    return state, revision, None


def register(app, repository, planner, live, lock, command_context):
    """Register the app-owned read side of Home Assistant's command protocol."""

    async def status(_request):
        """Return a small durable identity without a plan archive read or refresh."""
        state, revision, plan = command_mode(repository, live)
        return web.json_response(
            {
                "release": RELEASE,
                "instance_id": state["instance_id"],
                "enabled": state["enabled"],
                "config_revision": revision,
                "measurements_required": bool(live and live.configuration),
                "plan_id": plan["plan_id"] if plan is not None else None,
                "acknowledged_plan": state["acknowledged_plan"],
                "planning": {"status": planner.status, "reason": planner.reason},
            },
            headers=NO_STORE,
        )

    async def measurement_config(request):
        """Read the exact active measurement binding without visiting optional setup or history."""
        configuration = live.configuration if live else None
        if configuration is None or configuration.revision != request.query.get("revision"):
            raise web.HTTPConflict(
                text="The requested measurement configuration is no longer active"
            )
        return web.json_response(
            {
                "instance_id": repository.committed_control_state()["instance_id"],
                "measurement_config": configuration_document(configuration),
            },
            headers=NO_STORE,
        )

    async def plan(request):
        """Project one immutable command revision and reject a superseded read."""
        requested_id = request.query.get("plan_id")
        async with timed_lock(request, "plan_capture_wait", lock):
            state, _revision, current = command_mode(repository, live)
            if current is None or current["plan_id"] != requested_id:
                raise web.HTTPConflict(text="The requested plan is no longer active")
            context = command_context()
        with phase(request, "plan_projection"):

            def render():
                """Serialize immutable commands outside the app lock and event loop."""
                return json.dumps(
                    {
                        "instance_id": state["instance_id"],
                        "plan": execution_document(current),
                        **context,
                    }
                )

            document = await asyncio.to_thread(render)
        _state, _revision, latest = command_mode(repository, live)
        if latest is None or latest["plan_id"] != requested_id:
            raise web.HTTPConflict(text="The requested plan is no longer active")
        return web.Response(text=document, content_type="application/json", headers=NO_STORE)

    app.router.add_get("/v1/ha/plan-status", status)
    app.router.add_get("/v1/ha/plan", plan)
    app.router.add_get("/v1/ha/measurement-config", measurement_config)
