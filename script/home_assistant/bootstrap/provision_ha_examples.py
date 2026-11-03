"""Bind existing OEP blueprints only to development simulators through native HA APIs."""

import asyncio

from aiohttp import ClientResponseError, ClientWSTimeout

from script.provisioning.automations import automations
from script.provisioning.device_examples import (
    ExampleJournal,
    boiler_configuration,
    unchanged_ev_configuration,
)

ENTITY_WAIT_ATTEMPTS = 60
ENTITY_WAIT_SECONDS = 2
WEBSOCKET_TIMEOUT_SECONDS = 15
WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 10
PRE_DISPATCH_ERRORS = frozenset({"unauthorized", "invalid_format", "unknown_command"})


class CommandNotApplied(RuntimeError):
    """Identify a command not sent or rejected before dispatch, allowing a safe corrected retry."""


async def native_command(session, url, command):
    """Execute an authenticated native HA command without proxying unsupported HTTP paths."""
    dispatch_started = False
    try:
        async with session.ws_connect(
            url + "/api/websocket",
            timeout=ClientWSTimeout(
                ws_receive=WEBSOCKET_TIMEOUT_SECONDS, ws_close=WEBSOCKET_CLOSE_TIMEOUT_SECONDS
            ),
        ) as websocket:
            if (await websocket.receive_json())["type"] != "auth_required":
                raise CommandNotApplied("Unexpected Home Assistant WebSocket handshake")
            await websocket.send_json(
                {
                    "type": "auth",
                    "access_token": session.headers["Authorization"].removeprefix("Bearer "),
                }
            )
            if (await websocket.receive_json())["type"] != "auth_ok":
                raise CommandNotApplied("Home Assistant WebSocket authentication failed")
            dispatch_started = True
            await websocket.send_json({"id": 1, **command})
            response = await websocket.receive_json()
            if response.get("id") != 1:
                raise RuntimeError("Unexpected Home Assistant native response identity")
            if response.get("success") is not True:
                if (
                    response.get("success") is False
                    and response.get("error", {}).get("code") in PRE_DISPATCH_ERRORS
                ):
                    raise CommandNotApplied("Home Assistant rejected the command before dispatch")
                # Supervisor timeouts also become unknown_error, possibly after an applied write.
                raise RuntimeError("Home Assistant native command outcome is unconfirmed")
            return response["result"]
    except CommandNotApplied:
        raise
    except Exception as error:
        if not dispatch_started:
            raise CommandNotApplied("Home Assistant command was not sent") from error
        raise


async def entity_registry(session, url):
    """Read native registry identities without guessing generated entity names or editing storage."""
    return await native_command(session, url, {"type": "config/entity_registry/list"})


async def provision(session, url, request, journal_path, *, app_request):
    """Wait for normal virtual-device discovery and create missing automation instances once."""
    journal = ExampleJournal(journal_path)
    if "sidebar" not in journal.state["completed"]:
        command = {
            "type": "supervisor/api",
            "endpoint": "/addons/local_open_energy_planner/info",
            "method": "get",
        }
        information = await native_command(session, url, command)
        if (
            not information.get("ingress_panel")
            and "attempted:sidebar" not in journal.state["completed"]
        ):
            journal.complete("attempted:sidebar")
            try:
                await native_command(
                    session,
                    url,
                    {
                        **command,
                        "endpoint": "/addons/local_open_energy_planner/options",
                        "method": "post",
                        "data": {"ingress_panel": True},
                    },
                )
            except CommandNotApplied:
                journal.discard("attempted:sidebar")
                raise
            information = await native_command(session, url, command)
            if not information.get("ingress_panel"):
                raise RuntimeError("Open Energy Planner sidebar panel was not enabled")
        journal.complete("sidebar")
    expected = {
        "automation:" + source.removeprefix("sensor.oep_test_").removesuffix("_energy")
        for source in journal.state["appliances"]
    }
    if "owns:ev" in journal.state["completed"]:
        expected.update(("automation:wallbox-a", "automation:wallbox-b"))
    if "owns:thermal" in journal.state["completed"]:
        expected.add("automation:boiler")
    if expected <= set(journal.state["completed"]):
        return
    for attempt in range(ENTITY_WAIT_ATTEMPTS):
        registry = await entity_registry(session, url)
        states = await request("GET", "/api/states")
        completed = journal.state["completed"]
        pending = {}
        for source in journal.state["appliances"]:
            key = "automation:" + source.removeprefix("sensor.oep_test_").removesuffix("_energy")
            if key not in completed:
                pending[source] = key
        if pending:
            selected = {
                selected_consumer["entity_id"]
                for selected_consumer in (await app_request("GET", "/v1/consumers"))["selected"]
            }
            devices = {
                appliance_device["entity_id"]: appliance_device
                for appliance_device in (await app_request("GET", "/v1/appliances"))["devices"]
            }
            for source, key in pending.items():
                if source not in selected or not devices.get(source, {}).get("shiftable"):
                    journal.complete(key)
        if (
            "owns:ev" in completed
            and any(
                key not in completed for key in ("automation:wallbox-a", "automation:wallbox-b")
            )
            and not unchanged_ev_configuration(await app_request("GET", "/v1/ev"))
        ):
            journal.discard("owns:ev")
        if "owns:thermal" in completed and "automation:boiler" not in completed:
            current = await app_request("GET", "/v1/thermal")
            if current["configuration"] != boiler_configuration():
                journal.discard("owns:thermal")
        try:
            definitions = automations(registry, states, journal)
            break
        except LookupError:
            if attempt == ENTITY_WAIT_ATTEMPTS - 1:
                raise RuntimeError("Virtual example devices did not become available") from None
            await asyncio.sleep(ENTITY_WAIT_SECONDS)
    for key, body in definitions:
        automation_id = "oep_lab_" + key.removeprefix("automation:").replace("-", "_")
        path = "/api/config/automation/config/" + automation_id
        try:
            await request("GET", path)
        except ClientResponseError as error:
            if error.status != 404:
                raise
            if "attempted:" + key not in journal.state["completed"]:
                journal.complete("attempted:" + key)
                try:
                    await request("POST", path, json=body)
                except ClientResponseError as rejected:
                    if 400 <= rejected.status < 500:
                        journal.discard("attempted:" + key)
                    raise
        journal.complete(key)
