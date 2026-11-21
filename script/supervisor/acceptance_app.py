"""Compose simulated weather only inside the disposable Supervisor acceptance app image."""

import os
from contextlib import asynccontextmanager


@asynccontextmanager
async def temperature_factory(documents=None, history_start=None):
    """Accept the production history dependencies and supply fixed offline temperatures."""
    from acceptance_sources import TestTemperatures

    yield TestTemperatures()


@asynccontextmanager
async def live_weather_factory(store=None, history_start=None):
    """Supply the existing simulated irradiance and temperature through the live weather port."""
    from acceptance_sources import TestWeather

    yield TestWeather()


def main():
    """Use fixture weather for both inputs and disable external satellite requests."""
    if os.environ.get("OEP_SUPERVISOR_SMOKE") != "1":
        raise RuntimeError("Simulated weather requires the disposable Supervisor acceptance image")
    from production_ha_app import main as production_main

    from open_energy_planner.weather import history, provider, satellite

    history.weather_factory = temperature_factory
    provider.live_weather_factory = live_weather_factory
    satellite.satellite_factory = None
    production_main()


if __name__ == "__main__":
    main()
