"""Distinguish unavailable solar notification evidence from unrelated application work."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest

from open_energy_planner.solar.wire import configuration_document
from tests.app.forecast.support.http import configure_forecasts, running_forecasts
from tests.app.solar.support import SITE, plant, weather


@pytest.mark.parametrize("condition", ["no_pv", "current", "expired"])
def test_unrelated_application_lock_does_not_make_solar_notifications_unavailable(
    tmp_path, monkeypatch, condition
):
    """A held shared lock preserves confirmed no-PV, healthy and expired solar notification states."""
    app_module = import_module("open_energy_planner.app")
    original_register = app_module.register_solar
    registered = {}

    def register(app, runtime, live, lock, *arguments):
        """Capture the real composition's state and lock without substituting its notification provider."""
        registered.update(runtime=runtime, lock=lock)
        return original_register(app, runtime, live, lock, *arguments)

    monkeypatch.setattr(app_module, "register_solar", register)

    async def scenario():
        """Read the authenticated endpoint while normal application work owns the shared lock."""
        async with running_forecasts(tmp_path) as (session, url):
            definition = None if condition == "no_pv" else configuration_document(plant())
            await configure_forecasts(session, url, solar_definition=definition)
            runtime = registered["runtime"]
            if condition != "no_pv":
                now = datetime.now(UTC)
                runtime.set_weather(SITE, weather(now))
                assert runtime.install(runtime.build(runtime.job(now)), now)
                if condition == "expired":
                    runtime.forecast = replace(runtime.forecast, issued_at=now - timedelta(hours=1))
            async with registered["lock"]:
                async with asyncio.timeout(2):
                    async with session.get(url + "/v1/notifications") as response:
                        assert response.status == 200
                        document = await response.json()
                assert "solar" not in document["unavailable_sources"]
                solar = [item for item in document["notifications"] if item["owner"] == "solar"]
                if condition == "expired":
                    assert len(solar) == 1
                    assert solar[0]["title"] == "Solar forecast is unavailable"
                    assert solar[0]["stale"] is False
                else:
                    assert solar == []

    asyncio.run(scenario())
