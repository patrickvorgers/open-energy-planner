"""Recognize bounded complete cycles from paired fine or hourly AC measurements.

This is conservative statistical segmentation, not a claim to know appliance
programs or readiness. Quiet periods bracket cycles. Gaps and counter resets
break evidence, while shorter zero-power pauses remain inside a cycle.
"""

from datetime import timedelta
from itertools import pairwise
from math import ceil, isfinite

from open_energy_planner.appliances.cycles.model import (
    MAX_CYCLE_HOURS,
    MAX_CYCLE_POWER_W,
    MAX_ENERGY_COUNTER_KWH,
    PROFILE_STEP_SECONDS,
    CycleProfile,
    instant,
)
from open_energy_planner.core.units import (
    MINUTES_PER_HOUR,
    SECONDS_PER_DAY,
    SECONDS_PER_MINUTE,
    WATTS_PER_KW,
)

SAMPLE_SECONDS = 300
QUIET_SAMPLES = 6
MIN_ACTIVE_SAMPLES = 3
MIN_COMPLETE_CYCLES = 2
MIN_ACTIVITY_W = 10.0
STANDBY_MARGIN_W = 5.0
STANDBY_QUANTILE = 0.1
MAX_RECENT_CYCLES = 12
MAX_HISTORY_DAYS = 7
MAX_HISTORY_SAMPLES = (MAX_HISTORY_DAYS + 1) * SECONDS_PER_DAY // SAMPLE_SECONDS
RECENCY_HALF_LIFE_DAYS = 90
HOURLY_SAMPLE_SECONDS = 3600
PROFILE_MINUTES = PROFILE_STEP_SECONDS / SECONDS_PER_MINUTE


def retained_weight(weight, previous_end, completed_at):
    """Decay accumulated cycle weight by elapsed calendar time, not by how often the appliance runs."""
    elapsed = (completed_at - previous_end).total_seconds()
    if elapsed < 0:
        raise ValueError("Cycle weighting requires chronological evidence")
    return weight * 2 ** (-elapsed / (RECENCY_HALF_LIFE_DAYS * SECONDS_PER_DAY))


def history_quantities(row):
    """Treat numeric sensor faults as unknown W/kWh without hiding malformed transport types."""
    quantities = []
    for field, minimum, maximum in (
        ("power_w", 0, MAX_CYCLE_POWER_W),
        ("kwh", -MAX_ENERGY_COUNTER_KWH, MAX_ENERGY_COUNTER_KWH),
    ):
        value = row[field]
        if value is not None and type(value) not in (int, float):
            raise ValueError("Invalid appliance history quantity type")
        quantities.append(
            value if value is not None and minimum <= value <= maximum and isfinite(value) else None
        )
    return tuple(quantities)


def history_evidence(rows, detected, *, sample_seconds=SAMPLE_SECONDS, start=None, end=None):
    """Summarize already validated received data, without claiming unqueried HA coverage.

    A usable interval has paired, finite, within-bound power/energy and an adjacent nondecreasing
    energy predecessor. The first row supplies a baseline, not a usable delta.

    Args:
        rows: Validated interval starts with mean W and cumulative end kWh.
        detected: Caller-supplied cycle count, returned unchanged. Fine history
            reports all detected candidates, including skipped or rejected runs.
            Hourly batches report only newly accepted cycles.
        sample_seconds: Row resolution in seconds, matching the supplied rows.
        start: Inclusive core start, defaulting to the first received row.
        end: Exclusive core end, defaulting to one interval after the last row.

    Returns:
        Coverage within the received span, or the explicit requested core when
        bounds are supplied. Missing core timestamps count against coverage.
        Rows outside that core can supply baselines but never usable intervals.
        Empty unbounded input has unknown dates and zero intervals.
    """
    if not rows and start is None:
        return {
            "start": None,
            "end": None,
            "expected_intervals": 0,
            "usable_intervals": 0,
            "detected_cycles": detected,
        }
    first = start if start is not None else instant(rows[0]["at"])
    until = end if end is not None else instant(rows[-1]["at"]) + timedelta(seconds=sample_seconds)
    checked = [(instant(row["at"]), *history_quantities(row)) for row in rows]
    usable = sum(
        current[1] is not None
        and current[2] is not None
        and previous[2] is not None
        and current[2] >= previous[2]
        and (current[0] - previous[0]).total_seconds() == sample_seconds
        and first <= current[0] < until
        for previous, current in pairwise(checked)
    )
    return {
        "start": first.isoformat(),
        "end": until.isoformat(),
        "expected_intervals": int((until - first).total_seconds() / sample_seconds),
        "usable_intervals": usable,
        "detected_cycles": detected,
    }


def manual_profile(duration_minutes, energy_kwh):
    """Distribute manual kWh uniformly, retaining energy in a partial final quarter.

    Planning conservatively rounds duration up to a quarter. This estimate does
    not claim to capture heater peaks. Original duration and energy stay saved.
    """
    if (
        type(duration_minutes) not in (int, float)
        or type(energy_kwh) not in (int, float)
        or not isfinite(duration_minutes)
        or not isfinite(energy_kwh)
        or not PROFILE_MINUTES <= duration_minutes <= MAX_CYCLE_HOURS * MINUTES_PER_HOUR
        or energy_kwh <= 0
    ):
        raise ValueError("Enter a duration of 15 to 720 minutes and positive energy in kWh")
    power = energy_kwh * WATTS_PER_KW * MINUTES_PER_HOUR / duration_minutes
    if power > MAX_CYCLE_POWER_W:
        raise ValueError("Manual average power exceeds 20000 W")
    powers = tuple(
        power * min(PROFILE_MINUTES, duration_minutes - index * PROFILE_MINUTES) / PROFILE_MINUTES
        for index in range(ceil(duration_minutes / PROFILE_MINUTES))
    )
    return CycleProfile(powers, evidence="manual")


