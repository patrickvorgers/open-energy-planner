"""Own isolated HA/app lifecycles, optional Recorder and peer gateways.

Temporary configuration and fixture solver/catalogue patches belong to each
chain. Remote solver scenarios share only the explicitly supplied app. Energy
HTTP uses a free loopback port, and teardown always stops HA and app resources.
"""

import asyncio
import os
import shutil
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

pytest.importorskip(
    "homeassistant", reason="Real HA integration suite runs in the pinned container"
)

from aiohttp import ClientSession, web
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.open_energy_planner.const import DOMAIN
from open_energy_planner.app import create_app

TOKEN = "local-test-only-0123456789abcdef"

COMPONENT = Path(__file__).resolve().parents[3] / "custom_components" / DOMAIN


@asynccontextmanager
async def running_chain(
    remote_url=None,
    *,
    live=False,
    recorder=False,
    fixture_devices=True,
    before_start=None,
    peer_gateway=False,
    compact_history=True,
):
    """Run isolated real HA and an authenticated test app with bounded lifecycle cleanup.

    Recorder-enabled chains retain all hourly fixture history and recent fine
    history. compact_history=False exercises unchanged full generator imports.
    This test-only policy never applies to the real Supervisor developer lab.
    """
    with TemporaryDirectory() as directory, ExitStack() as stack:
        if recorder and compact_history:
            from .fixture_history import compact_fixture_history

            stack.enter_context(compact_fixture_history())
        root = Path(directory)
        config = root / "ha"
        config.mkdir()
        shutil.copytree(COMPONENT, config / "custom_components" / DOMAIN)
        runner = gateway_runner = None
        if remote_url is None:
            future = None
            if live and fixture_devices:
                # Direct execution fixtures supply their own current device commands.
                # Keep their unavailable forecasts explicit.
                class UnavailableFixtureBasis:
                    """Keep direct execution fixtures unavailable without importing a solver into HA."""

                    def current(self, configuration, now, *, report=None):
                        """Require a deliberately injected fixture plan. Invent no future measurements."""
                        return

                future = UnavailableFixtureBasis()
            from open_energy_planner.installation.model import (
                BatteryFlow,
                InstallationFlow,
                InstallationPlan,
                InverterFlow,
            )
            from open_energy_planner.optimization.domain import Costs, SolveResult, Status

            class FixtureSolver:
                """Return independently specified feasible installation flows without native solver packages in HA."""

                async def solve(self, problem):
                    """Charge and sell one quarter kWh at one kW, yielding 0.03 minus 0.10 cash cost."""
                    unit = problem.batteries[0]
                    stored = unit.battery.initial_kwh
                    flows = []
                    for index, step in enumerate(problem.intervals):
                        charged = 0.25 if index == 0 else 0
                        discharged = 0.25 if index == 1 else 0
                        stored += charged - discharged
                        flows.append(
                            InstallationFlow(
                                charged,
                                discharged,
                                (BatteryFlow(unit.device_id, charged, discharged, stored),),
                                (InverterFlow(unit.inverter_id, charged, discharged, False),),
                                (),
                            )
                        )
                    costs = Costs(0.03, 0.10, 0, 0, -0.07, 0, -0.07)
                    return SolveResult(
                        Status.FEASIBLE,
                        InstallationPlan(tuple(flows), costs),
                        "fixture_candidate",
                        0,
                        0,
                        None,
                    )

            if live and fixture_devices:
                from unittest.mock import patch

                from open_energy_planner import app as app_module

                catalogue = app_module.installation_catalogue

                def fixture_catalogue(store, solar, snapshot=None):
                    """Declare the test battery only when no real installation settings exist."""
                    actual = catalogue(store, solar, snapshot)
                    return (
                        actual
                        if actual is not None
                        else [
                            {
                                "id": "battery",
                                "kind": "battery",
                                "name": "Battery",
                                "can_follow_grid": True,
                            }
                        ]
                    )

                stack.enter_context(
                    patch.object(app_module, "installation_catalogue", fixture_catalogue)
                )
            runner = web.AppRunner(
                create_app(
                    root / "app",
                    TOKEN,
                    solver=FixtureSolver() if not live else None,
                    source="home_assistant" if live else "synthetic",
                    basis_source=future,
                )
            )
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            url = f"http://127.0.0.1:{port}"
        else:
            url = remote_url
        hass = HomeAssistant(str(config))
        try:
            loader.async_setup(hass)
            hass.config_entries = ConfigEntries(hass, {})
            await bootstrap.async_load_base_functionality(hass)
            await async_setup_component(hass, "network", {})
            await async_setup_component(hass, "homeassistant", {})
            await async_setup_component(hass, "persistent_notification", {})
            if recorder:
                bootstrap.recorder.async_initialize_recorder(hass)
                assert await async_setup_component(
                    hass,
                    "recorder",
                    {
                        "recorder": {"auto_purge": False},
                    },
                )
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "user"},
            )
            if result["type"] == "form":
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"], {"url": url, "token": TOKEN}
                )
            assert result["type"] == "create_entry", result
            entry = result["result"]
            await hass.async_block_till_done()
            if peer_gateway:
                from custom_components.open_energy_planner.client import LocalClient
                from open_energy_planner.discovery import certificate, connection_app
                from open_energy_planner.pairing import PAIRING_KEY

                assert runner is not None
                pairing = runner.app[PAIRING_KEY]
                tls_context, pin = certificate(root / "app", pairing.instance_id)
                pairing.fingerprint = pin
                secret = "fixture-ev-peer-proof-0123456789"
                request_id = pairing.request(
                    {"peer_id": "ev-fixture-peer-123456", "name": "EV fixture", "secret": secret}
                )["request_id"]
                await pairing.approve(request_id)
                peer_token = pairing.claim({"request_id": request_id, "secret": secret})["token"]
                gateway_runner = web.AppRunner(connection_app(pairing, TOKEN, url, url + "/setup"))
                await gateway_runner.setup()
                gateway_site = web.TCPSite(gateway_runner, "127.0.0.1", 0, ssl_context=tls_context)
                await gateway_site.start()
                peer_url = f"https://127.0.0.1:{gateway_runner.addresses[0][1]}"
                entry.runtime_data.client = LocalClient(
                    entry.runtime_data.client.session, peer_url, peer_token, pin
                )

            if before_start:
                await before_start(hass, entry, url)
            await hass.async_start()
            await hass.async_block_till_done()
            yield hass, entry, url, root
        finally:
            await hass.async_stop(force=True)
            if gateway_runner:
                await gateway_runner.cleanup()
            if runner:
                await runner.cleanup()


