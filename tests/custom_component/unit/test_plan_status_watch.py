"""Verify catch-up and fallback independent of plan payload delivery."""

import asyncio
from contextlib import suppress

import pytest
from aiohttp import ClientConnectionError

from custom_components.open_energy_planner.plan_status_watch import (
    plan_status_cursor,
    watch_plan_status,
)


def test_watch_reconciles_publication_during_initial_read_but_ignores_idle():
    """An event during reconciliation is caught without a second periodic status read."""

    async def scenario():
        """Pause the first authoritative read while a publication changes the cursor."""
        release_read = asyncio.Event()
        next_request = asyncio.Event()
        first = "a" * 32
        next_cursor = "b" * 32
        replies = iter((first, next_cursor, next_cursor))
        requests = []
        reconciliations = []

        async def wait_for_change(cursor):
            """Return the publication and one identical idle reply, then hold the next watch."""
            requests.append(cursor)
            reply = next(replies, None)
            if reply is None:
                next_request.set()
                await asyncio.Event().wait()
            return reply

        async def reconcile():
            """Hold the initial read until the simulated publication is already visible."""
            reconciliations.append(len(requests))
            if len(reconciliations) == 1:
                await release_read.wait()
            return True

        task = asyncio.create_task(watch_plan_status(wait_for_change, reconcile, retry_seconds=0))
        try:
            await asyncio.sleep(0)
            assert requests == [None]
            release_read.set()
            await asyncio.wait_for(next_request.wait(), timeout=1)
            assert requests == [None, first, next_cursor, next_cursor]
            assert reconciliations == [1, 2]
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_watch_reconciles_revocation_after_disconnect_and_unsuccessful_status_read():
    """A lost wake connection and a failed reconciliation both lead to a fresh authority read."""

    async def scenario():
        """Expire the first connection, revoke the plan, then reconnect without an old cursor."""
        first = "a" * 32
        revoked = "b" * 32
        replies = iter((first, ClientConnectionError(), revoked))
        requests = []
        installs = []
        caught_up = asyncio.Event()

        async def wait_for_change(cursor):
            """Simulate a transport outage followed by a new authoritative position."""
            requests.append(cursor)
            reply = next(replies, None)
            if isinstance(reply, ClientConnectionError):
                raise reply
            if reply is None:
                caught_up.set()
                await asyncio.Event().wait()
            return reply

        async def reconcile():
            """Fail the first direct fallback, then install the revoked status on reconnect."""
            installs.append({1: "plan-a", 2: "unreachable", 3: None}[len(requests)])
            return len(requests) != 2

        task = asyncio.create_task(watch_plan_status(wait_for_change, reconcile, retry_seconds=0))
        try:
            await asyncio.wait_for(caught_up.wait(), timeout=1)
            assert requests == [None, first, None, revoked]
            assert installs == ["plan-a", "unreachable", None]
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"cursor": None},
        {"cursor": "z" * 32},
        {"cursor": "b" * 32, "plan": {"id": "synthetic-plan"}},
    ],
)
def test_watch_cursor_rejects_invalid_or_extra_fields(result):
    """Accept only a valid opaque server cursor in the advisory response."""
    with pytest.raises(ValueError, match="Invalid plan status watch response"):
        plan_status_cursor(result)
    assert plan_status_cursor({"cursor": "4" * 32}) == "4" * 32
