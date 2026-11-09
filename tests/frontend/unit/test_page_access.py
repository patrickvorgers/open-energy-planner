"""Verify the public setup shell, authenticated setup/discovery APIs and read-only preview validation."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from open_energy_planner.app import create_app
from tests.ha_contract.support.metadata import setup_metadata


def test_setup_shell_is_public_but_all_settings_and_discovery_require_authentication(tmp_path):
    """The browser can load its static shell, but no configuration, sensor metadata or preview is readable/writable without the token."""

    async def scenario():
        """Use actual app middleware/routes rather than assuming static hosting bypass rules."""
        async with TestClient(
            TestServer(create_app(tmp_path, "setup-test-token-0123456789", source="home_assistant"))
        ) as client:
            for path in (
                "/",
                "/setup",
                "/setup/setup.js",
                "/setup/style.css",
                "/appliance-settings",
                "/appliance-settings/thermal",
                "/appliance-settings/ev",
                "/setup/save-bar.js",
                "/setup/thermal-settings.js",
                "/setup/ev-settings.js",
                "/contract",
                "/setup/contract.js",
            ):
                response = await client.get(path)
                assert response.status == 200
                assert "script-src 'self'" in response.headers["Content-Security-Policy"]
            for method, path in (
                ("GET", "/v1/setup"),
                ("GET", "/v1/contracts/budgets"),
                ("POST", "/v1/contracts/budgets"),
                ("GET", "/v1/strategy"),
                ("GET", "/v1/models"),
                ("PUT", "/v1/strategy"),
                ("POST", "/v1/ha/catalogue"),
                ("POST", "/v1/setup/preview"),
            ):
                response = await client.request(method, path, json={})
                assert response.status == 401
            headers = {"Authorization": "Bearer setup-test-token-0123456789"}
            body = {
                "installation": {
                    "grid_power_entity": "sensor.grid",
                    "import_limit_w": 5000,
                    "export_limit_w": 5000,
                },
                "solar": {"sources": [], "inverters": []},
                "demand": {
                    "annual_kwh": 3500,
                    "timezone": "Europe/Amsterdam",
                    "terms": [],
                    "exclusions": [],
                },
            }
            response = await client.post("/v1/setup/preview", headers=headers, json=body)
            assert response.status == 409
            catalogue = setup_metadata()
            await client.post("/v1/ha/catalogue", headers=headers, json=catalogue)
            body["demand"]["terms"] = catalogue["suggestions"]["terms"]
            response = await client.post("/v1/setup/preview", headers=headers, json=body)
            assert response.status == 200
            assert await response.json() == {"batteries": 0, "solar_sources": 0, "annual_kwh": 3500}
            body["demand"]["timezone"] = "America/New_York"
            response = await client.post("/v1/setup/preview", headers=headers, json=body)
            assert (
                response.status == 400
            )  # Preview cannot approve an independently chosen timezone.
            body["demand"]["timezone"] = "Europe/Amsterdam"
            body["installation"]["grid_power_entity"] = "sensor.missing"
            response = await client.post("/v1/setup/preview", headers=headers, json=body)
            assert (
                response.status == 400
            )  # Free-text API calls cannot bypass dropdown type validation.
            body["installation"]["grid_power_entity"] = "sensor.grid"
            body["demand"]["terms"] = []
            response = await client.post("/v1/setup/preview", headers=headers, json=body)
            assert response.status == 400  # Do not silently lose the Energy household balance.
            body["demand"]["terms"] = catalogue["suggestions"]["terms"]
            state = await (await client.get("/v1/setup", headers=headers)).json()
            assert state["installation"] is None  # A preview has no persistent side effects.
            body["installation"]["unexpected"] = "unsupported"
            response = await client.post("/v1/setup/preview", headers=headers, json=body)
            assert response.status == 400

    asyncio.run(scenario())
