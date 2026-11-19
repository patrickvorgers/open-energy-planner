"""Synthetic metered solar runtimes shared by native learning and daily-training proofs."""

from dataclasses import replace
from datetime import timedelta

from open_energy_planner.history.model import EnergyHistory, EnergyHour
from tests.app.solar.support import SITE, plant


def runtime_case(tmp_path, *, store=None, hybrid=False):
    """Build real geometry for two roofs, with repeated morning shade measured only on the first roof."""
    from open_energy_planner.solar.calculation.predictor import PVWattsPredictor
    from open_energy_planner.solar.runtime import SolarRuntime
    from open_energy_planner.solar.storage import SolarStore
    from tests.app.solar.support import START as NOW
    from tests.app.solar.support import weather
    from tests.database.support.archive import make_store

    configuration = plant(count=2)
    configuration = replace(
        configuration,
        inverters=(replace(configuration.inverters[0], hybrid=hybrid), configuration.inverters[1]),
    )
    conditions = tuple(
        replace(row, received_at=NOW) for row in weather(NOW - timedelta(days=56), days=59)
    )
    physical = PVWattsPredictor().hourly(configuration, SITE, conditions)
    total, readings = 100.0, []
    for row in physical:
        if row.source_id == "pv0" and row.start < NOW:
            total += row.ac_kwh * (0.25 if row.start.hour < 11 else 0.9)
            readings.append(EnergyHour(row.start, total, total))
    store = store or make_store(tmp_path, SolarStore)
    store.save(configuration)
    runtime = SolarRuntime(store)
    runtime.set_weather(SITE, conditions)
    from tests.app.solar.support import frozen_runs

    runtime.set_forecast_runs(SITE, frozen_runs(conditions, SITE, NOW))
    runtime.histories["sensor.pv0"] = EnergyHistory("sensor.pv0", tuple(readings))
    return runtime, NOW
