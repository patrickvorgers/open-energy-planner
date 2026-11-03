"""Immutable appliance work and measured profiles. Times are aware and powers are AC watts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from math import isfinite

from open_energy_planner.core.units import SECONDS_PER_HOUR, SECONDS_PER_MINUTE, WATTS_PER_KW
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES

PROFILE_STEP_SECONDS = FINE_INTERVAL_MINUTES * SECONDS_PER_MINUTE
MAX_CYCLE_HOURS = 12
MAX_CYCLE_POWER_W = 20000
MAX_ENERGY_COUNTER_KWH = 1e12
MAX_PROFILE_STEPS = int(MAX_CYCLE_HOURS * SECONDS_PER_HOUR / PROFILE_STEP_SECONDS)
MAX_APPLIANCES = 16
CONFIRMATION_SECONDS = 120
DURATION_ALLOWANCE_SECONDS = 300
TIME_TOLERANCE_SECONDS = 1
ENERGY_TOLERANCE_KWH = 1e-7


def instant(value):
    """Parse an aware ISO timestamp. Reject naive dates at the transport boundary."""
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return result


@dataclass(frozen=True)
class CycleProfile:
    """One learned average or manual estimated cycle, in quarter-hour mean AC watts."""

    power_w: tuple[float, ...]
    samples: int = 1
    evidence: str = "measured"

    def __post_init__(self):
        """Require finite measured nonnegative phases, including legitimate zero-power pauses."""
        if (
            not isinstance(self.power_w, tuple)
            or not 1 <= len(self.power_w) <= MAX_PROFILE_STEPS
            or type(self.samples) is not int
            or self.samples < 1
            or self.evidence not in ("measured", "synthetic", "manual", "hourly_estimate")
            or any(
                type(power) not in (int, float)
                or not isfinite(power)
                or not 0 <= power <= MAX_CYCLE_POWER_W
                for power in self.power_w
            )
            or sum(self.power_w) <= 0
        ):
            raise ValueError("A complete measured cycle profile is required")

    @property
    def duration_seconds(self):
        """Return the conservative complete profile duration in seconds."""
        return len(self.power_w) * PROFILE_STEP_SECONDS

    @property
    def energy_kwh(self):
        """Return the whole-cycle AC energy, including partial-power phases."""
        return sum(self.power_w) * PROFILE_STEP_SECONDS / SECONDS_PER_HOUR / WATTS_PER_KW

    def energy(self, started_at, left, right):
        """Integrate phase overlaps exactly, including mixed planning intervals and remaining work."""
        energy = 0.0
        for index, power in enumerate(self.power_w):
            start = started_at + timedelta(seconds=index * PROFILE_STEP_SECONDS)
            end = start + timedelta(seconds=PROFILE_STEP_SECONDS)
            overlap = max(0, (min(end, right) - max(start, left)).total_seconds())
            energy += power * overlap / SECONDS_PER_HOUR / WATTS_PER_KW
        return energy


@dataclass(frozen=True)
class ApplianceWork:
    """An acknowledged task. A fixed start denotes already requested or running work."""

    task_id: str
    entity_id: str
    ready_at: datetime
    deadline: datetime
    profile: CycleProfile
    fixed_start: datetime | None = None

    def __post_init__(self):
        """Reject ambiguous task identity, time windows and mutable profile values."""
        from open_energy_planner.demand.calculation.model import EnergyTerm

        EnergyTerm(self.entity_id, 1)
        if not isinstance(self.task_id, str) or not self.task_id or len(self.task_id) > 64:
            raise ValueError("Invalid appliance task identity")
        if not isinstance(self.profile, CycleProfile):
            raise TypeError("An immutable cycle profile is required")
        if (
            any(
                value.utcoffset() is None
                for value in (
                    self.ready_at,
                    self.deadline,
                    *(() if self.fixed_start is None else (self.fixed_start,)),
                )
            )
            or self.deadline <= self.ready_at
        ):
            raise ValueError("Invalid appliance time window")


def candidates(work, intervals):
    """List absolute quarter-aligned feasible starts, or the one committed actual start."""
    if work.fixed_start is not None:
        return (work.fixed_start,)
    first = max(work.ready_at, intervals[0].start)
    stamp = (int(first.timestamp()) // PROFILE_STEP_SECONDS + 1) * PROFILE_STEP_SECONDS
    # A round finishing exactly on the boundary can still choose that boundary.
    if first.timestamp() == stamp - PROFILE_STEP_SECONDS:
        stamp -= PROFILE_STEP_SECONDS
    start = datetime.fromtimestamp(stamp, first.tzinfo)
    end = min(
        work.deadline - timedelta(seconds=DURATION_ALLOWANCE_SECONDS),
        intervals[-1].start + timedelta(hours=intervals[-1].duration_h),
    )
    result = []
    while start + timedelta(seconds=work.profile.duration_seconds) <= end:
        result.append(start)
        start += timedelta(seconds=PROFILE_STEP_SECONDS)
    return tuple(result)


def average_profiles(previous, learned, *, previous_weight=None):
    """Average aligned cycles with optional retained weight, preserving count and longest duration."""
    if previous is None:
        return learned
    weight = previous.samples if previous_weight is None else previous_weight
    if type(weight) not in (int, float) or not isfinite(weight) or weight < 0:
        raise ValueError("Invalid retained cycle weight")
    powers = tuple(
        (
            (previous.power_w[index] if index < len(previous.power_w) else 0) * weight
            + (learned.power_w[index] if index < len(learned.power_w) else 0)
        )
        / (weight + 1)
        for index in range(max(len(previous.power_w), len(learned.power_w)))
    )
    return CycleProfile(
        powers,
        previous.samples + 1,
        "hourly_estimate"
        if "hourly_estimate" in (previous.evidence, learned.evidence)
        else "synthetic"
        if "synthetic" in (previous.evidence, learned.evidence)
        else "measured",
    )


def learn_cycle(points, started_at, completed_at, *, synthetic=False):
    """Build a completed cycle from fine cumulative kWh observations and known boundaries.

    Linear interpolation is limited to adjacent measurements no farther apart
    than one profile phase. Hourly totals cannot invent quarter-hour phases.
    """
    duration = (completed_at - started_at).total_seconds()
    if not PROFILE_STEP_SECONDS <= duration <= MAX_CYCLE_HOURS * SECONDS_PER_HOUR:
        raise ValueError("Cycle duration needs at least one quarter and at most twelve hours")
    if len(points) < 2:
        raise ValueError("Fine cumulative energy history is required")
    samples = tuple((instant(row["at"]), row["kwh"]) for row in points)
    if any(
        type(value) not in (int, float)
        or not isfinite(value)
        or abs(value) > MAX_ENERGY_COUNTER_KWH
        for _, value in samples
    ) or any(
        not 0 < (right[0] - left[0]).total_seconds() <= PROFILE_STEP_SECONDS or right[1] < left[1]
        for left, right in pairwise(samples)
    ):
        raise ValueError("Cycle history has gaps, resets or coarse observations")
    if samples[0][0] > started_at or samples[-1][0] < completed_at:
        raise ValueError("Cycle history does not cover the complete run")

    def counter(at):
        """Interpolate a measured cumulative counter only inside its bounded fine segment."""
        for left, right in pairwise(samples):
            if left[0] <= at <= right[0]:
                fraction = (at - left[0]).total_seconds() / (right[0] - left[0]).total_seconds()
                return left[1] + (right[1] - left[1]) * fraction
        raise ValueError("Cycle endpoint missing")

    powers = []
    start = started_at
    while start < completed_at:
        end = min(completed_at, start + timedelta(seconds=PROFILE_STEP_SECONDS))
        powers.append(
            (counter(end) - counter(start)) * WATTS_PER_KW * SECONDS_PER_HOUR / PROFILE_STEP_SECONDS
        )
        start = end
    return CycleProfile(tuple(powers), evidence="synthetic" if synthetic else "measured")
