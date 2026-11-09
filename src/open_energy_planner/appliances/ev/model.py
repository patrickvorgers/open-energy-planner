"""Solver-neutral vehicle charging, explicit on periods and default AC conversion losses."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isfinite

from open_energy_planner.core.units import (
    FULL_SOC_PERCENT,
    MINUTES_PER_HOUR,
    SECONDS_PER_HOUR,
    WATTS_PER_KW,
)
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES

MAX_EV_CAPACITY_KWH = 1000
MAX_EV_POWER_W = 100000
EV_ENERGY_TOLERANCE_KWH = 1e-7
MAX_IRREGULAR_CONTROL_WINDOWS = 2
TAIL_SECONDS_PRECISION = 6
EV_LOSS_PROFILE_VERSION = "ac-default-2026-v1"
# Rounded planning assumptions informed by ADAC's August 2026 AC measurements.
# These are not manufacturer guarantees. Interpolate delivered power, not percentages.
DELIVERED_POWER_POINTS = ((0, 0), (2300, 1840), (4100, 3485), (11000, 9900), (100000, 90000))


def number(value, lower, upper):
    """Reject booleans and nonfinite/out-of-range physical inputs at the domain boundary."""
    if type(value) not in (float, int) or not isfinite(value) or not lower <= value <= upper:
        raise ValueError("Invalid EV physical quantity")
    return float(value)


def delivered_power(power_w):
    """Return estimated battery-side watts from AC watts using the documented piecewise curve."""
    number(power_w, 0, MAX_EV_POWER_W)
    for (left, delivered_left), (right, delivered_right) in pairwise(DELIVERED_POWER_POINTS):
        if power_w <= right:
            return delivered_left + (power_w - left) * (delivered_right - delivered_left) / (
                right - left
            )
    raise ValueError("Unsupported EV charging power")


@dataclass(frozen=True)
class EVWork:
    """One identified connected vehicle. Energy is usable battery kWh, power is AC watts.

    The vehicle cannot discharge. The requested target is an upper bound on new
    useful energy, so negative prices cannot authorize unwanted overcharging.
    Service shortfall is optimized before the configured economic strategy.
    """

    session_id: str
    point_id: str
    vehicle_id: str
    ready_at: datetime
    deadline: datetime
    capacity_kwh: float
    initial_soc: float
    target_soc: float
    minimum_w: float
    maximum_w: float
    control: str

    def __post_init__(self):
        """Require immutable identities, an aware time window and physically meaningful charge limits."""
        for value in (self.session_id, self.point_id, self.vehicle_id):
            if not isinstance(value, str) or not value or len(value) > 64:
                raise ValueError("Invalid EV identity")
        if (
            self.ready_at.utcoffset() is None
            or self.deadline.utcoffset() is None
            or self.deadline <= self.ready_at
        ):
            raise ValueError("Invalid EV charging window")
        number(self.capacity_kwh, EV_ENERGY_TOLERANCE_KWH, MAX_EV_CAPACITY_KWH)
        number(self.initial_soc, 0, FULL_SOC_PERCENT)
        number(self.target_soc, 0, FULL_SOC_PERCENT)
        number(self.maximum_w, 1, MAX_EV_POWER_W)
        number(self.minimum_w, 0, self.maximum_w)
        if self.maximum_w != round(self.maximum_w) or self.minimum_w != round(self.minimum_w):
            raise ValueError("EV command limits require whole watts")
        if self.control not in ("on_off", "variable"):
            raise ValueError("Only controllable EV work enters the solver")

    @property
    def required_kwh(self):
        """Return additional useful battery energy, never an obligation to discharge an over-target car."""
        return max(0, self.target_soc - self.initial_soc) * self.capacity_kwh / FULL_SOC_PERCENT


@dataclass(frozen=True)
class EVWindow:
    """One command window, independently of price/bonus billing subdivisions."""

    key: str
    start: datetime
    end: datetime

    @property
    def duration_h(self):
        """Return elapsed duration, preserving DST and first/last partial command windows."""
        return (self.end - self.start).total_seconds() / SECONDS_PER_HOUR


def windows(work, intervals):
    """Use UTC-quarter EV commands, independent of coarser stationary-device command groups."""
    first = max(intervals[0].start, work.ready_at)
    last = min(intervals[-1].start + timedelta(hours=intervals[-1].duration_h), work.deadline)
    quantum = FINE_INTERVAL_MINUTES * SECONDS_PER_HOUR / MINUTES_PER_HOUR
    cursor = datetime.fromtimestamp(int(first.timestamp() // quantum) * quantum, UTC)
    result = []
    while cursor < last:
        end = cursor + timedelta(seconds=quantum)
        if end > first:
            result.append(EVWindow("ev:" + cursor.isoformat(), max(cursor, first), min(end, last)))
        cursor = end
    return tuple(result)


def tail_durations(work, intervals):
    """Allow one exact final maximum-power pulse rather than inventing below-minimum setpoints.

    Full fine/coarse windows contain integer fine-step quanta. At most the first
    and last clipped window are irregular. Their possible inclusion gives a
    finite remainder catalogue, including a short session that fits no full slot.
    Starts remain on the configured command grid. Only the final stop can be earlier.
    """
    rate = delivered_power(work.maximum_w) / WATTS_PER_KW
    quantum_h = FINE_INTERVAL_MINUTES / MINUTES_PER_HOUR
    quantum_kwh = rate * quantum_h
    irregular = [
        window.duration_h
        for window in windows(work, intervals)
        if abs(window.duration_h / quantum_h - round(window.duration_h / quantum_h))
        > EV_ENERGY_TOLERANCE_KWH
    ]
    if len(irregular) > MAX_IRREGULAR_CONTROL_WINDOWS:
        raise ValueError("Unsupported irregular EV control grid")
    durations = set()
    for mask in range(1 << len(irregular)):
        remainder = work.required_kwh - rate * sum(
            duration for index, duration in enumerate(irregular) if mask & (1 << index)
        )
        if remainder <= EV_ENERGY_TOLERANCE_KWH:
            continue
        remainder %= quantum_kwh
        if min(remainder, quantum_kwh - remainder) > EV_ENERGY_TOLERANCE_KWH:
            durations.add(round(remainder / rate * SECONDS_PER_HOUR, TAIL_SECONDS_PRECISION))
    return tuple(sorted(durations))


def tail_options(work, intervals):
    """Return explicit lower/upper-power stop choices on the existing command grid.

    Variable full-window setpoints remain available. A final partial window uses
    one of the physical power limits, keeping the timing contract finite and
    independently checkable. Coarse controls also need whole fine quanta before
    their residual stop, rather than discarding these feasible longer pulses.
    """
    powers = {work.maximum_w}
    if work.control == "variable" and work.minimum_w > 0:
        powers.add(work.minimum_w)
    maximum_seconds = max(
        (window.duration_h * SECONDS_PER_HOUR for window in windows(work, intervals)), default=0
    )
    quantum_seconds = FINE_INTERVAL_MINUTES * SECONDS_PER_HOUR / MINUTES_PER_HOUR
    options = set()
    for power in powers:
        adjusted = replace(work, minimum_w=min(work.minimum_w, power), maximum_w=power)
        remainders = {0, *tail_durations(adjusted, intervals)}
        for multiple in range(int(maximum_seconds // quantum_seconds) + 1):
            for remainder in remainders:
                seconds = round(multiple * quantum_seconds + remainder, TAIL_SECONDS_PRECISION)
                if 0 < seconds < maximum_seconds:
                    options.add((power, seconds))
    return tuple(sorted(options))


def refine(problem, works):
    """Split exact EV stop/arrival/departure edges without freeing other devices' command groups."""
    from open_energy_planner.installation.model import slice_interval

    grouped = tuple(
        replace(step, control_group=step.control_group or f"ev-control:{index}")
        for index, step in enumerate(problem.intervals)
    )
    edges = set()
    for work in works:
        if work.required_kwh <= EV_ENERGY_TOLERANCE_KWH:
            continue
        durations = {seconds for _power, seconds in tail_options(work, grouped)}
        for window in windows(work, grouped):
            edges.update((window.start, window.end))
            edges.update(
                window.start + timedelta(seconds=seconds)
                for seconds in durations
                if seconds < window.duration_h * SECONDS_PER_HOUR
            )
    intervals, parents = [], []
    for index, step in enumerate(grouped):
        end = step.start + timedelta(hours=step.duration_h)
        boundaries = sorted({step.start, end, *(edge for edge in edges if step.start < edge < end)})
        for left, right in pairwise(boundaries):
            intervals.append(slice_interval(step, left, right))
            parents.append(index)
    return replace(
        problem,
        intervals=tuple(intervals),
        ev_work=tuple(works),
        reserve_checkpoints=problem.reserve_checkpoints
        or tuple(step.start + timedelta(hours=step.duration_h) for step in problem.intervals),
        export_bonuses=tuple(
            replace(
                bonus,
                rates=tuple(bonus.rates[index] for index in parents),
                caps_kwh=tuple(bonus.caps_kwh[index] for index in parents),
                periods=tuple(bonus.periods[index] for index in parents),
                prevent_battery_export=tuple(
                    bonus.prevent_battery_export[index] for index in parents
                )
                if bonus.prevent_battery_export
                else (),
            )
            for bonus in problem.export_bonuses
        ),
    )


@dataclass(frozen=True)
class EVFlow:
    """One vehicle's actual planned AC intake and useful stored energy in a billing segment."""

    session_id: str
    ac_kwh: float
    stored_kwh: float
    power_w: float
