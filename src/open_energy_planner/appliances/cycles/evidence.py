"""Refine durable cycle evidence without counting coarse and fine views of one run twice."""

from bisect import bisect_left
from dataclasses import asdict

from .learning import MAX_RECENT_CYCLES, hourly_profile, retained_weight
from .model import MAX_CYCLE_POWER_W, CycleProfile, average_profiles, instant, learn_cycle


def measured(record):
    """Restore a profile from the original counters, retaining its actual source resolution."""
    if "_profile" in record:
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
    """Average chronological observations with the existing recency policy and independent raw count."""
    result, weight, previous_end = None, 0.0, None
    for record in sorted(records, key=lambda item: instant(item["completed_at"])):
        completed = instant(record["completed_at"])
        if previous_end:
            weight = retained_weight(weight, previous_end, completed)
        result = average_profiles(result, measured(record), previous_weight=weight)
        weight, previous_end = weight + 1, completed
    return result, weight


def distinct_ranges(records):
    """Reconcile overlapping views conservatively, retaining the most complete original observation."""
    result, group_end = [], None
    for record in sorted(records, key=lambda item: instant(item["started_at"])):
        start, end = instant(record["started_at"]), instant(record["completed_at"])
        if group_end is not None and start < group_end:
            result[-1] = max(
                (result[-1], record),
                key=lambda item: (len(item["points"]), instant(item["completed_at"])),
            )
            group_end = max(group_end, end)
        else:
            result.append(record)
            group_end = end
    return result


def refine(settings, records):
    """Use all distinct cycles for energy and available fine cycles for the timing shape.

    Fine evidence supersedes overlapping hourly ranges from the same sensor pair.
    Original archives remain intact, including ambiguous coarse ranges that may
    contain more than one finer run. Excluding those coarse ranges avoids counting
    their consumption a second time. Manual estimates remain until fine evidence
    is available. Existing explicitly recorded profiles remain usable.
    """
    fine = distinct_ranges(
        [record for record in records if record.get("quality") != "hourly_estimate"]
    )
    coarse = distinct_ranges(
        [record for record in records if record.get("quality") == "hourly_estimate"]
    )
    fine_starts = [instant(record["started_at"]) for record in fine]

    def overlaps_fine(record):
        """Find a finer view with a bounded lookup instead of comparing every historical pair."""
        previous = bisect_left(fine_starts, instant(record["completed_at"])) - 1
        return previous >= 0 and instant(fine[previous]["completed_at"]) > instant(
            record["started_at"]
        )

    distinct = fine + [record for record in coarse if not overlaps_fine(record)]
    if not distinct:
        return
    fine_profile, fine_weight = average(fine)
    hourly, hourly_weight = average(coarse)
    combined, _ = average(distinct)
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
        factor = combined.energy_kwh / fine_profile.energy_kwh
        if max(fine_profile.power_w) * factor > MAX_CYCLE_POWER_W:
            # Coarse energy cannot force a finer timing shape beyond the physical power boundary.
            distinct, combined, factor = fine, fine_profile, 1
        combined = CycleProfile(
            tuple(power * factor for power in fine_profile.power_w),
            combined.samples,
            "synthetic" if any(record["synthetic"] for record in distinct) else "measured",
            fine_profile.measured_duration_seconds,
            fine_profile.phase_seconds,
        )
    previous = settings.get("profile", {})
    if fine_profile or (
        not settings.get("manual_profile") and previous.get("evidence") in (None, "hourly_estimate")
    ):
        settings["profile"] = asdict(combined)
    settings["profile_sources"] = {
        "hourly_cycles": len(distinct) - len(fine),
        "fine_cycles": len(fine),
    }
    settings["recent_cycles"] = [
        {
            "id": record["id"],
            "started_at": record["started_at"],
            "completed_at": record["completed_at"],
            "energy_kwh": measured(record).energy_kwh,
            "synthetic": record["synthetic"],
            "resolution": "hour" if record.get("quality") == "hourly_estimate" else "5minute",
        }
        for record in sorted(distinct, key=lambda item: instant(item["completed_at"]))[
            -MAX_RECENT_CYCLES:
        ]
    ]
