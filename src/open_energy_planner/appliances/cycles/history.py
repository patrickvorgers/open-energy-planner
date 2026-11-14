"""Source-bound hourly and fine backfill checkpoints with atomic evidence refinement."""

from copy import deepcopy
from datetime import timedelta
from enum import StrEnum

from .learning import (
    HOURLY_SAMPLE_SECONDS,
    SAMPLE_SECONDS,
    detect_cycles,
    history_evidence,
)
from .model import MAX_CYCLE_HOURS, instant

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


def scan(service, entity, power_entity, phase, start, end, now, *, resolution="hour"):
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
    field, sample_seconds = scan_settings(resolution)
    phase = HistoryPhase(phase)
    if phase == HistoryPhase.COMPLETE:
        raise ValueError("Invalid history scan phase")
    service.sync_sources()
    data = deepcopy(service.state)
    settings = data["devices"].setdefault(entity, {"shiftable": False})
    previous = settings.get(field, {})
    current = {**previous, "phase": phase, "power_entity": power_entity}
    if phase == HistoryPhase.PROCESSING:
        first, last = instant(start), instant(end)
        if (
            first.timestamp() < 0
            or not first < last <= now
            or any(value.timestamp() % sample_seconds for value in (first, last))
        ):
            raise ValueError("Invalid hourly history bounds")
        if previous.get("start") and previous["start"] != first.isoformat():
            raise ValueError("History source range changed. Retained learning is unchanged")
        if previous.get("end") and last < instant(previous["end"]):
            raise ValueError("History target cannot move backwards")
        current.update(
            start=first.isoformat(),
            end=last.isoformat(),
            cursor=previous.get("cursor", (last if resolution == "5minute" else first).isoformat()),
        )
        if resolution == "5minute":
            initial_end = previous.get("end", last.isoformat())
            current.update(
                backfill_end=previous.get("backfill_end", initial_end),
                forward_cursor=previous.get("forward_cursor", initial_end),
            )
        if (
            instant(current["cursor"]) <= first and instant(current["forward_cursor"]) >= last
            if resolution == "5minute"
            else instant(current["cursor"]) >= last
        ):
            current["phase"] = HistoryPhase.COMPLETE
    elif start is not None or end is not None:
        raise ValueError("Only processing accepts history bounds")
    settings[field] = current
    service.commit(data)


def scan_settings(resolution):
    """Select the explicitly supported progress contract and sampling grid."""
    if resolution not in ("hour", "5minute"):
        raise ValueError("Unsupported cycle history resolution")
    return (
        ("history_import", HOURLY_SAMPLE_SECONDS)
        if resolution == "hour"
        else ("fine_import", SAMPLE_SECONDS)
    )


def request(settings, *, resolution="hour"):
    """Project the next bounded core range from durable progress. No history is queried by the app."""
    field, _ = scan_settings(resolution)
    progress = settings.get(field, {})
    if progress.get("phase") != HistoryPhase.PROCESSING:
        return None
    if resolution == "5minute":
        forward = instant(progress.get("forward_cursor", progress["end"]))
        if forward < instant(progress["end"]):
            start = forward
            core_end = min(start + timedelta(days=HISTORY_BATCH_DAYS), instant(progress["end"]))
        else:
            end = instant(progress["cursor"])
            start = max(instant(progress["start"]), end - timedelta(days=HISTORY_BATCH_DAYS))
            core_end = end
    else:
        start, end = instant(progress["cursor"]), instant(progress["end"])
        core_end = min(start + timedelta(days=HISTORY_BATCH_DAYS), end)
    return {
        "start": start.isoformat(),
        "end": core_end.isoformat(),
        "read_start": (start - timedelta(hours=HISTORY_OVERLAP_HOURS)).isoformat(),
        "read_end": (core_end + timedelta(hours=HISTORY_LOOKAHEAD_HOURS)).isoformat(),
    }


def batch(
    service,
    entity,
    power_entity,
    rows,
    synthetic,
    start,
    end,
    read_start,
    read_end,
    now,
    *,
    resolution="hour",
):
    """Admit one requested hourly or fine batch and advance its checkpoint atomically.

    Candidate cycles retain their full occupied-hour duration and measured kWh,
    but their subhour shape is explicitly estimated. The first complete cycle
    is usable immediately. Fine evidence refines timing without double counting.
    User estimates and accepted task profiles retain their explicit ownership.

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
    field, sample_seconds = scan_settings(resolution)
    first, last = instant(start), instant(end)
    settings = service.state["devices"].get(entity, {})
    progress = settings.get(field, {})
    if not progress or progress.get("power_entity") != power_entity or not first < last:
        raise ValueError("History scan must be initialized for this source")
    if resolution == "5minute":
        boundary = instant(progress.get("backfill_end", progress["end"]))
        backward_examined = instant(progress["cursor"]) <= first < last <= boundary
        forward_examined = (
            boundary <= first < last <= instant(progress.get("forward_cursor", progress["end"]))
        )
        already_examined = backward_examined or forward_examined
    else:
        already_examined = last <= instant(progress["cursor"])
    if already_examined:
        return
    expected = request(settings, resolution=resolution)
    if expected != {"start": start, "end": end, "read_start": read_start, "read_end": read_end}:
        raise ValueError("History progress changed. Refresh before continuing")
    query_start, query_end = instant(read_start), min(instant(read_end), now)
    cycles = detect_cycles(rows, now, sample_seconds=sample_seconds)
    if any(not query_start <= instant(row["at"]) < query_end for row in rows):
        raise ValueError("Hourly samples exceed the requested batch")
    from .evidence import refine

    records, extra, changed = service.collect_cycles(
        entity,
        power_entity,
        [
            (started, completed, points)
            for started, completed, points in cycles
            if first < completed <= last
        ],
        synthetic,
        hourly=resolution == "hour",
    )
    data = deepcopy(service.state)
    target = data["devices"][entity]
    if changed:
        refine(target, list(records.values()))
    if records:
        target["learning_source"] = power_entity
    accepted = len(extra)
    updated = target[field]
    if resolution == "5minute" and first >= boundary:
        updated["forward_cursor"] = last.isoformat()
    else:
        updated["cursor"] = (first if resolution == "5minute" else last).isoformat()
    evidence = history_evidence(
        rows, accepted, sample_seconds=sample_seconds, start=first, end=last
    )
    updated["last_batch"] = evidence
    for field in ("expected_intervals", "usable_intervals", "detected_cycles"):
        updated[field] = updated.get(field, 0) + evidence[field]
    if (
        instant(updated["cursor"]) == instant(updated["start"])
        and instant(updated.get("forward_cursor", updated["end"])) == instant(updated["end"])
        if resolution == "5minute"
        else last == instant(updated["end"])
    ):
        updated["phase"] = HistoryPhase.COMPLETE
    service.commit(data, extra=tuple(extra))
    service._cycle_records[(entity, power_entity)] = records
