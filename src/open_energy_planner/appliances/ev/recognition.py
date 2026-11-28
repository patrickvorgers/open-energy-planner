"""Resolve session-local vehicle candidates without operating equipment or guessing a tie."""

from collections import Counter
from copy import deepcopy
from datetime import timedelta
from enum import StrEnum

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.ev.requests import active_request

STATUS_MAX_AGE_SECONDS = 300
POWER_MARGIN_W = 200
POWER_MARGIN_FRACTION = 0.1


def recognition_evidence(state, report, point_id):
    """Freeze bounded signal provenance for every allowed car, including excluded candidates.

    Configuration changes clear assignments and accepted reports share that
    configuration version. Copy only the boolean status contract and source IDs,
    never provider attributes or location details.
    """
    point = next(row for row in state["points"] if row["id"] == point_id)
    cars = {row["id"]: row for row in state["vehicles"]}
    readings = {row["id"]: row for row in report["vehicles"]}
    return {
        **deepcopy(state["assignments"][point_id]),
        "captured_at": report["at"],
        "inputs": [
            {
                "vehicle_id": vehicle_id,
                **{
                    field: {
                        "source_entity": cars[vehicle_id].get(source),
                        "observation": deepcopy(readings[vehicle_id].get(field)),
                    }
                    for field, source in (
                        ("connected", "connected_entity"),
                        ("charging", "charging_entity"),
                        ("home", "location_entity"),
                    )
                },
            }
            for vehicle_id in point["vehicle_ids"]
        ],
    }


class RecognitionReason(StrEnum):
    """Describe the evidence behind an assignment without claiming physical identity proof."""

    MANUAL = "manual"
    RETAINED = "retained_session"
    SINGLE = "only_allowed_vehicle"
    SIGNAL = "vehicle_status"
    ELIMINATION = "remaining_candidate"
    PLANNED = "planned_expectation"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflicting_evidence"


def current_status(row, field, now):
    """Use bounded original sample times, never revive an old status through a new capture."""
    status = row.get(field)
    if not isinstance(status, dict) or type(status.get("value")) is not bool:
        return None
    observed = instant(status["observed_at"])
    if not now - timedelta(seconds=STATUS_MAX_AGE_SECONDS) <= observed <= now:
        return None
    return status["value"]


def _candidate_signals(vehicle_ids, cars, readings, point_reading, now):
    """Keep possible cars and the subset supported by current plug or charging evidence."""
    candidates, positive = [], []
    for vehicle_id in vehicle_ids:
        vehicle = cars[vehicle_id]
        reading = readings[vehicle_id]
        plugged = current_status(reading, "connected", now)
        charging = current_status(reading, "charging", now)
        home = current_status(reading, "home", now)
        maximum = vehicle["maximum_w"]
        excess = max(POWER_MARGIN_W, maximum * POWER_MARGIN_FRACTION)
        if plugged is False or home is False or (point_reading["power_w"] or 0) > maximum + excess:
            continue
        candidates.append(vehicle_id)
        if plugged is True or charging is True:
            positive.append(vehicle_id)
    return candidates, positive


def _session_owner(state, point, reading, candidates, positive, now):
    """Prefer a positive signal, then compatible session continuity and planned intent."""
    previous = state["assignments"].get(point["id"], {})
    retained = (
        previous.get("vehicle_id")
        if previous.get("session_id") == reading["session_id"]
        and previous.get("vehicle_id") in candidates
        else None
    )
    planned = [
        vehicle_id
        for vehicle_id in candidates
        if (request := active_request(state, vehicle_id)) and instant(request["departure"]) > now
    ]
    if len(positive) == 1:
        return positive[0], RecognitionReason.SIGNAL
    if retained and not positive:
        return retained, previous.get("origin", RecognitionReason.MANUAL)
    if len(candidates) == 1:
        reason = (
            RecognitionReason.SINGLE
            if len(point["vehicle_ids"]) == 1
            else RecognitionReason.ELIMINATION
        )
        return candidates[0], reason
    if not positive and len(planned) == 1:
        return planned[0], RecognitionReason.PLANNED
    return None, RecognitionReason.AMBIGUOUS if candidates else RecognitionReason.CONFLICT


def recognize(state, report, now):
    """Prefer positive evidence over planned expectation and reject duplicate concurrent ownership.

    Saved assignments remain bound to their exact physical connection nonce.
    Missing status does not contradict a retained assignment. A current negative
    status does. Power only excludes impossible candidates, never identifies a
    lower-power car merely because the charger happens to draw little power.
    """
    cars = {row["id"]: row for row in state["vehicles"]}
    readings = {row["id"]: row for row in report["vehicles"]}
    points = {row["id"]: row for row in report["points"]}
    recognized = {}
    for point in state["points"]:
        actual = points[point["id"]]
        if actual["connected"] is not True:
            continue
        candidates, positive = _candidate_signals(point["vehicle_ids"], cars, readings, actual, now)
        vehicle_id, reason = _session_owner(state, point, actual, candidates, positive, now)
        recognized[point["id"]] = {
            "session_id": actual["session_id"],
            "vehicle_id": vehicle_id,
            "origin": reason,
            "candidates": candidates,
        }
    owners = Counter(row["vehicle_id"] for row in recognized.values() if row["vehicle_id"])
    for row in recognized.values():
        if row["vehicle_id"] and owners[row["vehicle_id"]] > 1:
            row.update(vehicle_id=None, origin=RecognitionReason.AMBIGUOUS)
    return recognized
