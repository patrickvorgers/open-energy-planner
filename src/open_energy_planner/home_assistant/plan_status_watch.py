"""Offer a bounded wake signal for authenticated Home Assistant plan reconciliation."""

import asyncio
import re
from contextlib import suppress
from uuid import uuid4

from aiohttp import web

WATCH_SECONDS = 20
WATCH_PATH = "/v1/ha/plan-status/watch"
_CURSOR = re.compile(r"[0-9a-f]{32}")


class PlanStatusWatch:
    """Signal state changes to the connected peer without sending command data."""

    def __init__(self):
        """Keep a fresh opaque cursor for this app process."""
        self._cursor = uuid4().hex
        self._changed = asyncio.Event()
        self._status: tuple | None = None

    def publish(self, status: tuple):
        """Wake existing requests after a new authoritative status becomes visible."""
        if status == self._status:
            return
        self._status = status
        self._cursor = uuid4().hex
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def wait(self, cursor: str | None) -> str:
        """Return the current cursor after a change or bounded idle wait."""
        if cursor == self._cursor:
            with suppress(TimeoutError):
                async with asyncio.timeout(WATCH_SECONDS):
                    await self._changed.wait()
        return self._cursor

    async def response(self, request: web.Request) -> web.Response:
        """Return only the advisory cursor through the existing authenticated API."""
        cursor = request.query.get("cursor")
        if cursor is not None and not _CURSOR.fullmatch(cursor):
            raise web.HTTPBadRequest(text="Invalid plan status cursor")
        return web.json_response(
            {"cursor": await self.wait(cursor)},
            headers={"Cache-Control": "no-store"},
        )
