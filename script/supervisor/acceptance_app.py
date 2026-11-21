"""Compose simulated weather only inside the disposable Supervisor acceptance app image."""

import os
from contextlib import asynccontextmanager


@asynccontextmanager
async def temperature_factory(history_start=None):
    """Accept the history callback while retaining the fixed offline weather fixture."""
    from acceptance_sources import TestTemperatures

    yield TestTemperatures()


def main():
    """Use fixture temperatures and disable external live weather and satellite requests."""
    if os.environ.get("OEP_SUPERVISOR_SMOKE") != "1":
        raise RuntimeError("Simulated weather requires the disposable Supervisor acceptance image")
    from production_ha_app import main as production_main

    from open_energy_planner.weather import history, provider, satellite

    history.weather_factory = temperature_factory
    provider.live_weather_factory = None
    satellite.satellite_factory = None
    production_main()


if __name__ == "__main__":
    main()
