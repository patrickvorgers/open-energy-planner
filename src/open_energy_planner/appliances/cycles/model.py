"""Immutable appliance work and measured profiles. Times are aware and powers are AC watts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from heapq import merge
from itertools import accumulate, pairwise
from math import ceil, isfinite

from open_energy_planner.core.units import SECONDS_PER_HOUR, SECONDS_PER_MINUTE, WATTS_PER_KW
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES

PROFILE_STEP_SECONDS = FINE_INTERVAL_MINUTES * SECONDS_PER_MINUTE
FINE_PROFILE_SECONDS = 5 * SECONDS_PER_MINUTE
MAX_CYCLE_HOURS = 12
MAX_CYCLE_POWER_W = 20000
MAX_ENERGY_COUNTER_KWH = 1e12
MAX_PROFILE_STEPS = int(MAX_CYCLE_HOURS * SECONDS_PER_HOUR / FINE_PROFILE_SECONDS)
MAX_VARIABLE_PROFILE_STEPS = 2048
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
    """One immutable average or estimate in explicitly declared mean-AC-power phases."""

    power_w: tuple[float, ...]
    samples: int = 1
    evidence: str = "measured"
    measured_duration_seconds: float | None = None
    phase_seconds: int = PROFILE_STEP_SECONDS
    phase_durations_seconds: tuple[float, ...] | None = None

    def __post_init__(self):
        """Require finite measured nonnegative phases, including legitimate zero-power pauses."""
        if (
            type(self.phase_seconds) is not int
            or self.phase_seconds not in (FINE_PROFILE_SECONDS, PROFILE_STEP_SECONDS)
            or not isinstance(self.power_w, tuple)
            or not 1
            <= len(self.power_w)
            <= (
                MAX_VARIABLE_PROFILE_STEPS
                if self.phase_durations_seconds is not None
                else MAX_CYCLE_HOURS * SECONDS_PER_HOUR / self.phase_seconds
            )
            or type(self.samples) is not int
            or self.samples < 1
            or self.evidence not in ("measured", "synthetic", "manual", "hourly_estimate")
            or any(
                type(power) not in (int, float)
                or not isfinite(power)
                or not 0 <= power <= MAX_CYCLE_POWER_W
                for power in self.power_w
            )
            or (
                self.phase_durations_seconds is not None
                and (
                    not isinstance(self.phase_durations_seconds, tuple)
                    or len(self.phase_durations_seconds) != len(self.power_w)
                    or any(
                        type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 1e-6
                        for seconds in self.phase_durations_seconds
                    )
                    or self.measured_duration_seconds is not None
                    or round(sum(self.phase_durations_seconds), 6)
                    > MAX_CYCLE_HOURS * SECONDS_PER_HOUR
                )
            )
            or (
                self.measured_duration_seconds is not None
                and (
                    type(self.measured_duration_seconds) not in (int, float)
                    or not isfinite(self.measured_duration_seconds)
                    or not (len(self.power_w) - 1) * self.phase_seconds
                    < self.measured_duration_seconds
                    <= len(self.power_w) * self.phase_seconds
                )
            )
            or self.duration_seconds < PROFILE_STEP_SECONDS
            or sum(self.power_w) <= 0
        ):
            raise ValueError("A complete measured cycle profile is required")

    @property
    def duration_seconds(self):
        """Return the conservative complete profile duration in seconds."""
        if self.phase_durations_seconds is not None:
            return round(sum(self.phase_durations_seconds), 6)
        return self.measured_duration_seconds or len(self.power_w) * self.phase_seconds

    @property
    def phase_boundaries(self):
        """Return actual elapsed phase boundaries without inventing a constant measurement cadence."""
        if self.phase_durations_seconds is not None:
            return (0, *(round(value, 6) for value in accumulate(self.phase_durations_seconds)))
        return (
            *[index * self.phase_seconds for index in range(len(self.power_w))],
            self.duration_seconds,
        )

    @property
    def energy_kwh(self):
        """Return the whole-cycle AC energy, including partial-power phases."""
        return (
            sum(
                power * (right - left)
                for power, (left, right) in zip(self.power_w, pairwise(self.phase_boundaries))
            )
            / SECONDS_PER_HOUR
            / WATTS_PER_KW
        )

    def energy(self, started_at, left, right):
        """Integrate phase overlaps exactly, including mixed planning intervals and remaining work."""
        energy = 0.0
        for power, (first, last) in zip(self.power_w, pairwise(self.phase_boundaries)):
            start = started_at + timedelta(seconds=first)
            end = started_at + timedelta(seconds=last)
            overlap = max(0, (min(end, right) - max(start, left)).total_seconds())
            energy += power * overlap / SECONDS_PER_HOUR / WATTS_PER_KW
        return energy


def restore_profile(value):
    """Restore either legacy uniform or actual irregular immutable phase durations from JSON."""
    return CycleProfile(
        tuple(value["power_w"]),
        value["samples"],
        value["evidence"],
        value.get("measured_duration_seconds"),
        value.get("phase_seconds", PROFILE_STEP_SECONDS),
        tuple(value["phase_durations_seconds"])
        if value.get("phase_durations_seconds") is not None
        else None,
    )


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
    """Average weighted cycle means with actual irregular or coarser uniform timing.

    An admitted irregular boundary union is swept with advancing source indices.
    Each output interval lies within one constant-power phase per source, with
    zero contribution after a shorter source ends. This preserves weighted AC
    energy and the longest duration without repeatedly integrating whole traces.
    """
    if previous is None:
        return learned
    weight = previous.samples if previous_weight is None else previous_weight
    if type(weight) not in (int, float) or not isfinite(weight) or weight < 0:
        raise ValueError("Invalid retained cycle weight")
    duration = max(previous.duration_seconds, learned.duration_seconds)
    phase_seconds = max(previous.phase_seconds, learned.phase_seconds)
    if previous.phase_durations_seconds is not None or learned.phase_durations_seconds is not None:
        previous_boundaries, learned_boundaries = (
            previous.phase_boundaries,
            learned.phase_boundaries,
        )
        boundaries = []
        for boundary in merge(previous_boundaries, learned_boundaries):
            if not boundaries or boundary != boundaries[-1]:
                boundaries.append(boundary)
                if len(boundaries) - 1 > MAX_VARIABLE_PROFILE_STEPS:
                    raise ValueError("Cycle profile exceeds bounded phase complexity")
        previous_index, learned_index = 0, 0
        powers = []
        for left in boundaries[:-1]:
            while (
                previous_index + 1 < len(previous_boundaries)
                and previous_boundaries[previous_index + 1] <= left
            ):
                previous_index += 1
            while (
                learned_index + 1 < len(learned_boundaries)
                and learned_boundaries[learned_index + 1] <= left
            ):
                learned_index += 1
            # Every union interval lies within one phase per source. Exhausted sources contribute zero.
            previous_power = (
                previous.power_w[previous_index] if previous_index < len(previous.power_w) else 0
            )
            learned_power = (
                learned.power_w[learned_index] if learned_index < len(learned.power_w) else 0
            )
            powers.append((previous_power * weight + learned_power) / (weight + 1))
        return CycleProfile(
            tuple(powers),
            previous.samples + 1,
            "synthetic" if "synthetic" in (previous.evidence, learned.evidence) else "measured",
            phase_durations_seconds=tuple(right - left for left, right in pairwise(boundaries)),
        )

    def phase_energy(cycle, left, right):
        """Integrate only intersecting source phases without inventing finer source detail."""
        return sum(
            cycle.power_w[index]
            * max(
                0,
                min(right, (index + 1) * cycle.phase_seconds, cycle.duration_seconds)
                - max(left, index * cycle.phase_seconds),
            )
            for index in range(
                int(left // cycle.phase_seconds),
                min(len(cycle.power_w), ceil(right / cycle.phase_seconds)),
            )
        )

    powers = tuple(
        (
            phase_energy(
                previous, index * phase_seconds, min(duration, (index + 1) * phase_seconds)
            )
            * weight
            + phase_energy(
                learned, index * phase_seconds, min(duration, (index + 1) * phase_seconds)
            )
        )
        / (weight + 1)
        / min(phase_seconds, duration - index * phase_seconds)
        for index in range(ceil(duration / phase_seconds))
    )
    return CycleProfile(
        powers,
        previous.samples + 1,
        "hourly_estimate"
        if "hourly_estimate" in (previous.evidence, learned.evidence)
        else "synthetic"
        if "synthetic" in (previous.evidence, learned.evidence)
        else "measured",
        duration if duration % phase_seconds else None,
        phase_seconds,
    )


def learn_cycle(points, started_at, completed_at, *, synthetic=False):
    """Build a completed cycle from fine cumulative kWh observations and known boundaries.

    Linear interpolation is limited to adjacent measurements no farther apart
    than one profile phase. The output retains five-minute detail only when the observations support it.
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

    phase_seconds = (
        FINE_PROFILE_SECONDS
        if all(
            (right[0] - left[0]).total_seconds() <= FINE_PROFILE_SECONDS
            for left, right in pairwise(samples)
        )
        else PROFILE_STEP_SECONDS
    )
    powers = []
    start = started_at
    while start < completed_at:
        end = min(completed_at, start + timedelta(seconds=phase_seconds))
        powers.append(
            (counter(end) - counter(start))
            * WATTS_PER_KW
            * SECONDS_PER_HOUR
            / (end - start).total_seconds()
        )
        start = end
    return CycleProfile(
        tuple(powers),
        evidence="synthetic" if synthetic else "measured",
        measured_duration_seconds=duration if duration % phase_seconds else None,
        phase_seconds=phase_seconds,
    )
