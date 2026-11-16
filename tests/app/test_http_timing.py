"""HTTP duration diagnostics expose execution spans without identities or request secrets."""

import asyncio
import re

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.runtime.diagnostics.http import phase, register


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