def states(hass):
    """Return actual HA sensor/switch states for entity/projection assertions."""
    return {
        state_item.entity_id: state_item
        for state_item in hass.states.async_all()
        if state_item.entity_id.startswith(("sensor.", "switch."))
    }


def find(hass, suffix):
    """Resolve one expected integration entity suffix and fail when it is missing or ambiguous."""
    matches = [
        state_item for state_item in states(hass).values() if state_item.entity_id.endswith(suffix)
    ]
    assert len(matches) == 1, (suffix, list(states(hass)))
    return matches[0]


async def remote_replan(url, **inputs):
    """Ask the separately running glibc app for a new, independently checked result."""
    async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session:
        async with session.post(url + "/v1/replan", json=inputs) as response:
            assert response.status == 200
            requested = (await response.json())["planning"]["requested_snapshot_id"]
        for _ in range(300):
            async with session.get(url + "/v1/snapshot") as response:
                data = await response.json()
            if data["planning"]["requested_snapshot_id"] == requested and data["planning"][
                "status"
            ] in ("ready", "failed"):
                return data
            await asyncio.sleep(0.05)
    raise AssertionError("Remote planning did not complete")


async def remote_reset():
    """Enable and replan the separately running synthetic app so remote chain tests start consistently."""
    url = os.environ["OEP_TEST_APP_URL"]
    async with (
        ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
        session.put(
            url + "/v1/enabled",
            json={
                "enabled": True,
                "request_id": str(uuid4()),
            },
        ) as response,
    ):
        assert response.status == 200
    await remote_replan(url, initial_kwh=0)
    return url


async def setup_energy(hass):
    """Serialize test-worker Energy startup until the real HTTP listener owns its port.

    Workers share one user and a private temporary directory in a fresh container.
    Closing the file releases the lock after HTTP setup returns or raises. Only
    the private file remains until container cleanup.
    """
    import fcntl
    import socket
    from tempfile import gettempdir

    lock_path = Path(gettempdir()) / "oep-ha-energy-http.lock"
    with lock_path.open("a") as lock:
        await hass.async_add_executor_job(fcntl.flock, lock, fcntl.LOCK_EX)
        # HA rejects port zero. Keep other workers out until actual HTTP setup completes.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            http_port = listener.getsockname()[1]
        return await async_setup_component(
            hass, "energy", {"http": {"server_host": "127.0.0.1", "server_port": http_port}}
        )


async def configure_energy(hass, entry, sources):
    """Configure genuine Energy membership and upload it before testing app-owned property edits."""
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.components.energy.data import async_get_manager

    from custom_components.open_energy_planner.catalogue import energy_suggestions

    hass.auth = await auth_manager_from_config(hass, [], [])
    assert await setup_energy(hass)
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": sources, "device_consumption": []})
    entry.runtime_data.catalogue_sent = None
    await entry.runtime_data.sync()
    return (await energy_suggestions(hass))["terms"]


async def app_write(url, route, body):
    """Configure a real app endpoint. HA remains only a read-only data/device adapter."""
    async with (
        ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as session,
        session.put(url + "/v1/" + route, json=body) as response,
    ):
        assert response.status == 200, await response.text()
        return await response.json()
