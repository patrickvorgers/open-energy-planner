"""Coalesce selected Home Assistant changes into independent observation reports."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from .measurements import MAX_READING_AGE_SECONDS
from .observation_bridge import ObservationBridge

COALESCE_SECONDS = 0.25
MIN_PUSH_INTERVAL_SECONDS = 5
HEARTBEAT_SECONDS = 30


class ObservationPush:
    """Drive one observation sender from selected changes and a freshness heartbeat."""

    def __init__(
        self,
        subscribe: Callable[[tuple[str, ...], Callable[[], None]], Callable[[], None]],
        send: Callable[[], Awaitable[bool]],
    ):
        """Accept an indexed HA listener factory and a current-revision sender."""
        self._subscribe = subscribe
        self._send = send
        self._revision: str | None = None
        self._entities: tuple[str, ...] = ()
        self._unsubscribe: Callable[[], None] | None = None
        self._changed = asyncio.Event()
        self._reconfigured = asyncio.Event()
        self._interval = MIN_PUSH_INTERVAL_SECONDS
        self._heartbeat = HEARTBEAT_SECONDS

    def configure(self, bridge: ObservationBridge | None):
        """Replace selected listeners and request a fresh capture for a new revision."""
        revision = bridge.revision if bridge is not None else None
        if revision == self._revision:
            return
        bindings = bridge.bindings if bridge is not None else ()
        entities = tuple(sorted({binding.entity_id for binding in bindings}))
        if entities != self._entities:
            self._remove_listener()
            self._entities = entities
            if entities:
                self._unsubscribe = self._subscribe(entities, self._changed.set)
        self._revision = revision
        max_age = min(
            (binding.maximum_age_s for binding in bindings if binding.required),
            default=MAX_READING_AGE_SECONDS,
        )
        self._heartbeat = min(HEARTBEAT_SECONDS, max_age / 2)
        self._interval = min(MIN_PUSH_INTERVAL_SECONDS, self._heartbeat)
        self._reconfigured.set()
        self._changed.set()

    async def run(self):
        """Send one coherent capture at a time, replacing superseded pending captures."""
        loop = asyncio.get_running_loop()
        last_revision = None
        last_sent = 0.0
        accepted = False
        while True:
            if self._revision is None:
                self._changed.clear()
                await self._changed.wait()
                continue
            if self._revision == last_revision:
                deadline = last_sent + (self._heartbeat if accepted else self._interval)
                try:
                    async with asyncio.timeout_at(deadline):
                        await self._changed.wait()
                except TimeoutError:
                    pass
                else:
                    if self._revision == last_revision:
                        deadline = min(
                            deadline,
                            max(loop.time() + COALESCE_SECONDS, last_sent + self._interval),
                        )
                        with suppress(TimeoutError):
                            async with asyncio.timeout_at(deadline):
                                await self._reconfigured.wait()
                if self._revision is None:
                    continue
            self._reconfigured.clear()
            self._changed.clear()
            last_revision, last_sent = self._revision, loop.time()
            accepted = await self._send()

    def close(self):
        """Release listeners and disable capture while the coordinator drains its task."""
        self.configure(None)

    def _remove_listener(self):
        """Unsubscribe from the previous entity set before replacing the listener."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
