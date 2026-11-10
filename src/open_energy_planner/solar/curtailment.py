"""Retain observed on/off feedback separately from production and planning permission."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property

from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import version

MAX_FEEDBACK_READINGS = 10000
MAX_FEEDBACK_RANGE_DAYS = 367
FEEDBACK_REFRESH_MINUTES = 5
FEEDBACK_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FeedbackRequestError(ValueError):
    """Reject invalid feedback before any durable write or response projection begins."""


@dataclass(frozen=True)
class CurtailmentHistory:
    """Actual state changes and the last successfully checked Recorder boundary.

    True means curtailed. None is an unknown observation. Before the first valid
    observation production is assumed unrestricted by the configured policy.
    That assumption never fills unknown observations after feedback has begun.
    """

    entity_id: str
    checked_until: datetime
    readings: tuple[tuple[datetime, bool | None], ...]

    @cached_property
    def first_valid(self):
        """Retain the original first valid observation independently of later unknown readings."""
        return next((at for at, state in self.readings if state is not None), None)

    @cached_property
    def times(self):
        """Index transitions once rather than scanning all feedback for each training hour."""
        return tuple(at for at, _state in self.readings)

    def unrestricted(self, start, end):
        """Require every part of a complete measured interval to be unrestricted."""
        if not start < end <= self.checked_until:
            return False
        first = self.first_valid
        if first is None or end <= first:
            return True
        index = bisect_right(self.times, start)
        state = self.readings[index - 1][1] if index and start >= first else False
        for reading_index in range(index, len(self.readings)):
            at, value = self.readings[reading_index]
            if at >= end:
                break
            if at < first:
                continue
            if at > start and state is not False:
                return False
            state = value
        return state is False

    def receipt(self):
        """Reference deduplicated daily observation blocks from a small immutable receipt."""
        rows = [{"start": at.isoformat(), "curtailed": state} for at, state in self.readings]
        return {
            "entity_id": self.entity_id,
            "checked_until": self.checked_until.isoformat(),
            "readings_reference": version({"entity_id": self.entity_id, "readings": rows}),
        }

    @property
    def reference(self):
        """Bind training provenance to the same receipt published by the feedback writer."""
        return version(self.receipt())


def parse_feedback(body, now, *, maximum=MAX_FEEDBACK_READINGS):
    """Validate bounded, strictly ordered feedback and reject future observations."""
    if not isinstance(body, dict) or set(body) != {"entity_id", "checked_until", "readings"}:
        raise ValueError("Invalid solar curtailment feedback")
    entity = body["entity_id"]
    if not isinstance(entity, str) or not entity.startswith(("binary_sensor.", "sensor.")):
        raise ValueError("Invalid solar curtailment sensor")
    until = aware(datetime.fromisoformat(body["checked_until"]))
    if not FEEDBACK_EPOCH <= until <= now:
        raise ValueError("Invalid solar curtailment boundary")
    rows = body["readings"]
    if not isinstance(rows, list) or maximum is not None and len(rows) > maximum:
        raise ValueError("Invalid solar curtailment history size")
    readings = []
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or row[1] is not None
            and type(row[1]) is not bool
        ):
            raise ValueError("Invalid solar curtailment observation")
        at = aware(datetime.fromisoformat(row[0]))
        if not FEEDBACK_EPOCH <= at < until or readings and at <= readings[-1][0]:
            raise ValueError("Invalid solar curtailment observation time")
        readings.append((at, row[1]))
    return CurtailmentHistory(entity, until, tuple(readings))


def load_feedback(documents, entity, now):
    """Restore source-bound evidence. Corruption remains unavailable rather than unrestricted."""
    pointer = "curtailment-current-" + version(entity) + ".json"
    if not documents.exists(pointer):
        return None
    reference = documents.read(pointer)
    body = documents.read("curtailment-history-" + reference + ".json")
    if version(body) != reference:
        raise ValueError("Solar curtailment evidence checksum changed")
    manifest = documents.read("curtailment-values-" + body["readings_reference"] + ".json")
    if manifest["entity_id"] != entity:
        raise ValueError("Solar curtailment source changed")
    rows = []
    for entry in manifest["blocks"]:
        block = documents.read("block-" + entry["block"] + ".json")
        if version(block) != entry["block"] or block["entity_id"] != entity:
            raise ValueError("Solar curtailment observations changed")
        rows.extend(block["hours"])
    if version({"entity_id": entity, "readings": rows}) != body["readings_reference"]:
        raise ValueError("Solar curtailment history changed")
    result = parse_feedback(
        {
            "entity_id": entity,
            "checked_until": body["checked_until"],
            "readings": [[row["start"], row["curtailed"]] for row in rows],
        },
        now,
        maximum=None,
    )
    if result.entity_id != entity:
        raise ValueError("Solar curtailment sensor changed")
    return result


def save_feedback(store, previous, incoming, start):
    """Append a checked range atomically without moving the first observed state after purge."""
    if start >= incoming.checked_until or previous and start != previous.checked_until:
        raise ValueError("Solar curtailment request changed")
    if (incoming.checked_until - start).total_seconds() > MAX_FEEDBACK_RANGE_DAYS * 24 * 3600:
        raise ValueError("Solar curtailment range exceeds its limit")
    if any(at < start for at, _state in incoming.readings):
        raise ValueError("Solar curtailment response precedes its request")
    retained = previous.readings if previous else ()
    if previous and previous.entity_id != incoming.entity_id:
        raise ValueError("Solar curtailment sensor changed")
    result = CurtailmentHistory(
        incoming.entity_id, incoming.checked_until, retained + incoming.readings
    )
    rows = [{"start": at.isoformat(), "curtailed": state} for at, state in result.readings]
    values_reference = version({"entity_id": result.entity_id, "readings": rows})
    body = result.receipt()
    reference = version(body)
    with store.transaction():
        store.archive_series("curtailment-values", values_reference, rows, entity=result.entity_id)
        store.archive("curtailment-history", reference, body)
        store.documents.write(
            "curtailment-current-" + version(result.entity_id) + ".json", reference
        )
    return result
