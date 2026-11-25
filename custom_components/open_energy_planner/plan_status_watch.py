"""Wait for advisory plan signals and use normal reconciliation for every change."""

import asyncio
import re
from collections.abc import Awaitable, Callable

from aiohttp import ClientError

WATCH_PATH = "/v1/ha/plan-status/watch"
WATCH_TIMEOUT_SECONDS = 25
_CURSOR = re.compile(r"[0-9a-f]{32}")


def plan_status_cursor(result: dict) -> str:
    """Validate the complete bounded watch response before using its cursor."""
    cursor = result.get("cursor")
    if set(result) != {"cursor"} or not isinstance(cursor, str) or not _CURSOR.fullmatch(cursor):
        raise ValueError("Invalid plan status watch response")
    return cursor


async def watch_plan_status(
    wait_for_change: Callable[[str | None], Awaitable[str]],
    reconcile: Callable[[], Awaitable[bool]],
    *,
    has_pending_intent: Callable[[], bool],
    retry_seconds: float,
):
    """Reconcile on connection or change, polling only while recovery is needed.

    Sample the cursor before reconciling, so a publication during the read
    wakes the next wait. A pending local intent also retries after an
    unchanged wake. A failed reconciliation discards the cursor so recovery
    can continue even when the app does not publish again.
    """
    cursor = None
    while True:
        try:
            observed = await wait_for_change(cursor)
        except (ClientError, TimeoutError, ValueError, TypeError, KeyError):
            observed = None
        if observed is not None and observed == cursor and not has_pending_intent():
            continue
        if await reconcile() and observed is not None:
            cursor = observed
        else:
            cursor = None
            await asyncio.sleep(retry_seconds)
