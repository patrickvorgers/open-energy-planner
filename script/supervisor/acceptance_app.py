"""Compose simulated weather only inside the disposable Supervisor acceptance app image."""

import os
from contextlib import asynccontextmanager


@asynccontextmanager
async def temperature_factory(history_start=None):
    """Accept the history callback while retaining the fixed offline weather fixture."""
    from acceptance_sources import TestTemperatures

    yield TestTemperatures()


def main():
    """Inject the existing weather port, then retain the original Supervisor bootstrap and runtime."""
    if os.environ.get("OEP_SUPERVISOR_SMOKE") != "1":
        raise RuntimeError("Simulated weather requires the disposable Supervisor acceptance image")
    from production_ha_app import main as production_main

    from open_energy_planner.demand import weather

    weather.weather_factory = temperature_factory
    production_main()


if __name__ == "__main__":
    main()
