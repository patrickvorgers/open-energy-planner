"""Retain completed planning observations by installation-local day without rerunning plans."""

import asyncio
import logging
import re
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiohttp import web

MAX_PENDING_HISTORY_RECORDS = 32
_LOGGER = logging.getLogger(__name__)


class CalculationHistory:
    """Use one existing planning document per UTC start day, with direct reads for local days.

    Only the serialized history writer writes. Each day document is atomic.
    UTC partitioning preserves day lookup when the installation timezone changes.
    """

    def __init__(self, documents):
        """Borrow the app-owned active document store without retaining solver or HA objects."""
        self.documents = documents

    def record(self, snapshot, outcome, started, finished, reason):
        """Synchronously append an observation for fixtures and explicit offline use."""
        self.write(self.capture(snapshot, outcome, started, finished, reason))

    def capture(self, snapshot, outcome, started, finished, reason):
        """Detach bounded observation data without retaining a snapshot, plan or native solver."""
        result = outcome.result if outcome else None
        document = outcome.document if outcome else None
        worker_offset = (
            max(0, (result.started_at - started).total_seconds())
            if result and result.started_at
            else 0
        )
        row = {
            "id": uuid4().hex,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": max(0, (finished - started).total_seconds()),
            "time_limit_seconds": result.time_limit_seconds if result else None,
            "snapshot_id": snapshot.snapshot_id,
            "strategy": snapshot.strategy,
            "intervals": len(snapshot.problem.intervals),
            "reason": reason,
            "stop_reason": result.stop_reason if result else None,
            "plan_id": document["plan_id"] if document and reason == "published" else None,
            "currency": document.get("currency") if document else None,
            "build_seconds": result.build_seconds if result else None,
            "solve_seconds": result.solve_seconds if result else None,
            "financial_started_seconds": result.financial_started_seconds if result else None,
            "first_candidate_seconds": (
                result.first_candidate_seconds + worker_offset
                if result and result.first_candidate_seconds is not None
                else None
            ),
            "worker_start_seconds": worker_offset if result and result.started_at else None,
            "progress": [
                {**asdict(sample), "elapsed_seconds": sample.elapsed_seconds + worker_offset}
                for sample in result.progress
            ]
            if result
            else [],
        }
        return row

    def write(self, observation):
        """Atomically persist one detached observation and its lightweight daily summary."""
        row = dict(observation)
        samples = row.pop("progress")
        started = datetime.fromisoformat(row["started_at"])
        key = "calculations-" + started.astimezone(UTC).date().isoformat() + ".json"
        with self.documents.transaction():
            rows = self.documents.read(key) if self.documents.exists(key) else []
            self.documents.write("calculation-" + row["id"] + ".json", {**row, "progress": samples})
            self.documents.write(key, [*rows, row])

    def detail(self, identity):
        """Read one retained observation without transferring every curve for its day."""
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("Invalid calculation identity")
        return self.documents.read("calculation-" + identity + ".json")

    def day(self, selected, timezone):
        """Read UTC partitions surrounding a real local date, including 23-hour and 25-hour days."""
        selected = date.fromisoformat(selected)
        zone = ZoneInfo(timezone)
        rows = []
        for offset in (-1, 0, 1):
            try:
                partition = selected + timedelta(days=offset)
            except OverflowError:
                continue
            key = "calculations-" + partition.isoformat() + ".json"
            if self.documents.exists(key):
                rows.extend(self.documents.read(key))
        rows = [
            row
            for row in rows
            if datetime.fromisoformat(row["started_at"]).astimezone(zone).date() == selected
        ]
        return sorted(rows, key=lambda row: datetime.fromisoformat(row["started_at"]))


def register(app, documents, timezone):
    """Register an authenticated daily view and return the coordinator's nonblocking recorder."""
    history = CalculationHistory(documents)
    pending = asyncio.Queue(maxsize=MAX_PENDING_HISTORY_RECORDS)

    async def write_pending():
        """Serialize optional writes off-loop, surviving individual storage failures."""
        while True:
            observation = await pending.get()
            try:
                await asyncio.to_thread(history.write, observation)
            except Exception:
                _LOGGER.exception("Calculation history write failed. Planning is unaffected")
            finally:
                pending.task_done()

    async def lifecycle(_app):
        """Drain after planning stops and before the app-owned archive engine closes."""
        worker = asyncio.create_task(write_pending())
        try:
            yield
        finally:
            await pending.join()
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    app.cleanup_ctx.append(lifecycle)

    async def daily(request):
        """Default to today's HA calendar day. Reject invalid dates and sanitize storage failures."""
        zone = timezone()
        if not zone:
            raise web.HTTPConflict(text="Home Assistant timezone is not available yet")
        selected = request.query.get("day", datetime.now(ZoneInfo(zone)).date().isoformat())
        try:
            selected = date.fromisoformat(selected).isoformat()
        except ValueError:
            raise web.HTTPBadRequest(text="Choose a valid calendar day") from None
        try:
            rows = await asyncio.to_thread(history.day, selected, zone)
        except (OSError, TypeError, KeyError, ValueError):
            raise web.HTTPServiceUnavailable(text="Calculation history unavailable") from None
        return web.json_response({"day": selected, "timezone": zone, "calculations": rows})

    async def detail(request):
        """Return one authenticated calculation, with distinct missing and unavailable states."""
        identity = request.match_info["identity"]
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise web.HTTPBadRequest(text="Invalid calculation identity")
        try:
            value = await asyncio.to_thread(history.detail, identity)
        except FileNotFoundError:
            raise web.HTTPNotFound(text="Calculation not found") from None
        except (OSError, TypeError, KeyError, ValueError):
            raise web.HTTPServiceUnavailable(text="Calculation details unavailable") from None
        return web.json_response(value)

    async def record(snapshot, outcome, started, finished, reason):
        """Enqueue bounded detached data without making the next round wait for SQL."""
        observation = history.capture(snapshot, outcome, started, finished, reason)
        try:
            pending.put_nowait(observation)
        except asyncio.QueueFull:
            _LOGGER.warning("Calculation history queue is full. This observation was not recorded")

    app.router.add_get("/v1/planning-performance", daily)
    app.router.add_get("/v1/planning-performance/{identity}", detail)
    return record