def detect_cycles(rows, now, *, sample_seconds=SAMPLE_SECONDS):
    """Return complete ranges and cumulative kWh points, never fill missing samples.

    Each row contains the interval start, mean W, and cumulative kWh at its end.
    Five-minute rows require thirty quiet minutes on both sides and at least
    fifteen active minutes. Hourly rows conservatively round these thresholds
    to one quiet hour on each side and one active hour. Ten W or five W above
    the observed low decile defines activity. The seven-day fine-history request
    has one additional admission day of tolerance. Hourly history has no age cap.
    Continuous loads and ambiguous long pauses are not reliable program labels.
    Numeric power/counter faults become unknown samples. They break the affected
    run without preventing later valid cycles or historical checkpoint progress.

    Args:
        rows: Ordered completed interval means in W and cumulative end kWh.
        now: Aware admission time. Future or unfinished intervals are rejected.
        sample_seconds: SAMPLE_SECONDS (300) or HOURLY_SAMPLE_SECONDS (3600).

    Raises:
        ValueError: Resolution, chronology, sample shape or quantity type is invalid.
    """
    if sample_seconds not in (SAMPLE_SECONDS, HOURLY_SAMPLE_SECONDS):
        raise ValueError("Unsupported cycle history resolution")
    if not isinstance(rows, list) or len(rows) > MAX_HISTORY_SAMPLES:
        raise ValueError("Too many appliance history samples")
    checked = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"at", "power_w", "kwh"}:
            raise ValueError("Invalid appliance history sample")
        at = instant(row["at"])
        if (
            at.timestamp() % sample_seconds
            or at + timedelta(seconds=sample_seconds) > now
            or (
                sample_seconds == SAMPLE_SECONDS and at < now - timedelta(days=MAX_HISTORY_DAYS + 1)
            )
            or (checked and at <= checked[-1][0])
        ):
            raise ValueError("History needs ordered completed intervals at the declared resolution")
        checked.append((at, *history_quantities(row)))
    powers = sorted(
        power for _, power, energy in checked if power is not None and energy is not None
    )
    if not powers:
        return []
    threshold = max(
        MIN_ACTIVITY_W, powers[int((len(powers) - 1) * STANDBY_QUANTILE)] + STANDBY_MARGIN_W
    )
    quiet, active, start_index, last_active = 0, 0, None, None
    cycles = []
    for index, (at, power, energy) in enumerate(checked):
        previous = checked[index - 1] if index else None
        broken = (
            power is None
            or energy is None
            or previous is None
            or previous[2] is None
            or at - previous[0] != timedelta(seconds=sample_seconds)
            or energy < previous[2]
        )
        if broken:
            quiet, active, start_index, last_active = 0, 0, None, None
            continue
        if power > threshold:
            if start_index is None and quiet >= ceil(
                QUIET_SAMPLES * SAMPLE_SECONDS / sample_seconds
            ):
                start_index, active = index, 0
            if start_index is not None:
                last_active, active = index, active + 1
            quiet = 0
        else:
            quiet += 1
            if start_index is not None and quiet >= ceil(
                QUIET_SAMPLES * SAMPLE_SECONDS / sample_seconds
            ):
                if active >= ceil(MIN_ACTIVE_SAMPLES * SAMPLE_SECONDS / sample_seconds):
                    start = checked[start_index][0]
                    end = checked[last_active][0] + timedelta(seconds=sample_seconds)
                    if (
                        timedelta(seconds=PROFILE_STEP_SECONDS)
                        <= end - start
                        <= timedelta(hours=MAX_CYCLE_HOURS)
                    ):
                        points = [
                            {
                                "at": (stamp + timedelta(seconds=sample_seconds)).isoformat(),
                                "kwh": value,
                            }
                            for stamp, _, value in checked[start_index - 1 : last_active + 1]
                        ]
                        cycles.append((start, end, points))
                start_index, active, last_active = None, 0, None
    return cycles


def hourly_profile(points, started_at, completed_at):
    """Spread measured hourly energy uniformly into explicitly estimated quarter phases.

    The full occupied hours form a conservative duration. Subhour start times,
    heater peaks and multiple runs within an hour cannot be reconstructed.
    """
    duration = (completed_at - started_at).total_seconds()
    if not HOURLY_SAMPLE_SECONDS <= duration <= MAX_CYCLE_HOURS * HOURLY_SAMPLE_SECONDS:
        raise ValueError("Invalid hourly cycle duration")
    if (
        started_at.timestamp() % HOURLY_SAMPLE_SECONDS
        or duration % HOURLY_SAMPLE_SECONDS
        or len(points) != int(duration / HOURLY_SAMPLE_SECONDS) + 1
        or instant(points[0]["at"]) != started_at
        or instant(points[-1]["at"]) != completed_at
        or any(
            type(point["kwh"]) not in (int, float)
            or not isfinite(point["kwh"])
            or abs(point["kwh"]) > MAX_ENERGY_COUNTER_KWH
            for point in points
        )
    ):
        raise ValueError("Hourly estimate needs complete finite hourly counter evidence")
    powers = []
    for previous, current in pairwise(points):
        if (
            instant(current["at"]) - instant(previous["at"])
        ).total_seconds() != HOURLY_SAMPLE_SECONDS:
            raise ValueError("Hourly estimate has a history gap")
        energy = current["kwh"] - previous["kwh"]
        if energy < 0:
            raise ValueError("Hourly estimate has a counter reset")
        powers.extend([energy * WATTS_PER_KW] * int(HOURLY_SAMPLE_SECONDS / PROFILE_STEP_SECONDS))
    return CycleProfile(tuple(powers), evidence="hourly_estimate")
