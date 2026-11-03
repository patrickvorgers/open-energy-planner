"""Explicit reproducible synthetic sources. No live HA credentials or physical devices."""

from datetime import UTC, datetime
from uuid import uuid4

from open_energy_planner.solar.calculation.model import SolarInverter

from .installation.model import BatteryState, InstallationProblem
from .optimization.domain import Battery, Interval
from .planning import InputSnapshot
from .planning_policy import planning_grid

SYNTHETIC_POWER_LIMIT_W = 4000
SYNTHETIC_BATTERY_CAPACITY_KWH = 1
SYNTHETIC_INVERTER_EFFICIENCY = 1


def capture(
    now: datetime | None = None, initial_kwh: float = 0, first_load_kwh: float = 0
) -> InputSnapshot:
    """Build a 48-hour snapshot with repeatable values on the shared clock-hour grid.

    The first buy/sell opportunities and optional initial load exercise real solver
    behaviour. The ideal 1 kWh battery and unity-efficiency AC converter have
    matching 4000 W battery-terminal, inverter AC and grid import/export limits.
    With initial_kwh=0, first_load_kwh=0 and a full first quarter, it imports
    1 kWh at 0.12 EUR/kWh and exports 1 kWh at 0.40 EUR/kWh in the second quarter.
    Zero wear and no terminal valuation give an independent cash cost of -0.28 EUR.
    Negative cash cost represents a net credit. Custom inputs can be infeasible
    within the fixture limits even when their individual quantities are valid.
    These explicit fixture inputs never substitute for missing live data.
    Snapshot and source-version UUIDs are fresh for every capture.

    Args:
        now: Timezone-aware planning instant. None uses the current UTC time.
        initial_kwh: Finite initial stored energy between 0 and the 1 kWh capacity.
        first_load_kwh: Finite nonnegative household energy in kWh for the first
            interval. All later household loads are zero.

    Returns:
        A frozen InputSnapshot containing the synthetic installation and fresh
        snapshot, measurement, forecast and price version identifiers. Solving
        and persistence remain the caller's responsibility.

    Raises:
        ValueError: The planning instant has no UTC offset, initial stored energy
            is outside its finite capacity bounds, or the first load is negative
            or nonfinite.
    """
    now = now or datetime.now(UTC)
    intervals = []
    for index, (start, hours) in enumerate(planning_grid(now, align=False)):
        intervals.append(
            Interval(
                start,
                hours,
                0.12 if index == 0 else 1,
                0.4 if index == 1 else 0,
                first_load_kwh if index == 0 else 0,
            )
        )
    return InputSnapshot(
        str(uuid4()),
        "demo-1",
        now,
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        InstallationProblem(
            tuple(intervals),
            (SolarInverter("battery", SYNTHETIC_POWER_LIMIT_W, SYNTHETIC_INVERTER_EFFICIENCY),),
            (
                BatteryState(
                    "battery",
                    "battery",
                    Battery(
                        SYNTHETIC_BATTERY_CAPACITY_KWH,
                        initial_kwh,
                        charge_limit_w=SYNTHETIC_POWER_LIMIT_W,
                        discharge_limit_w=SYNTHETIC_POWER_LIMIT_W,
                    ),
                ),
            ),
            (),
            SYNTHETIC_POWER_LIMIT_W,
            SYNTHETIC_POWER_LIMIT_W,
        ),
    )
