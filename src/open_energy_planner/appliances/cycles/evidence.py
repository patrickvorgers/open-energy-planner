"""Refine durable cycle evidence without counting coarse and fine views of one run twice."""

from bisect import bisect_left
from dataclasses import asdict, replace
from enum import StrEnum
from itertools import accumulate, pairwise
from statistics import median_low

from open_energy_planner.demand.calculation.model import digest

from .learning import HOURLY_SAMPLE_SECONDS, MAX_RECENT_CYCLES, hourly_profile, retained_weight
from .model import (
    FINE_PROFILE_SECONDS,
    MAX_CYCLE_POWER_W,
    CycleProfile,
    average_profiles,
    instant,
    learn_cycle,
    restore_profile,
)

HOURLY_PROFILE_POLICY = 1


class ProfileBasis(StrEnum):
    """Identify the selected model's statistical, observed or explicit-completion provenance."""

    STATISTICS = "statistics"
    OBSERVED = "observed"
    EXPLICIT = "explicit"


def completion(record):
    """Normalize explicit energy-counter evidence without inventing a measured power sensor."""
    return {
        **record,
        "id": digest([record["entity_id"], record["cycle_id"]]),
        "quality": "explicit_completion",
    }


def measured(record):
    """Restore a profile from the original counters, retaining its actual source resolution."""
    if "_profile" in record:
        return record["_profile"]
    if record.get("quality") in ("recorder_state", "five_minute_statistics"):
        record["_profile"] = restore_profile(record["profile"])
        return record["_profile"]
    function = hourly_profile if record.get("quality") == "hourly_estimate" else learn_cycle
    arguments = {} if function is hourly_profile else {"synthetic": record["synthetic"]}
    record["_profile"] = function(
        record["points"],
        instant(record["started_at"]),
        instant(record["completed_at"]),
        **arguments,
    )
    return record["_profile"]


def average(records):
    """Rebuild time-weighted automatic evidence and retain legacy explicit sample contributions.

    Explicit completion records keep the existing arithmetic-mean contract.
    They add one independent observation each to the automatic aggregate, whose
    internal recency weights remain unchanged. Rebuilding uses original evidence,
    never the previously merged profile, so refreshes cannot add or drop a copy.
    """
    result, weight, previous_end = None, 0.0, None
    explicit = []
    for record in sorted(records, key=lambda item: instant(item["completed_at"])):
        if record.get("quality") == "explicit_completion":
            explicit.append(record)
            continue
        completed = instant(record["completed_at"])
        if previous_end:
            weight = retained_weight(weight, previous_end, completed)
        result = average_profiles(result, measured(record), previous_weight=weight)
        weight, previous_end = weight + 1, completed
    for record in explicit:
        result = average_profiles(result, measured(record))
    return result, float(result.samples) if explicit else weight


def distinct_ranges(records):
    """Reconcile overlapping views conservatively, retaining the most complete original observation."""
    result, group_end = [], None
    for record in sorted(records, key=lambda item: instant(item["started_at"])):
        start, end = instant(record["started_at"]), instant(record["completed_at"])
        if group_end is not None and start < group_end:
            result[-1] = max(
                (result[-1], record),
                key=lambda item: (
                    item.get("quality") == "recorder_state",
                    len(item["points"]),
                    item.get("quality") == "explicit_completion",
                    item.get("quality") == "five_minute_statistics",
                    instant(item["completed_at"]),
                ),
            )
            group_end = max(group_end, end)
        else:
            result.append(record)
            group_end = end
    return result


def representative_hourly_profile(records):
    """Build the hourly planning shape around the lower median occupied duration.

    Whole-hour attribution can move a boundary by one bin. Average nearby
    cycles with recency weights, then retime that curve without changing its
    energy. If compression exceeds the power limit, use exact-duration evidence.
    """
    duration = median_low([measured(record).duration_seconds for record in records])
    nearby = [
        record
        for record in records
        if abs(measured(record).duration_seconds - duration) <= HOURLY_SAMPLE_SECONDS
    ]
    profile, _ = average(nearby)
    if profile.duration_seconds == duration:
        return profile

    steps = duration // profile.phase_seconds
    cumulative_power = (0, *accumulate(profile.power_w))
    destination_edges = []
    for step in range(steps + 1):
        index, remainder = divmod(step * len(profile.power_w), steps)
        edge = cumulative_power[index]
        if remainder:
            edge += profile.power_w[index] * remainder / steps
        destination_edges.append(edge)
    powers = tuple(right - left for left, right in pairwise(destination_edges))
    if max(powers) > MAX_CYCLE_POWER_W:
        profile, _ = average(
            [record for record in nearby if measured(record).duration_seconds == duration]
        )
        return profile
    return replace(profile, power_w=powers)


def _reconcile_cycle_ranges(records):
    """Retain separate resolution summaries and select independent evidence for the model."""
    fine = [record for record in records if record.get("quality") != "hourly_estimate"]

    coarse = distinct_ranges(
        [record for record in records if record.get("quality") == "hourly_estimate"]
    )
    observed = [record for record in fine if record.get("observation_coverage") is True]
    if observed:
        fine = [
            record
            for record in fine
            if record.get("observation_coverage") is True
            or record.get("quality") == "explicit_completion"
        ]
    fine = distinct_ranges(fine)
    fine_starts = [instant(record["started_at"]) for record in fine]

    def overlaps_fine(record):
        """Find a finer view with a bounded lookup instead of comparing every historical pair."""
        previous = bisect_left(fine_starts, instant(record["completed_at"])) - 1
        return previous >= 0 and instant(fine[previous]["completed_at"]) > instant(
            record["started_at"]
        )

    distinct = (
        fine if observed else fine + [record for record in coarse if not overlaps_fine(record)]
    )
    explicit_only = bool(fine) and all(
        record.get("quality") == "explicit_completion" for record in fine
    )
    if explicit_only:
        # An explicit completed profile takes precedence over coarse-only fallback learning.
        distinct = fine
    return coarse, fine, distinct, bool(observed), explicit_only


