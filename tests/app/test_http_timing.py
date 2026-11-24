"""HTTP duration diagnostics expose execution spans without identities or request secrets."""

import asyncio
import re

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from open_energy_planner.build_info import BUILD_INFO
from open_energy_planner.runtime.diagnostics.http import phase, register
from open_energy_planner.savings.runtime import SavingsRuntime


def test_timing_reports_work_and_error_duration_without_request_values():
    """A deliberate delay appears in fixed numeric metrics. Error responses never echo request data."""

    async def check():
        """Exercise prepared response headers for an ordinary response and a raised HTTP failure."""
        app = web.Application()
        register(app)

        async def handler(request):
            """Model a small awaited archive operation and an independently selected failure."""
            with phase(request, "archive"):
                await asyncio.sleep(0.010)
            if request.path == "/failed":
                raise web.HTTPServiceUnavailable(text="Unavailable")
            return web.json_response({"ready": True})

        app.router.add_get("/{path}", handler)
        async with TestClient(TestServer(app)) as client:
            for path, status in (("/ready", 200), ("/failed", 503)):
                response = await client.get(
                    path + "?source=PRIVATE-QUERY-SENTINEL",
                    headers={"Authorization": "Bearer PRIVATE-TOKEN-SENTINEL"},
                )
                assert response.status == status
                header = response.headers["Server-Timing"]
                assert re.fullmatch(r"app;dur=\d+\.\d{3}, archive;dur=\d+\.\d{3}", header)
                durations = [float(metric.split("=")[1]) for metric in header.split(", ")]
                assert durations[0] >= durations[1] >= 5
                assert "PRIVATE" not in header

    asyncio.run(check())


def test_ha_command_snapshot_attributes_waiting_settlement_to_its_own_phase(tmp_path, monkeypatch):
    """Return a real authenticated snapshot with numeric phase durations and no request values."""

    async def check():
        """Hold the settlement reader and release it from the event loop during the request."""
        from threading import Event

        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), Event()

        def settlement_request(*_arguments):
            """Represent an archive wait inside the request's settlement worker."""
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)

        monkeypatch.setattr(SavingsRuntime, "request", settlement_request)
        token = "PRIVATE-TEST-SNAPSHOT-TOKEN-1234567890"
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            request = asyncio.create_task(
                client.get(
                    "/v1/snapshot?source=PRIVATE-QUERY-SENTINEL",
                    headers={
                        "Authorization": "Bearer " + token,
                        "X-OEP-View": "commands",
                        "X-OEP-Release": BUILD_INFO["version"],
                        "X-OEP-Build": BUILD_INFO["build"] or "",
                    },
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.sleep(0.030)
            finally:
                release.set()
            response = await request
            assert response.status == 200
            body = await response.json()
            assert body["source"] == "home_assistant"
            assert body["settlement_request"] is None
            header = response.headers["Server-Timing"]
            assert "PRIVATE" not in header
            timings = {
                metric: float(duration)
                for metric, duration in (part.split(";dur=") for part in header.split(", "))
            }
            assert {
                "app",
                "snapshot_capture_wait",
                "snapshot_settlement",
                "snapshot_state_wait",
                "snapshot_appliance_tick",
                "snapshot_live_refresh",
                "snapshot_bonus_history",
                "snapshot_forecast_history",
                "snapshot_demand",
                "snapshot_document",
                "snapshot_json",
            } <= timings.keys()
            assert timings["app"] >= timings["snapshot_settlement"] >= 20
            assert all(value >= 0 for value in timings.values())

    asyncio.run(check())
