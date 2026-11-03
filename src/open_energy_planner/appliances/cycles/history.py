"""Resumable hourly-history learning, kept separate from fine measured cycles."""

from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from enum import StrEnum

from open_energy_planner.demand.calculation.model import digest

from .learning import (
    HOURLY_SAMPLE_SECONDS,
    MIN_COMPLETE_CYCLES,
    detect_cycles,
    history_evidence,
    hourly_profile,
    retained_weight,
)
from .model import MAX_CYCLE_HOURS, CycleProfile, average_profiles, instant

HISTORY_BATCH_DAYS = 7
HISTORY_LOOKAHEAD_HOURS = 1
HISTORY_OVERLAP_HOURS = MAX_CYCLE_HOURS + HISTORY_LOOKAHEAD_HOURS


class HistoryPhase(StrEnum):
    """Durable source-bound scan phases exposed over the appliance API."""

    DISCOVERING = "discovering"
    PROCESSING = "processing"
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


def member(service, entity, power_entity):
    """Require the current selected Energy pair before accepting progress or measurements."""
    service.member(entity)
    if not power_entity or power_entity != service.source(entity):
        raise ValueError("History must use the current Home Assistant Energy power sensor")


def scan(service, entity, power_entity, phase, start, end, now):
    """Persist discovery, resumable processing or failure without inventing available coverage.

    Processing bounds are aware, UTC-hour-aligned and exclusive at the end.
    Their first boundary is stable for the current source. A later target may
    extend the range, but neither a retry nor a failure resets its cursor.
    Discovery and failure carry no caller-supplied progress percentage.

    Raises:
        ValueError: The source, phase or chronological bounds are invalid.
        OSError: Persistence fails, retaining the previous checkpoint.
    """
    member(service, entity, power_entity)
    phase = HistoryPhase(phase)
    if phase == HistoryPhase.COMPLETE:
        raise ValueError("Invalid history scan phase")
    service.sync_sources()
    data = deepcopy(service.state)
    settings = data["devices"].setdefault(entity, {"shiftable": False})
    previous = settings.get("history_import", {})
    current = {**previous, "phase": phase, "power_entity": power_entity}
    if phase == HistoryPhase.PROCESSING:
        first, last = instant(start), instant(end)
        if (
            first.timestamp() < 0
            or not first < last <= now
            or any(value.timestamp() % HOURLY_SAMPLE_SECONDS for value in (first, last))
        ):
            raise ValueError("Invalid hourly history bounds")
        if previous.get("start") and previous["start"] != first.isoformat():
            raise ValueError("History source range changed. Retained learning is unchanged")
        if previous.get("end") and last < instant(previous["end"]):
            raise ValueError("History target cannot move backwards")
        current.update(
            start=first.isoformat(),
            end=last.isoformat(),
            cursor=previous.get("cursor", first.isoformat()),
        )
        if instant(current["cursor"]) >= last:
            current["phase"] = HistoryPhase.COMPLETE
    elif start is not None or end is not None:
        raise ValueError("Only processing accepts history bounds")
    settings["history_import"] = current
    service.commit(data)


def request(settings):
    """Project the next bounded core range from durable progress. No history is queried by the app."""
    progress = settings.get("history_import", {})
    if progress.get("phase") != HistoryPhase.PROCESSING:
        return None
    start, end = instant(progress["cursor"]), instant(progress["end"])
    core_end = min(start + timedelta(days=HISTORY_BATCH_DAYS), end)
    return {
        "start": start.isoformat(),
        "end": core_end.isoformat(),
        "read_start": (start - timedelta(hours=HISTORY_OVERLAP_HOURS)).isoformat(),
        "read_end": (core_end + timedelta(hours=HISTORY_LOOKAHEAD_HOURS)).isoformat(),
    }