def _fine_timing_profile(fine_profile, combined, fine, distinct):
    """Apply the model's energy to the finer duration within the physical power bound."""
    factor = combined.energy_kwh / fine_profile.energy_kwh
    if max(fine_profile.power_w) * factor > MAX_CYCLE_POWER_W:
        # Coarse energy cannot force a finer timing shape beyond the physical power boundary.
        distinct, combined, factor = fine, fine_profile, 1
    return (
        CycleProfile(
            tuple(power * factor for power in fine_profile.power_w),
            combined.samples,
            "synthetic" if any(record["synthetic"] for record in distinct) else "measured",
            fine_profile.measured_duration_seconds,
            fine_profile.phase_seconds,
            fine_profile.phase_durations_seconds,
        ),
        distinct,
    )


def _apply_program_profiles(settings, fine):
    """Replace the selected profiles only when opted-in fine evidence distinguishes programs."""
    settings.pop("additional_profile", None)
    if not settings.get("learn_additional_profile"):
        return

    from .programs import learn_programs

    programs = learn_programs(
        [
            (instant(record["completed_at"]), measured(record))
            for record in fine
            if record.get("quality") in ("recorder_state", "five_minute_statistics")
        ]
    )
    if programs:
        standard, additional = programs
        settings["profile"] = asdict(standard)
        settings["additional_profile"] = asdict(additional)
        settings["profile_sources"] = {"fine_cycles": standard.samples, "hourly_cycles": 0}


def _recent_cycle_rows(records):
    """Project model evidence with its original chronology, identities and source resolution."""
    return [
        {
            "id": record["id"],
            "started_at": record["started_at"],
            "completed_at": record["completed_at"],
            "energy_kwh": measured(record).energy_kwh,
            "synthetic": record["synthetic"],
            "resolution": "hour"
            if record.get("quality") == "hourly_estimate"
            else "state_events"
            if record.get("quality") == "recorder_state"
            else "5minute"
            if measured(record).phase_seconds == FINE_PROFILE_SECONDS
            else "15minute",
        }
        for record in sorted(records, key=lambda item: instant(item["completed_at"]))[
            -MAX_RECENT_CYCLES:
        ]
    ]


def refine(settings, records):
    """Use all distinct cycles for energy and available fine cycles for the timing shape.

    Fine evidence supersedes overlapping hourly ranges from the same sensor pair.
    Original archives remain intact, including ambiguous coarse ranges that may
    contain more than one finer run. Excluding those coarse ranges avoids counting
    their consumption a second time. Manual estimates remain until fine evidence
    is available. Existing explicitly recorded profiles remain usable.
    """
    coarse, fine, distinct, observed, explicit_only = _reconcile_cycle_ranges(records)
    if not distinct:
        return
    automatic = next(
        (record for record in fine + coarse if record.get("quality") != "explicit_completion"),
        None,
    )
    if automatic:
        settings["learning_source"] = automatic["power_entity"]
    else:
        settings.pop("learning_source", None)
    fine_profile, fine_weight = average(fine)
    hourly, hourly_weight = average(coarse)
    if fine_profile:
        combined, _ = average(distinct)
    else:
        combined = representative_hourly_profile(coarse)
        settings["hourly_profile_policy"] = HOURLY_PROFILE_POLICY
    if hourly:
        settings.update(
            hourly_profile=asdict(hourly),
            hourly_weight=hourly_weight,
            hourly_first_start=min((record["started_at"] for record in coarse), key=instant),
            hourly_last_end=max((record["completed_at"] for record in coarse), key=instant),
            hourly_synthetic=any(record["synthetic"] for record in coarse),
        )
    if fine_profile:
        settings.update(
            learned_profile=asdict(fine_profile),
            learning_weight=fine_weight,
            first_detected_start=min((record["started_at"] for record in fine), key=instant),
            last_detected_end=max((record["completed_at"] for record in fine), key=instant),
        )
        combined, distinct = _fine_timing_profile(fine_profile, combined, fine, distinct)
    previous = settings.get("profile", {})
    if fine_profile or (
        not settings.get("manual_profile") and previous.get("evidence") in (None, "hourly_estimate")
    ):
        settings["profile"] = asdict(combined)
    settings["profile_sources"] = {
        "hourly_cycles": combined.samples - len(fine),
        "fine_cycles": len(fine),
    }
    if observed:
        settings["profile_basis"] = ProfileBasis.OBSERVED
    elif explicit_only:
        settings["profile_basis"] = ProfileBasis.EXPLICIT
    elif any(record.get("quality") == "recorder_state" for record in fine):
        settings.pop("profile_basis", None)
    else:
        settings["profile_basis"] = ProfileBasis.STATISTICS
    _apply_program_profiles(settings, fine)
    settings["recent_cycles"] = _recent_cycle_rows(distinct)
