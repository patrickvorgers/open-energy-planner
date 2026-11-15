"""Resolve session-local vehicle candidates without operating equipment or guessing a tie."""

from collections import Counter
from datetime import timedelta
from enum import StrEnum

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.ev.requests import active_request

STATUS_MAX_AGE_SECONDS = 300
POWER_MARGIN_W = 200
POWER_MARGIN_FRACTION = 0.1


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
        candidates, positive = [], []
        for vehicle_id in point["vehicle_ids"]:
            vehicle = cars[vehicle_id]
            reading = readings[vehicle_id]
            plugged = current_status(reading, "connected", now)
            charging = current_status(reading, "charging", now)
            home = current_status(reading, "home", now)
            maximum = vehicle["maximum_w"]
            excess = max(POWER_MARGIN_W, maximum * POWER_MARGIN_FRACTION)
            if plugged is False or home is False or (actual["power_w"] or 0) > maximum + excess:
                continue
            candidates.append(vehicle_id)
            if plugged is True or charging is True:
                positive.append(vehicle_id)
        previous = state["assignments"].get(point["id"], {})
        retained = (
            previous.get("vehicle_id")
            if previous.get("session_id") == actual["session_id"]
            and previous.get("vehicle_id") in candidates
            else None
        )
        planned = [
            vehicle_id
            for vehicle_id in candidates
            if (request := active_request(state, vehicle_id))
            and instant(request["departure"]) > now
        ]
        vehicle_id = None
        reason = RecognitionReason.AMBIGUOUS if candidates else RecognitionReason.CONFLICT
        if len(positive) == 1:
            vehicle_id, reason = positive[0], RecognitionReason.SIGNAL
        elif retained and not positive:
            vehicle_id, reason = retained, previous.get("origin", RecognitionReason.MANUAL)
        elif len(candidates) == 1:
            vehicle_id = candidates[0]
            reason = (
                RecognitionReason.SINGLE
                if len(point["vehicle_ids"]) == 1
                else RecognitionReason.ELIMINATION
            )
        elif not positive and len(planned) == 1:
            vehicle_id, reason = planned[0], RecognitionReason.PLANNED
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
