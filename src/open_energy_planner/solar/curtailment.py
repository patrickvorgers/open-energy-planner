"""Retain observed on/off feedback separately from production and planning permission."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cached_property

from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.storage import SERIES_ARCHIVE_READ_BATCH_BLOCKS

MAX_FEEDBACK_READINGS = 10000
MAX_FEEDBACK_RANGE_DAYS = 367
FEEDBACK_REFRESH_MINUTES = 5
FEEDBACK_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FeedbackRequestError(ValueError):
    """Reject invalid feedback before any durable write or response projection begins."""


@dataclass(frozen=True, eq=False)
class CurtailmentHistory:
    """Actual state changes and the last successfully checked Recorder boundary.

    True means curtailed. None is an unknown observation. Before the first valid
    observation production is assumed unrestricted by the configured policy.
    That assumption never fills unknown observations after feedback has begun.
    """

    entity_id: str
    checked_until: datetime
    observations: tuple[tuple[datetime, bool | None], ...]
    parent: CurtailmentHistory | None = field(default=None, repr=False)
    archived: bool = field(default=False, repr=False)

    def __eq__(self, other):
        """Compare original values independently of their immutable storage chunk boundaries."""
        if not isinstance(other, CurtailmentHistory):
            return NotImplemented
        return (self.entity_id, self.checked_until, self.readings) == (
            other.entity_id,
            other.checked_until,
            other.readings,
        )

    @cached_property
    def readings(self):
        """Materialize an immutable chronological view only when learning or comparison needs it."""
        chunks, current = [], self
        while current is not None:
            chunks.append(current.observations)
            current = current.parent
        return tuple(row for chunk in reversed(chunks) for row in chunk)

    @cached_property
    def first_valid(self):
        """Retain the original first valid observation independently of later unknown readings."""
        if self.parent is not None and self.parent.first_valid is not None:
            return self.parent.first_valid
        return next((at for at, state in self.observations if state is not None), None)

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

    def segment(self):
        """Encode only this append and its preceding content-addressed segment."""
        return {
            "entity_id": self.entity_id,
            "previous": self.parent.tail if self.parent else None,
            "index": self.count - 1,
            "readings": [[at.isoformat(), state] for at, state in self.observations],
        }

    @cached_property
    def count(self):
        """Locate immutable segments by sequence so restart can read them in bounded SQL batches."""
        return (self.parent.count if self.parent else 0) + bool(self.observations)

    @cached_property
    def tail(self):
        """Identify the original observation chain without hashing previously accepted values again."""
        if not self.observations:
            return self.parent.tail if self.parent else None
        return version(self.segment())

    def receipt(self):
        """Capture an actual checked cursor and exact immutable observation-chain identity."""
        return {
            "entity_id": self.entity_id,
            "checked_until": self.checked_until.isoformat(),
            "tail": self.tail,
            "count": self.count,
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


def load_feedback(documents, entity, now, *, reference=None):
    """Restore source-bound evidence. Corruption remains unavailable rather than unrestricted."""
    if reference is None:
        pointer = "curtailment-current-" + version(entity) + ".json"
        if not documents.exists(pointer):
            return None
        current = documents.read(pointer)
        reference, body = current["reference"], current["receipt"]
    else:
        body = documents.read("curtailment-history-" + reference + ".json")
    if version(body) != reference:
        raise ValueError("Solar curtailment evidence checksum changed")
    if set(body) != {"entity_id", "checked_until", "tail", "count"} or body["entity_id"] != entity:
        raise ValueError("Solar curtailment source changed")
    empty = parse_feedback(
        {"entity_id": entity, "checked_until": body["checked_until"], "readings": []}, now
    )
    count = body["count"]
    if type(count) is not int or count < 0:
        raise ValueError("Solar curtailment segment count changed")
    result, last, tail = None, None, None
    prefix = "curtailment-segment-" + version(entity) + "-"
    for offset in range(0, count, SERIES_ARCHIVE_READ_BATCH_BLOCKS):
        names = [
            prefix + str(index) + ".json"
            for index in range(offset, min(count, offset + SERIES_ARCHIVE_READ_BATCH_BLOCKS))
        ]
        batch = documents.read_many(names)
        for index, name in enumerate(names, start=offset):
            block = batch[name]
            if (
                set(block) != {"entity_id", "previous", "index", "readings"}
                or block["entity_id"] != entity
                or block["previous"] != tail
                or type(block["index"]) is not int
                or block["index"] != index
            ):
                raise ValueError("Solar curtailment observations changed")
            chunk = parse_feedback(
                {
                    "entity_id": entity,
                    "checked_until": body["checked_until"],
                    "readings": block["readings"],
                },
                now,
            )
            if not chunk.observations or last is not None and chunk.observations[0][0] <= last:
                raise ValueError("Solar curtailment history order changed")
            tail = version(block)
            result = CurtailmentHistory(
                entity, empty.checked_until, chunk.observations, result, True
            )
            result.__dict__.update(tail=tail, count=index + 1)
            _ = result.first_valid
            last = chunk.observations[-1][0]
    if tail != body["tail"]:
        raise ValueError("Solar curtailment evidence checksum changed")
    if result is None:
        result = replace(empty, archived=True)
    return result


def archive_feedback(store, history):
    """Retain an immutable receipt only when model evidence references the checked snapshot."""
    with store.transaction():
        unarchived, current = [], history
        while current is not None and not current.archived:
            unarchived.append(current)
            current = current.parent
        for current in reversed(unarchived):
            if current.observations:
                store.archive(
                    "curtailment-segment",
                    version(current.entity_id) + "-" + str(current.count - 1),
                    current.segment(),
                )
        store.archive("curtailment-history", history.reference, history.receipt())
    return history.reference


def save_feedback(store, previous, incoming, start):
    """Append a checked range atomically without moving the first observed state after purge."""
    if start >= incoming.checked_until or previous and start != previous.checked_until:
        raise ValueError("Solar curtailment request changed")
    if (incoming.checked_until - start).total_seconds() > MAX_FEEDBACK_RANGE_DAYS * 24 * 3600:
        raise ValueError("Solar curtailment range exceeds its limit")
    if any(at < start for at, _state in incoming.readings):
        raise ValueError("Solar curtailment response precedes its request")
    if previous and previous.entity_id != incoming.entity_id:
        raise ValueError("Solar curtailment sensor changed")
    if incoming.readings or previous is None:
        result = CurtailmentHistory(
            incoming.entity_id, incoming.checked_until, incoming.readings, previous, True
        )
    else:
        result = replace(previous, checked_until=incoming.checked_until)
        for name in ("tail", "count", "first_valid", "readings", "times"):
            if name in previous.__dict__:
                result.__dict__[name] = previous.__dict__[name]
    _ = result.first_valid
    body = result.receipt()
    reference = version(body)
    with store.transaction():
        pointer = "curtailment-current-" + version(result.entity_id) + ".json"
        expected = (
            {"reference": previous.reference, "receipt": previous.receipt()} if previous else None
        )
        current = store.documents.read(pointer) if store.documents.exists(pointer) else None
        if current != expected:
            raise ValueError("Solar curtailment request changed")
        if incoming.readings:
            store.archive(
                "curtailment-segment",
                version(result.entity_id) + "-" + str(result.count - 1),
                result.segment(),
            )
        store.documents.write(pointer, {"reference": reference, "receipt": body})
    return result
