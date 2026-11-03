"""Solver-neutral heat storage, declared heat demand and electrical heater contracts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

from open_energy_planner.core.units import SECONDS_PER_HOUR, SECONDS_PER_MINUTE
from open_energy_planner.optimization.domain import DEFAULT_SOLVER_TIME_LIMIT_SECONDS

MAX_THERMAL_STORES = 4
MAX_THERMAL_STORE_ID_LENGTH = 64
MAX_THERMAL_ENTITY_ID_LENGTH = 127
MAX_THERMAL_CLOCK_SKEW_SECONDS = 5
MAX_THERMAL_HOURLY_DRAW_KWH = 100
MAX_THERMAL_INTERVAL_DRAW_KWH = 100
THERMAL_HEAT_CAPACITY_BOUNDS_KWH_PER_C = (0.001, 12)
MIN_THERMAL_CONVERSION_EFFICIENCY = 0.01
THERMAL_CONFIGURATION_BOUNDS = {
    "volume_litres": (1, 10000),
    "minimum_c": (0, 120),
    "maximum_c": (1, 120),
    "ambient_c": (0, 60),
    "loss_w_per_c": (0, 1000),
    "heater_w": (1, 30000),
}
TEMPERATURE_MAX_AGE_SECONDS = 300
THERMAL_DISPATCH_LEAD_SECONDS = DEFAULT_SOLVER_TIME_LIMIT_SECONDS + SECONDS_PER_MINUTE
THERMAL_RUNNING_POWER_FRACTION = 0.5
WATER_KWH_PER_LITRE_C = 4.186 / 3600


def number(value, lower, upper):
    """Reject nonfinite numbers, booleans and unsupported physical settings."""
    if type(value) not in (int, float) or not isfinite(value) or not lower <= value <= upper:
        raise ValueError("Invalid thermal physical quantity")
    return float(value)


@dataclass(frozen=True)
class ThermalWork:
    """One lumped heat store. Temperature is Celsius, heat is kWh and power is watts.

    Heat demand is thermal energy extracted in each electrical billing segment.
    The state advances from state_at, or the horizon start when omitted.
    ready_at gates new heater commands, never passive loss or draw. Existing
    measured heating can continue without a repeated startup pause.
    Comfort and ending-stock deficits are visible
    service shortfalls, not permission to exceed the hard maximum temperature.
    """

    device_id: str
    ready_at: datetime
    initial_c: float
    minimum_c: float
    maximum_c: float
    ambient_c: float
    capacity_kwh_per_c: float
    loss_w_per_c: float
    heater_w: float
    efficiency: float
    demand_kwh: tuple[float, ...]
    terminal_c: float
    state_at: datetime | None = None
    initially_heating: bool = False

    def __post_init__(self):
        """Require bounded physical quantities and an immutable nonnegative demand forecast."""
        if (
            not isinstance(self.device_id, str)
            or not self.device_id
            or len(self.device_id) > MAX_THERMAL_STORE_ID_LENGTH
        ):
            raise ValueError("Invalid thermal store identity")
        if not isinstance(self.ready_at, datetime) or self.ready_at.utcoffset() is None:
            raise ValueError("Thermal dispatch needs an aware timestamp")
        if self.state_at is not None and (
            not isinstance(self.state_at, datetime)
            or self.state_at.utcoffset() is None
            or self.state_at > self.ready_at
        ):
            raise ValueError("Thermal state needs an aware initialization no later than dispatch")
        if type(self.initially_heating) is not bool:
            raise TypeError("Thermal heating evidence must be boolean")
        if self.initially_heating and (self.state_at is None or self.ready_at != self.state_at):
            raise ValueError(
                "Continuing heat requires a current state and immediate dispatch boundary"
            )
        number(self.maximum_c, *THERMAL_CONFIGURATION_BOUNDS["maximum_c"])
        number(self.initial_c, 0, self.maximum_c)
        number(self.minimum_c, 0, self.maximum_c)
        number(self.terminal_c, self.minimum_c, self.maximum_c)
        number(self.ambient_c, 0, min(THERMAL_CONFIGURATION_BOUNDS["ambient_c"][1], self.maximum_c))
        number(self.capacity_kwh_per_c, *THERMAL_HEAT_CAPACITY_BOUNDS_KWH_PER_C)
        number(self.loss_w_per_c, *THERMAL_CONFIGURATION_BOUNDS["loss_w_per_c"])
        number(self.heater_w, *THERMAL_CONFIGURATION_BOUNDS["heater_w"])
        number(self.efficiency, MIN_THERMAL_CONVERSION_EFFICIENCY, 1)
        if self.heater_w != round(self.heater_w):
            raise ValueError("Thermal heater ratings require whole watts")
        if not isinstance(self.demand_kwh, tuple):
            raise TypeError("Thermal demand must be immutable")
        for demand in self.demand_kwh:
            number(demand, 0, MAX_THERMAL_INTERVAL_DRAW_KWH)


@dataclass(frozen=True)
class ThermalFlow:
    """Detached heater AC intake, ending temperature and unmet comfort energy."""

    device_id: str
    ac_kwh: float
    end_c: float
    shortfall_kwh: float
    power_w: float
    unmet_draw_kwh: float = 0


def active_hours(work, step):
    """Advance physical state after its admission time, independently of heater-start readiness."""
    end = step.start + timedelta(hours=step.duration_h)
    return (
        max(0, (end - max(step.start, work.state_at or step.start)).total_seconds())
        / SECONDS_PER_HOUR
    )
