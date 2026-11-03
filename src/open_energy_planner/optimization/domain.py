"""Solver-neutral interval, battery, strategy and result contracts for current installation planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from open_energy_planner.installation.model import InstallationPlan

DEFAULT_POWER_LIMIT_W = 4000
DEFAULT_SOLVER_TIME_LIMIT_SECONDS = 120
DEFAULT_REPLAY_TIME_LIMIT_SECONDS = 2


class Strategy(StrEnum):
    """Choose the persistent planning objective without changing device physics."""

    COST = "minimize cost"
    CONSUMPTION = "minimize consumption"


def nonnegative(name: str, value: float) -> None:
    """Reject non-finite or negative physical quantities and monetary bounds."""
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class Interval:
    """Represent one forecast interval: duration in hours, energy in kWh, prices per kWh."""

    start: datetime
    duration_h: float
    buy_price: float
    sell_price: float
    load_kwh: float = 0
    pv_kwh: float = 0
    control_group: str | None = None
    pv_sources: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        """Require aware time, positive duration and finite prices. Allow negative prices."""
        if self.start.utcoffset() is None:
            raise ValueError("Intervals require timezone-aware timestamps")
        if not isfinite(self.duration_h) or self.duration_h <= 0:
            raise ValueError("Interval duration must be finite and positive")
        if not all(isfinite(price) for price in (self.buy_price, self.sell_price)):
            raise ValueError("Prices must be finite. Negative prices are permitted")
        nonnegative("load", self.load_kwh)
        nonnegative("PV", self.pv_kwh)
        if not isinstance(self.pv_sources, tuple):
            raise TypeError("Per-source PV energy must be immutable")
        for identity, energy in self.pv_sources:
            if not isinstance(identity, str) or not identity:
                raise ValueError("Invalid PV source identity")
            nonnegative("PV source", energy)
        if self.control_group is not None and (
            not isinstance(self.control_group, str) or not self.control_group
        ):
            raise ValueError("Invalid control group")


@dataclass(frozen=True, slots=True)
class Battery:
    """Describe internal energy bounds and terminal power limits for one battery.

    Efficiencies convert terminal charge/discharge to internal energy. The cycle fee
    is the configured cost per full equivalent discharge of capacity.
    """

    capacity_kwh: float
    initial_kwh: float
    minimum_kwh: float = 0
    charge_limit_w: float = DEFAULT_POWER_LIMIT_W
    discharge_limit_w: float = DEFAULT_POWER_LIMIT_W
    charge_efficiency: float = 1
    discharge_efficiency: float = 1
    cycle_fee: float = 0
    recover_reserve: bool = False

    def __post_init__(self) -> None:
        """Validate energy bounds, nonnegative ratings/fees and efficiencies in (0, 1]."""
        for name in (
            "capacity_kwh",
            "initial_kwh",
            "minimum_kwh",
            "charge_limit_w",
            "discharge_limit_w",
            "cycle_fee",
        ):
            nonnegative(name, getattr(self, name))
        if self.capacity_kwh <= 0 or not (
            0 <= self.minimum_kwh <= self.capacity_kwh
            and 0 <= self.initial_kwh <= self.capacity_kwh
        ):
            raise ValueError("Invalid battery energy bounds")
        if type(self.recover_reserve) is not bool:
            raise TypeError("Reserve recovery must be boolean")
        for efficiency in (self.charge_efficiency, self.discharge_efficiency):
            if not isfinite(efficiency) or not 0 < efficiency <= 1:
                raise ValueError("Efficiency must lie in (0, 1]")


@dataclass(frozen=True, slots=True)
class Costs:
    """Separate currency-valued grid cash flow, wear and terminal battery value.

    Cash cost is imports minus exports and bonuses plus wear. The minimized
    objective is cash cost minus terminal value. Income fields are not net costs.
    """

    import_cost: float
    export_income: float
    bonus_income: float
    wear_cost: float
    cash_cost: float
    terminal_value: float
    objective: float


class Status(StrEnum):
    """Expose solver outcome without leaking native HiGHS status types."""

    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    UNBOUNDED = "unbounded"
    NO_CANDIDATE = "no_candidate"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SolveSample:
    """Observe a finite incumbent in the final financial search.

    Elapsed seconds count from the worker solve start. Objective and lower bound
    use contract currency, including terminal inventory valuation. A missing
    finite native bound stays None. An incumbent may be retained from an earlier
    service stage, now observed under the final financial objective.
    """

    elapsed_seconds: float
    objective: float
    best_bound: float | None


@dataclass(frozen=True, slots=True)
class SolveResult:
    """Return an optional candidate, termination reason, timings and objective bound.

    A feasible candidate is not necessarily optimal. Build/solve times are seconds.
    The best bound uses the same currency-valued objective as the candidate.
    Optimal means within the configured native tolerances, not an exact proof
    of equality between the financial candidate and its best bound.
    consumption_optimum_kwh is the proved grid-side corrected-import optimum,
    or None when no consumption optimum is reported. It is not a monetary bound.

    Progress and financial_started_seconds use seconds from the worker solve
    start. first_candidate_seconds is None unless a callback observed a finite
    incumbent in the financial stage. It does not establish when that incumbent
    was originally discovered or independently validated. time_limit_seconds is
    the shared preparation/service/search budget, not a per-stage allocation.
    started_at is the UTC solve-start instant. CalculationHistory adds the
    coordinator-to-worker start offset to progress and first-candidate times.
    """

    status: Status
    plan: InstallationPlan | None
    stop_reason: str
    build_seconds: float
    solve_seconds: float
    best_bound: float | None
    consumption_optimum_kwh: float | None = None
    progress: tuple[SolveSample, ...] = ()
    financial_started_seconds: float | None = None
    first_candidate_seconds: float | None = None
    time_limit_seconds: float | None = None
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SolveOptions:
    """Choose a strategy and bound model construction plus every native priority/financial stage by seconds."""

    time_limit_s: float = DEFAULT_SOLVER_TIME_LIMIT_SECONDS
    strategy: Strategy = Strategy.COST

    def __post_init__(self) -> None:
        """Require positive finite seconds and coerce strategy through Strategy. Unknown values raise ValueError."""
        if not isfinite(self.time_limit_s) or self.time_limit_s <= 0:
            raise ValueError("Time limit must be finite and positive")
        object.__setattr__(self, "strategy", Strategy(self.strategy))
