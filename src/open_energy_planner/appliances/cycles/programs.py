"""Bound additional appliance profiles to two repeatable, distinguishable power patterns."""

from bisect import bisect_right
from enum import StrEnum
from math import ceil
from statistics import mean, median

from open_energy_planner.core.units import SECONDS_PER_MINUTE

from .learning import retained_weight
from .model import FINE_PROFILE_SECONDS, average_profiles, instant, restore_profile

MIN_PROGRAM_CYCLES = 3
MIN_PROGRAM_DURATION_GAP_SECONDS = 30 * SECONDS_PER_MINUTE
MIN_PROGRAM_DURATION_RATIO = 1.25
MIN_PROGRAM_ENERGY_RATIO = 1.2
RECOGNITION_WINDOW_SECONDS = 60 * SECONDS_PER_MINUTE
MIN_RECOGNITION_BINS = 3
MIN_ERROR_MARGIN_W = 25
MAX_RELATIVE_ERROR = 0.25


class CycleProgram(StrEnum):
    """Name the two profile choices on the wire and in retained planning evidence."""

    DEFAULT = "default"
    ADDITIONAL = "additional"


def bin_power(offsets, powers, duration):
    """Integrate known constant-power phases into five-minute mean power, including partial ends."""
    if not offsets or offsets[0] > 0:
        return ()
    bins = []
    for left in range(0, ceil(duration), FINE_PROFILE_SECONDS):
        right = min(left + FINE_PROFILE_SECONDS, duration)
        if right <= left:
            break
        index = bisect_right(offsets, left) - 1
        energy = 0.0
        while left < right:
            power = powers[index]
            if power is None or power < 0:
                return tuple(bins)
            end = min(right, offsets[index + 1]) if index + 1 < len(offsets) else right
            energy += power * (end - left)
            left = end
            index += 1
        bins.append(energy / (right - len(bins) * FINE_PROFILE_SECONDS))
    return tuple(bins)


def profile_bins(profile, duration):
    """Preserve integrated cycle energy when converting irregular phases to a common time grid."""
    return bin_power(profile.phase_boundaries[:-1], profile.power_w, duration)


def classify_prefix(first, second, measured_powers):
    """Return the first defensible classification of at least fifteen minutes of measured power."""
    length = min(
        len(measured_powers),
        int(min(first.duration_seconds, second.duration_seconds) // FINE_PROFILE_SECONDS),
    )
    if length < MIN_RECOGNITION_BINS:
        return None
    first_powers, second_powers = (
        profile_bins(candidate, length * FINE_PROFILE_SECONDS) for candidate in (first, second)
    )
    for count in range(MIN_RECOGNITION_BINS, length + 1):
        measured = measured_powers[:count]
        scale = max(mean(measured), mean(first_powers[:count]), mean(second_powers[:count]))
        errors = [
            mean(abs(actual - expected) for actual, expected in zip(measured, candidate[:count]))
            for candidate in (first_powers, second_powers)
        ]
        if min(errors) <= scale * MAX_RELATIVE_ERROR and abs(errors[0] - errors[1]) >= max(
            MIN_ERROR_MARGIN_W, scale * MAX_RELATIVE_ERROR
        ):
            return CycleProgram.DEFAULT if errors[0] < errors[1] else CycleProgram.ADDITIONAL
    return None


def average_program(samples):
    """Average original phases with the same recency weighting as the single profile."""
    result, weight, previous_end = None, 0.0, None
    for completed, profile in samples:
        if previous_end:
            weight = retained_weight(weight, previous_end, completed)
        result = average_profiles(result, profile, previous_weight=weight)
        weight, previous_end = weight + 1, completed
    return result


def learn_programs(samples):
    """Admit at most one extra program after a separate complete run validates each group."""
    profiles = sorted(samples, key=lambda sample: sample[1].duration_seconds)
    splits = []
    for split in range(MIN_PROGRAM_CYCLES, len(profiles) - MIN_PROGRAM_CYCLES + 1):
        short, long = (profiles[index][1].duration_seconds for index in (split - 1, split))
        gap = long - short
        if gap >= MIN_PROGRAM_DURATION_GAP_SECONDS and long >= short * MIN_PROGRAM_DURATION_RATIO:
            splits.append((gap, split))
    if not splits:
        return None
    _, split = max(splits)
    first, second = (
        sorted(group, key=lambda sample: sample[0])
        for group in (profiles[:split], profiles[split:])
    )
    if any(len({completed.date() for completed, _ in group}) < 2 for group in (first, second)):
        return None

    first_energy, second_energy = sorted(
        median([profile.energy_kwh for _, profile in group]) for group in (first[:-1], second[:-1])
    )
    if second_energy < first_energy * MIN_PROGRAM_ENERGY_RATIO:
        return None
    trained_first, trained_second = average_program(first[:-1]), average_program(second[:-1])
    for group, expected in ((first, CycleProgram.DEFAULT), (second, CycleProgram.ADDITIONAL)):
        held_out = group[-1][1]
        powers = profile_bins(held_out, min(held_out.duration_seconds, RECOGNITION_WINDOW_SECONDS))
        if classify_prefix(trained_first, trained_second, powers) != expected:
            return None

    standard, additional = (first, second) if len(first) >= len(second) else (second, first)
    return average_program(standard), average_program(additional)


def recognize(run, rows, raw_power):
    """Compare the initial covered meter intervals with the two profiles frozen for this run."""
    start, observed_until = instant(run["started_at"]), instant(run["observed_until"])
    elapsed = min((observed_until - start).total_seconds(), RECOGNITION_WINDOW_SECONDS)
    elapsed = int(elapsed // FINE_PROFILE_SECONDS) * FINE_PROFILE_SECONDS
    if raw_power:
        offsets = [(instant(row["at"]) - start).total_seconds() for row in raw_power]
        powers = bin_power(offsets, [row["power_w"] for row in raw_power], elapsed)
    else:
        readings = {(instant(row["at"]) - start).total_seconds(): row["power_w"] for row in rows}
        powers = []
        for offset in range(0, elapsed, FINE_PROFILE_SECONDS):
            power = readings.get(offset)
            if power is None or power < 0:
                break
            powers.append(power)
    return classify_prefix(
        restore_profile(run["profile"]), restore_profile(run["additional_profile"]), powers
    )
