"""Interpret bounded original Recorder events without inventing counter samples or scaling measured power."""

from bisect import bisect_left, bisect_right
from datetime import timedelta
from itertools import pairwise
from math import isfinite

from .learning import MIN_ACTIVE_SAMPLES, MIN_ACTIVITY_W, QUIET_SAMPLES, SAMPLE_SECONDS
from .model import (
    ENERGY_TOLERANCE_KWH,
    MAX_CYCLE_HOURS,
    MAX_CYCLE_POWER_W,
    MAX_ENERGY_COUNTER_KWH,
    CycleProfile,
    instant,
)

MAX_RAW_STATES = 2049
MAX_RAW_BODY_BYTES = 512 * 1024
MAX_RAW_WINDOW_SECONDS = (MAX_CYCLE_HOURS + 2) * 3600


def states(rows, end, *, energy=False):
    """Validate original ordered events and convert their individual recorded units once.

    Unknown/unavailable and numeric faults remain explicit breaks in coverage.
    A state continues until its next event under HA's state semantics. Absence
    of a changed state is not itself a measurement-frequency claim.
    """
    if not isinstance(rows, list) or len(rows) > MAX_RAW_STATES:
        raise ValueError("Raw appliance history exceeds bounded state count")
    result = []
    factors = {"Wh": 0.001, "kWh": 1, "MWh": 1000} if energy else {"W": 1, "kW": 1000}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"at", "state", "unit"}:
            raise ValueError("Invalid original appliance state")
        at = instant(row["at"])
        if at > end or (result and at <= result[-1][0]):
            raise ValueError("Raw appliance states must retain ordered original timestamps")
        if not isinstance(row["state"], str) or len(row["state"]) > 64:
            raise ValueError("Invalid original appliance state")
        try:
            value = float(row["state"]) * factors[row["unit"]]
        except (TypeError, ValueError, KeyError, OverflowError):
            value = None
        maximum = MAX_ENERGY_COUNTER_KWH if energy else MAX_CYCLE_POWER_W
        if value is not None and (
            not isfinite(value) or abs(value) > maximum or not energy and value < 0
        ):
            value = None
        result.append((at, value))
    return result


def _detect_completed_cycles(segments, conserved, *, synthetic):
    """Detect quiet-bracketed runs and learn only profiles with conserved energy."""
    quiet_seconds = QUIET_SAMPLES * SAMPLE_SECONDS
    active_minimum = MIN_ACTIVE_SAMPLES * SAMPLE_SECONDS
    quiet, active, started, last_active = 0.0, 0.0, None, None
    cycles, runs = [], []
    finite = [value for _, _, value in segments if value is not None]
    threshold = max(MIN_ACTIVITY_W, min(finite, default=0) + 5)
    for start, finish, power in segments:
        seconds = (finish - start).total_seconds()
        if power is None:
            quiet, active, started, last_active = 0.0, 0.0, None, None
            continue
        if power > threshold:
            if started is None and quiet >= quiet_seconds:
                started, active = start, 0.0
            if started is not None:
                active += seconds
                last_active = finish
            quiet = 0.0
        else:
            quiet += seconds
            if started is not None and quiet >= quiet_seconds:
                if active >= active_minimum and timedelta(
                    seconds=active_minimum
                ) <= last_active - started <= timedelta(hours=MAX_CYCLE_HOURS):
                    candidate = {
                        "started_at": started.isoformat(),
                        "completed_at": last_active.isoformat(),
                        "profile": None,
                    }
                    runs.append(candidate)
                    if conserved(started, last_active):
                        parts = [
                            (max(started, left), min(last_active, right), value)
                            for left, right, value in segments
                            if left < last_active and right > started
                        ]
                        learned = CycleProfile(
                            tuple(value for _, _, value in parts),
                            evidence="synthetic" if synthetic else "measured",
                            phase_durations_seconds=tuple(
                                (right - left).total_seconds() for left, right, _ in parts
                            ),
                        )
                        cycles.append(
                            {
                                "started_at": started.isoformat(),
                                "completed_at": last_active.isoformat(),
                                "profile": learned,
                            }
                        )
                        candidate["profile"] = learned
                started, active, last_active = None, 0.0, None
    return cycles, runs, started, active, quiet >= quiet_seconds and started is None


