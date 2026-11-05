"""Publish authoritative snapshots and bounded replay without a snapshot/subscription gap."""

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager, contextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from ..storage.writes import complete_write

_LOGGER = logging.getLogger(__name__)


class Outcome(StrEnum):
    """Execution outcomes are independent of notification severity and module health."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class Health(StrEnum):
    """Producers decide usability using their existing domain validity rules."""

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class Events:
    """Event-loop-owned live state. Subscribers receive detached data and never execute producers."""

    def __init__(self, store, clock=None):
        """Borrow the active activity store and an aware clock."""
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.epoch = uuid4().hex
        self.sequence = 0
        self.replay = deque(maxlen=512)
        self.changed = asyncio.Event()
        self.running = {}
        self.health = {}
        self.schedules = {}
        self.notifications = {}
        self.failed = set()
        self.history_available = True
        self.loop = None
        self.pending = asyncio.Queue(maxsize=512)
        self.writer = None
        self.lost_updates = 0

    @contextmanager
    def thread_attempt(self, owner, kind, title, *, failure):
        """Let an owned numerical worker publish through the event loop without sharing SQL connections."""
        context = self.attempt(owner, kind, title, failure=failure)
        identity = asyncio.run_coroutine_threadsafe(context.__aenter__(), self.loop).result()
        try:
            yield identity
        except BaseException as error:
            asyncio.run_coroutine_threadsafe(
                context.__aexit__(type(error), error, error.__traceback__), self.loop
            ).result()
            raise
        else:
            asyncio.run_coroutine_threadsafe(
                context.__aexit__(None, None, None), self.loop
            ).result()

    def finish_thread(self, identity, outcome, detail, *, usable=False):
        """Complete a worker-owned execution on the same serialized event loop."""
        asyncio.run_coroutine_threadsafe(
            self.finish(identity, outcome, detail, usable=usable), self.loop
        ).result()

    def availability_thread(self, owner, state, detail):
        """A numerical producer publishes its own usability decision on the owning event loop."""
        self.loop.call_soon_threadsafe(self.availability, owner, state, detail)

    @property
    def cursor(self):
        """A process epoch makes a restart an explicit replay reset."""
        return f"{self.epoch}:{self.sequence}"

    def publish(self, topic, value):
        """Copy source-owned safe data before exposing it to any subscriber."""
        self.sequence += 1
        self.replay.append({"sequence": self.sequence, "topic": topic, "value": deepcopy(value)})
        self.changed.set()
        self.changed = asyncio.Event()

    def availability(self, owner, state, detail):
        """Availability is a domain statement, never inferred from an activity outcome."""
        row = {"owner": owner, "state": Health(state).value, "detail": detail}
        if self.health.get(owner) != row:
            self.health[owner] = row
            self.publish("health", row)

    def scheduled(self, owner, kind, title, due=None):
        """Replace or cancel the owner's next known execution. No guessed ad-hoc work."""
        key = f"{owner}:{kind}"
        if due is not None and due.utcoffset() is None:
            raise ValueError("Scheduled activity requires a timezone")
        row = (
            {
                "id": key,
                "owner": owner,
                "kind": kind,
                "title": title,
                "due_at": due.astimezone(UTC).isoformat(),
            }
            if due
            else None
        )
        if self.schedules.get(key) != row:
            if row is None:
                self.schedules.pop(key, None)
            else:
                self.schedules[key] = row
            self.publish("schedule", {"id": key, "activity": row})

    def notices(self, document):
        """Relay business-owned notices independently of execution failures and recovery."""
        if self.notifications != document:
            self.notifications = deepcopy(document)
            self.publish("notifications", document)

    def withdraw(self, owner):
        """A producer that is disabled or replaced explicitly retracts its live state and schedule."""
        for row in tuple(self.schedules.values()):
            if row["owner"] == owner:
                self.scheduled(owner, row["kind"], row["title"])
        if self.health.pop(owner, None) is not None:
            self.publish("health", {"owner": owner, "removed": True})

    async def _save(self, row):
        """Queue detached evidence without holding up a solver, provider request or domain lock."""
        if self.writer is None:
            self.writer = asyncio.create_task(self._write_pending())
        try:
            self.pending.put_nowait((deepcopy(row), self.clock()))
        except asyncio.QueueFull:
            if row is None:
                return
            self.lost_updates += 1
            self.history_available = False
            self.publish("history", {"available": False, "lost_updates": self.lost_updates})

    async def _write_pending(self):
        """One writer survives individual failures. Shutdown drains before the engine closes."""
        while True:
            row, now = await self.pending.get()
            previous = self.history_available, self.lost_updates
            try:
                cancellation = (
                    await complete_write(self.store.prune, now)
                    if row is None
                    else await complete_write(self.store.write, row, now)
                )
                self.history_available = True
            except Exception:
                _LOGGER.exception("Activity history unavailable. Domain work is unaffected")
                self.history_available = False
                self.lost_updates += int(row is not None)
                cancellation = None
            finally:
                self.pending.task_done()
            if previous != (self.history_available, self.lost_updates):
                self.publish(
                    "history",
                    {"available": self.history_available, "lost_updates": self.lost_updates},
                )
            if cancellation:
                raise cancellation

    async def flush(self):
        """Await captured evidence only where durability is required, never in the calculation path."""
        await self.pending.join()

    async def prune(self):
        """Serialize retention with the same writer as all activity updates."""
        await self._save(None)

    async def close(self):
        """Stop after producers and drain the optional writer before the shared engine closes."""
        await self.flush()
        if self.writer:
            self.writer.cancel()
            with suppress(asyncio.CancelledError):
                await self.writer

    async def start(self, owner, kind, title, *, detail=""):
        """Allocate one ID for an actual attempt, before the underlying work starts."""
        if owner not in self.health:
            self.availability(owner, Health.UNKNOWN, "Waiting for current module evidence.")
        row = {
            "id": uuid4().hex,
            "owner": owner,
            "kind": kind,
            "title": title,
            "started_at": self.clock().astimezone(UTC).isoformat(),
            "finished_at": None,
            "state": "running",
            "detail": detail,
            "recovered": False,
        }
        self.running[row["id"]] = row
        self.publish("activity", row)
        await self._save(row)
        return row["id"]

    @asynccontextmanager
    async def attempt(self, owner, kind, title, *, failure):
        """Contain execution bookkeeping while the producer owns outcomes and safe failure text."""
        identity = await self.start(owner, kind, title)
        try:
            yield identity
        except asyncio.CancelledError:
            if identity in self.running:
                await self.finish(
                    identity,
                    Outcome.INTERRUPTED,
                    "Stopped before a definitive outcome was recorded.",
                )
            raise
        except Exception:
            if identity in self.running:
                await self.finish(identity, Outcome.FAILED, failure)
            raise
        finally:
            if identity in self.running:
                await self.finish(
                    identity, Outcome.INTERRUPTED, "No definitive outcome was recorded."
                )

    async def update(self, identity, detail):
        """Publish changed progress under the same execution without creating another attempt."""
        row = self.running[identity]
        if row["detail"] != detail:
            row = {**row, "detail": detail}
            self.running[identity] = row
            self.publish("activity", row)
            await self._save(row)

    async def finish(self, identity, outcome, detail, *, usable=False):
        """Only usable success ends a failure episode. Repeated failures remain real attempts."""
        outcome = Outcome(outcome)
        if outcome == Outcome.COMPLETED and not usable:
            raise ValueError("Completed activity requires a usable result")
        row = deepcopy(self.running[identity])
        key = (row["owner"], row["kind"])
        row.update(
            state=outcome.value,
            finished_at=self.clock().astimezone(UTC).isoformat(),
            detail=detail,
            usable=usable,
            finished_sequence=self.sequence + 1,
        )
        if outcome == Outcome.FAILED:
            self.failed.add(key)
        elif outcome == Outcome.COMPLETED and usable:
            row["recovered"] = key in self.failed
            self.failed.discard(key)
        await self._save(row)
        self.running.pop(identity, None)
        self.publish("activity", row)

    def snapshot(self):
        """Publish current state and upcoming six hours, never expired schedule entries."""
        now = self.clock()
        return deepcopy(
            {
                "running": list(self.running.values()),
                "health": list(self.health.values()),
                "scheduled": [
                    row
                    for row in self.schedules.values()
                    if now <= datetime.fromisoformat(row["due_at"]) <= now + timedelta(hours=6)
                ],
                "notifications": self.notifications,
                "history_available": self.history_available,
                "lost_updates": self.lost_updates,
            }
        )

    def changes(self, cursor=None):
        """Snapshot/cursor capture contains no await, making the subscription boundary atomic."""
        try:
            epoch, number = cursor.split(":")
            sequence = int(number)
            valid = (
                epoch == self.epoch
                and 0 <= sequence <= self.sequence
                and sequence >= self.sequence - len(self.replay)
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            return {"cursor": self.cursor, "reset": True, "snapshot": self.snapshot(), "events": []}
        return {
            "cursor": self.cursor,
            "reset": False,
            "events": deepcopy([row for row in self.replay if row["sequence"] > sequence]),
        }

    async def subscribe(self, cursor=None, timeout=25):
        """Long-poll uses the captured wakeup before checking state, so no event can be lost."""
        wakeup = self.changed
        result = self.changes(cursor)
        if result["reset"] or result["events"]:
            return result
        try:
            await asyncio.wait_for(wakeup.wait(), timeout)
        except TimeoutError:
            pass
        return self.changes(cursor)
