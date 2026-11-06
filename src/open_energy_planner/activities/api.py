"""Authenticated activity history and cursor-based live subscriptions."""

import asyncio
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web

from ..storage.adapters.activities import ActivityStore
from .events import Events, Outcome

EVENTS = web.AppKey("events", Events)


def register(app, engine, notifications, timezone):
    """Compose global state after the archive lifecycle and before all domain workers."""
    store = ActivityStore(engine)
    events = Events(store)
    app[EVENTS] = events
    subscribed = asyncio.Event()
    subscribers = 0
    closing = False

    async def shutdown(_app):
        """Release long polls before HTTP shutdown waits for active handlers."""
        nonlocal closing
        closing = True
        events.publish("shutdown", {})

    app.on_shutdown.append(shutdown)

    async def lifecycle(_app):
        """Recover unfinished work before workers start. Stop after all producers have drained."""
        events.loop = asyncio.get_running_loop()
        try:
            events.failed, events.sequence = await asyncio.to_thread(store.restart, events.clock())
        except (OSError, ValueError, KeyError, TypeError):
            events.history_available = False
            events.lost_updates = 1

        async def notices():
            """Bridge existing read-only business sources without deriving notifications from history."""
            while True:
                await subscribed.wait()
                events.notices(notifications.document())
                await asyncio.sleep(1)

        task = asyncio.create_task(notices())

        async def retention():
            """Keep physical retention bounded even without new work or connected users."""
            while True:
                await asyncio.sleep(3600)
                await events.prune()

        pruning = asyncio.create_task(retention())
        try:
            yield
        finally:
            task.cancel()
            pruning.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(asyncio.CancelledError):
                await pruning
            for identity in tuple(events.running):
                await events.finish(
                    identity,
                    Outcome.INTERRUPTED,
                    "Stopped before a definitive outcome was recorded.",
                )
            await events.close()

    async def history(request):
        """Use installation-local days, including DST. History pages are bounded to 100 executions."""
        zone_name = timezone()
        if not zone_name:
            raise web.HTTPConflict(text="Home Assistant timezone is not available yet")
        zone = ZoneInfo(zone_name)
        now = events.clock()
        try:
            selected = date.fromisoformat(
                request.query.get("day", now.astimezone(zone).date().isoformat())
            )
            start = datetime.combine(selected, time(), zone).astimezone(UTC)
            end = datetime.combine(selected + timedelta(days=1), time(), zone).astimezone(UTC)
            before = request.query.get("before")
            if before:
                stamp, identity = before.split("|", 1)
                parsed = datetime.fromisoformat(stamp)
                if (
                    parsed.utcoffset() is None
                    or len(identity) != 32
                    or any(character not in "0123456789abcdef" for character in identity)
                ):
                    raise ValueError("Invalid cursor")
                before = (parsed.astimezone(UTC).isoformat(), identity)
        except (ValueError, OverflowError):
            raise web.HTTPBadRequest(text="Choose a valid day and activity cursor") from None
        start = max(start, now - timedelta(days=7))
        kind = request.query.get("kind")
        if kind is not None and not 1 <= len(kind) <= 128:
            raise web.HTTPBadRequest(text="Invalid activity type")
        try:
            await events.flush()
            rows = await asyncio.to_thread(store.read, start, end, before, 100, kind)
        except (OSError, ValueError):
            raise web.HTTPServiceUnavailable(text="Activity history is unavailable") from None
        return web.json_response(
            {
                "day": selected.isoformat(),
                "timezone": zone_name,
                "activities": rows,
                "next": f"{rows[-1]['started_at']}|{rows[-1]['id']}" if len(rows) == 100 else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def subscribe(request):
        """A bounded long poll accepts bearer headers and keeps credentials out of URLs."""
        nonlocal subscribers
        if closing:
            raise web.HTTPServiceUnavailable(text="Restart in progress")
        cursor = request.query.get("cursor")
        if cursor is not None and len(cursor) > 96:
            raise web.HTTPBadRequest(text="Invalid event cursor")
        subscribers += 1
        subscribed.set()
        try:
            return web.json_response(
                await events.subscribe(cursor), headers={"Cache-Control": "no-store"}
            )
        finally:
            subscribers -= 1
            if not subscribers:
                subscribed.clear()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/v1/activities", history)
    app.router.add_get("/v1/events", subscribe)
    return events