def analyze(body):
    """Keep actual curves visible and train only fully bracketed, independently conserved runs.

    Power uses zero-order hold between actual Recorder state events. Extension
    to the capture boundary additionally requires a separately reported fresh
    current HA state. Counter values are never interpolated. Actual bracketing
    counter anchors must conserve the independently integrated power over their
    complete covered span, including measured quiet flanks. Unknowns and resets
    reject training while preserving original visible state points.
    """
    first, end = instant(body["start"]), instant(body["end"])
    if not 0 < (end - first).total_seconds() <= MAX_RAW_WINDOW_SECONDS:
        raise ValueError("Invalid bounded raw appliance history window")
    if type(body["limited"]) is not bool or type(body["synthetic"]) is not bool:
        raise ValueError("Invalid raw appliance history provenance")
    powers = states(body["power_states"], end)
    counters = states(body["energy_states"], end, energy=True)

    def fresh(field):
        """A carry-in timestamp is never a fresh report. Freshness has its own original HA provenance."""
        reported = body.get(field)
        return reported is not None and timedelta(0) <= end - instant(reported) <= timedelta(
            seconds=2 * SAMPLE_SECONDS
        )

    power_end = end if fresh("power_reported_at") else powers[-1][0] if powers else first
    segments = [
        (max(at, first), min(until, power_end), value)
        for (at, value), until in zip(powers, [at for at, _ in powers[1:]] + [power_end])
        if max(at, first) < min(until, power_end)
    ]

    def integrate(left, right):
        """Integrate only a completely covered power-state span, preserving every actual phase edge."""
        occupied, energy = 0.0, 0.0
        for start, finish, power in segments:
            seconds = max(0, (min(right, finish) - max(left, start)).total_seconds())
            if seconds and power is None:
                return None
            occupied += seconds
            energy += (power or 0) * seconds / 3600000
        return energy if abs(occupied - (right - left).total_seconds()) <= 1e-6 else None

    def conserved(start, finish):
        """Compare original bracketing counter delta with independent power integration, without scaling."""
        times = [at for at, _ in counters]
        left, right = bisect_right(times, start) - 1, bisect_left(times, finish)
        if left < 0 or right >= len(counters):
            return False
        anchors = counters[left : right + 1]
        if any(value is None for _, value in anchors) or any(
            later_counter[1] < earlier_counter[1]
            for earlier_counter, later_counter in pairwise(anchors)
        ):
            return False
        measured = anchors[-1][1] - anchors[0][1]
        integrated = integrate(anchors[0][0], anchors[-1][0])
        return integrated is not None and abs(integrated - measured) <= ENERGY_TOLERANCE_KWH

    cycles, runs, started, active, quiet = _detect_completed_cycles(
        segments, conserved, synthetic=body["synthetic"]
    )
    active_minimum = MIN_ACTIVE_SAMPLES * SAMPLE_SECONDS
    running = None
    if (
        started is not None
        and active >= active_minimum
        and fresh("power_reported_at")
        and fresh("energy_reported_at")
    ):
        energy_until = counters[-1][0] if counters else first
        measured = integrate(started, energy_until) if energy_until >= started else None
        if (
            measured is not None
            and end - energy_until <= timedelta(seconds=2 * SAMPLE_SECONDS)
            and conserved(started, energy_until)
        ):
            running = {
                "started_at": started.isoformat(),
                "observed_until": energy_until.isoformat(),
                "energy_kwh": measured,
                "status": "observed",
            }
    return {
        "cycles": cycles,
        "runs": runs,
        "running": running,
        "quiet": quiet,
        "observed_until": power_end.isoformat(),
        "status": "running"
        if running
        else "complete"
        if cycles
        else "unverified"
        if runs
        else "limited"
        if body["limited"]
        else "no_complete_cycle",
        "power_curve": [{"at": at.isoformat(), "power_w": value} for at, value in powers],
        "energy_curve": [{"at": at.isoformat(), "kwh": value} for at, value in counters],
    }
