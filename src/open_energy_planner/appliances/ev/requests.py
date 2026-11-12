"""Own explicit charging requests, opt-in weekday recurrence and retained request history."""

from copy import deepcopy
from datetime import datetime, time, timedelta
from enum import StrEnum
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from open_energy_planner.appliances.cycles.model import instant
from open_energy_planner.appliances.ev.configuration import identity
from open_energy_planner.appliances.ev.model import number
from open_energy_planner.core.units import FULL_SOC_PERCENT
from open_energy_planner.demand.calculation.model import digest

MAX_REQUEST_DAYS = 30
DAYS_PER_WEEK = 7
RECURRENCE_LOOKAHEAD_DAYS = 2 * DAYS_PER_WEEK
REQUEST_HISTORY_PAGE_SIZE = 25


class RequestStatus(StrEnum):
    """Keep durable request outcomes independent of live physical charging status."""

    PLANNED = "planned"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


def departure(settings, now, timezone, weekdays=()):
    """Resolve the next HA-local departure, skipping nonexistent spring-forward times."""
    zone = ZoneInfo(timezone)
    local_day = now.astimezone(zone).date()
    for offset in range(RECURRENCE_LOOKAHEAD_DAYS + 1):
        day = local_day + timedelta(days=offset)
        if weekdays and day.weekday() not in weekdays:
            continue
        local = datetime.combine(day, time.fromisoformat(settings["departure_time"]), zone)
        candidate = local.astimezone(now.tzinfo)
        if candidate > now and candidate.astimezone(zone).replace(tzinfo=None) == local.replace(
            tzinfo=None
        ):
            return candidate
    raise ValueError("No valid charging departure in the next two weeks")


def active_request(state, vehicle_id):
    """Read the single current request without creating a request from defaults or history."""
    request_id = state.get("active_requests", {}).get(vehicle_id)
    request = state.get("requests", {}).get(request_id)
    return request if request and request["status"] == RequestStatus.PLANNED else None


def request_document(request):
    """Bind optimistic writes to the exact saved request, independent of sensor refreshes."""
    if request is None:
        return None
    fields = (
        "id",
        "vehicle_id",
        "target_soc",
        "departure",
        "ready_at",
        "repeat_weekdays",
        "status",
        "created_at",
        "updated_at",
        "previous_request_id",
        "continued_by",
    )
    return {
        **{field: request[field] for field in fields if field in request},
        "version": digest(request),
    }


def save_request(state, body, now, timezone):
    """Create or atomically edit one request. Retried creation never revives a terminal request."""
    allowed = {
        "request_id",
        "vehicle_id",
        "target_soc",
        "departure",
        "repeat_weekdays",
        "expected_version",
    }
    if (
        not isinstance(body, dict)
        or not {"request_id", "vehicle_id"} <= set(body)
        or set(body) - allowed
    ):
        raise ValueError("Invalid charging request fields")
    identity(body["request_id"])
    vehicle = next((car for car in state["vehicles"] if car["id"] == body["vehicle_id"]), None)
    if vehicle is None:
        raise ValueError("Unknown vehicle")
    creation_digest = digest(
        {key: value for key, value in body.items() if key != "expected_version"}
    )
    existing = state.get("requests", {}).get(body["request_id"])
    expected = body.get("expected_version")
    if existing and expected is None:
        if existing["creation_digest"] == creation_digest:
            return state
        raise ValueError("Charging request ID was already used")
    target = number(body.get("target_soc", vehicle["target_soc"]), 0, FULL_SOC_PERCENT)
    deadline = (
        instant(body["departure"]) if body.get("departure") else departure(vehicle, now, timezone)
    )
    if not now < deadline <= now + timedelta(days=MAX_REQUEST_DAYS):
        raise ValueError("Charging departure must be in the next thirty days")
    weekdays = body.get("repeat_weekdays", [])
    if (
        not isinstance(weekdays, list)
        or any(type(day) is not int or not 0 <= day < DAYS_PER_WEEK for day in weekdays)
        or len(set(weekdays)) != len(weekdays)
    ):
        raise ValueError("Choose distinct repeat weekdays between zero and six")
    weekdays = sorted(weekdays)
    if existing:
        if existing["vehicle_id"] != vehicle["id"] or existing["status"] != RequestStatus.PLANNED:
            raise ValueError("Only a planned charging request can be edited")
        if (
            existing["target_soc"] == target
            and instant(existing["departure"]) == deadline
            and existing["repeat_weekdays"] == weekdays
        ):
            return state
        if expected != digest(existing):
            raise ValueError("Charging request changed. Refresh before saving")
    elif expected is not None:
        raise ValueError("Charging request no longer exists")
    elif active_request(state, vehicle["id"]):
        raise ValueError("This vehicle already has a planned charging request")
    updated = deepcopy(state)
    record = {
        "id": body["request_id"],
        "vehicle_id": vehicle["id"],
        "target_soc": target,
        "departure": deadline.isoformat(),
        "ready_at": now.isoformat(),
        "repeat_weekdays": weekdays,
        "repeat_time": deadline.astimezone(ZoneInfo(timezone)).strftime("%H:%M"),
        "timezone": timezone,
        "status": RequestStatus.PLANNED,
        "created_at": existing["created_at"] if existing else now.isoformat(),
        "updated_at": now.isoformat(),
        "creation_digest": existing["creation_digest"] if existing else creation_digest,
    }
    if existing and existing.get("previous_request_id"):
        record["previous_request_id"] = existing["previous_request_id"]
    updated.setdefault("requests", {})[record["id"]] = record
    updated.setdefault("active_requests", {})[vehicle["id"]] = record["id"]
    return updated