def batch(service, entity, power_entity, rows, synthetic, start, end, read_start, read_end, now):
    """Admit one chronological hourly batch and advance its checkpoint in the same transaction.

    Candidate cycles retain their full occupied-hour duration and measured kWh,
    but their subhour shape is explicitly estimated. A ready fine profile or
    user manual estimate takes precedence. Automatic provisional planning starts
    only after the initial scan reaches its target, not on ancient first batches.

    The four bounds must echo the current app-owned request. Rows describe
    completed hourly mean W and cumulative end-of-hour kWh. Overlap supports
    detection, while only core hours contribute coverage. The application lock
    serializes callers and the SQL transaction joins archives with the cursor.

    Raises:
        ValueError: Source ownership, requested bounds or samples are invalid.
        OSError: The batch transaction fails without advancing live state.
    """
    member(service, entity, power_entity)
    if type(synthetic) is not bool:
        raise ValueError("Invalid history provenance")
    first, last = instant(start), instant(end)
    settings = service.state["devices"].get(entity, {})
    progress = settings.get("history_import", {})
    if not progress or progress.get("power_entity") != power_entity or not first < last:
        raise ValueError("History scan must be initialized for this source")
    if progress.get("cursor") and last <= instant(progress["cursor"]):
        return
    expected = request(settings)
    if expected != {"start": start, "end": end, "read_start": read_start, "read_end": read_end}:
        raise ValueError("History progress changed. Refresh before continuing")
    query_start, query_end = instant(read_start), min(instant(read_end), now)
    cycles = detect_cycles(rows, now, sample_seconds=HOURLY_SAMPLE_SECONDS)
    if any(not query_start <= instant(row["at"]) < query_end for row in rows):
        raise ValueError("Hourly samples exceed the requested batch")
    data, extra, accepted = deepcopy(service.state), [], 0
    target = data["devices"][entity]
    previous_profile = target.get("hourly_profile")
    weight = target.get("hourly_weight", 0.0)
    for started, completed, points in cycles:
        if not first < completed <= last:
            continue
        if target.get("hourly_last_end") and started < instant(target["hourly_last_end"]):
            continue
        identity = digest([entity, power_entity, started.isoformat(), completed.isoformat()])
        key = "appliance-hourly-" + identity + ".json"
        if service.documents.exists(key):
            continue
        try:
            learned = hourly_profile(points, started, completed)
        except ValueError:
            continue
        if previous_profile:
            weight = retained_weight(weight, instant(target["hourly_last_end"]), completed)
        prior = (
            CycleProfile(
                tuple(previous_profile["power_w"]),
                previous_profile["samples"],
                previous_profile["evidence"],
            )
            if previous_profile
            else None
        )
        learned = average_profiles(prior, learned, previous_weight=weight)
        previous_profile = asdict(learned)
        weight += 1
        target.update(
            hourly_profile=previous_profile,
            hourly_weight=weight,
            hourly_last_end=completed.isoformat(),
            hourly_synthetic=target.get("hourly_synthetic", False) or synthetic,
        )
        accepted += 1
        target.setdefault("hourly_first_start", started.isoformat())
        extra.append(
            (
                key,
                {
                    "id": identity,
                    "entity_id": entity,
                    "power_entity": power_entity,
                    "started_at": started.isoformat(),
                    "completed_at": completed.isoformat(),
                    "points": points,
                    "synthetic": synthetic,
                    "quality": "hourly_estimate",
                },
            )
        )
    updated = target["history_import"]
    updated["cursor"] = last.isoformat()
    evidence = history_evidence(
        rows, accepted, sample_seconds=HOURLY_SAMPLE_SECONDS, start=first, end=last
    )
    updated["last_batch"] = evidence
    for field in ("expected_intervals", "usable_intervals", "detected_cycles"):
        updated[field] = updated.get(field, 0) + evidence[field]
    if last == instant(updated["end"]):
        updated["phase"] = HistoryPhase.COMPLETE
        if (
            previous_profile
            and previous_profile["samples"] >= MIN_COMPLETE_CYCLES
            and target.get("learned_profile", {}).get("samples", 0) < MIN_COMPLETE_CYCLES
            and not target.get("manual_profile")
            and target.get("profile", {}).get("evidence") in (None, "hourly_estimate")
        ):
            target["profile"] = previous_profile
    service.commit(data, extra=tuple(extra))