def cancel_request(state, request_id, expected_version, now):
    """Cancel the concrete request and its repetition while retaining its history."""
    request = state.get("requests", {}).get(request_id)
    if request is None:
        raise ValueError("Charging request no longer exists")
    if request["status"] == RequestStatus.CANCELLED:
        return state
    if request["status"] != RequestStatus.PLANNED or expected_version != digest(request):
        raise ValueError("Charging request changed. Refresh before cancelling")
    updated = deepcopy(state)
    updated["requests"][request_id].update(
        status=RequestStatus.CANCELLED, updated_at=now.isoformat()
    )
    updated["active_requests"].pop(request["vehicle_id"], None)
    return updated


def advance_requests(state, report, now, timezone=None):
    """Complete only from fresh identified evidence and create at most one next repeat occurrence."""
    if not state.get("active_requests"):
        return state
    updated = None
    readings = {row["id"]: row for row in report["vehicles"]}
    captured_at = instant(report["at"])
    for vehicle_id, request_id in tuple(state.get("active_requests", {}).items()):
        request = state["requests"][request_id]
        deadline = instant(request["departure"])
        assigned = any(
            point["connected"] is True
            and state["assignments"].get(point["id"], {}).get("vehicle_id") == vehicle_id
            and state["assignments"].get(point["id"], {}).get("session_id") == point["session_id"]
            for point in report["points"]
        )
        measured_soc = readings.get(vehicle_id, {}).get("soc")
        completed = (
            assigned
            and measured_soc is not None
            and instant(request["ready_at"]) <= captured_at < deadline
            and measured_soc >= request["target_soc"]
        )
        if not completed and deadline > now:
            continue
        if updated is None:
            updated = deepcopy(state)
        request = updated["requests"][request_id]
        request.update(
            status=RequestStatus.COMPLETED if completed else RequestStatus.EXPIRED,
            updated_at=(captured_at if completed else now).isoformat(),
        )
        updated["active_requests"].pop(vehicle_id, None)
        if request["repeat_weekdays"]:
            zone = timezone or request["timezone"]
            ready_at = max(now, deadline)
            following = departure(
                {"departure_time": request["repeat_time"]},
                ready_at,
                zone,
                request["repeat_weekdays"],
            )
            following_id = str(uuid5(UUID(request_id), following.isoformat()))
            next_request = {
                **{
                    key: request[key]
                    for key in (
                        "vehicle_id",
                        "target_soc",
                        "repeat_weekdays",
                        "repeat_time",
                        "creation_digest",
                    )
                },
                "id": following_id,
                "departure": following.isoformat(),
                "ready_at": ready_at.isoformat(),
                "timezone": zone,
                "status": RequestStatus.PLANNED,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "previous_request_id": request_id,
            }
            request["continued_by"] = following_id
            updated["requests"][following_id] = next_request
            updated["active_requests"][vehicle_id] = following_id
    return updated if updated is not None else state
