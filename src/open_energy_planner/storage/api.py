"""Serve authenticated capacity history and own its cancellable daily sampling worker."""

import asyncio
import logging
from calendar import monthrange
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web

from open_energy_planner.runtime.diagnostics.logging import BackgroundLog, request_error_message
from open_energy_planner.storage.writes import complete_write

from .adapters import usage as storage_capacity
from .adapters.usage_history import DAILY_SAMPLE_HOUR, StorageHistory

SAMPLE_POLL_SECONDS = 60
CALENDAR_PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12}
HISTORY_PERIODS = {"week", "all", *CALENDAR_PERIOD_MONTHS}
DAYS_PER_WEEK = 7
INVALID_PERIOD = "Choose a supported database history period."
_LOGGER = logging.getLogger(__name__)


def period_start(day, period):
    """Use inclusive installation calendar dates, clamping calendar month ends."""
    if period == "all":
        return None
    if period == "week":
        return day - timedelta(days=DAYS_PER_WEEK - 1)
    months = CALENDAR_PERIOD_MONTHS[period]
    month_index = day.year * 12 + day.month - 1 - months
    year, month = divmod(month_index, 12)
    return day.replace(year=year, month=month + 1, day=min(day.day, monthrange(year, month + 1)[1]))


def register(app, engine, timezone):
    """Compose read-only HTTP routes and daily collection around the app-owned database."""
    history = StorageHistory(engine)

    def measure():
        """Pair capacity with its UTC completion time before returning to the HTTP event loop."""
        usage = storage_capacity.storage_usage(engine)
        return usage, datetime.now(UTC).isoformat(timespec="microseconds")

    async def capacity(request):
        """Measure live capacity off the event loop without retaining an extra history point."""
        usage, observed_at = await asyncio.to_thread(measure)
        return web.json_response(
            {
                **asdict(usage),
                "status": usage.status,
                "observed_at": observed_at,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def observations(request):
        """Return a bounded calendar window without exposing connection credentials or paths."""
        try:
            period = request.query.get("period", "month")
            if period not in HISTORY_PERIODS:
                raise ValueError(INVALID_PERIOD)
            zone = timezone()
            if not zone:
                return web.json_response(
                    {
                        "period": period,
                        "start": None,
                        "end": None,
                        "timezone": None,
                        "sample_hour": DAILY_SAMPLE_HOUR,
                        "samples": [],
                        "latest": None,
                    },
                    headers={"Cache-Control": "no-store"},
                )
            today = datetime.now(UTC).astimezone(ZoneInfo(zone)).date()
            start = period_start(today, period)
            rows = await asyncio.to_thread(
                history.read, start.isoformat() if start else None, today.isoformat()
            )
            latest = (
                rows[-1] if rows else await asyncio.to_thread(history.latest, today.isoformat())
            )
            return web.json_response(
                {
                    "period": period,
                    "start": start.isoformat()
                    if start
                    else (rows[0]["day"] if rows else today.isoformat()),
                    "end": today.isoformat(),
                    "timezone": zone,
                    "sample_hour": DAILY_SAMPLE_HOUR,
                    "samples": rows,
                    "latest": latest,
                },
                headers={"Cache-Control": "no-store"},
            )
        except (OSError, ValueError, TypeError, KeyError) as error:
            message = request_error_message(
                error,
                logger=_LOGGER,
                operation="Read database growth",
                safe_messages=(INVALID_PERIOD,),
                fallback="Database history unavailable. Check the database connection and refresh.",
            )
            return web.json_response(
                {"error": message},
                status=400 if message == INVALID_PERIOD else 503,
                headers={"Cache-Control": "no-store"},
            )

    async def lifecycle(_app):
        """Drain a running SQL write before the shared repository closes during shutdown or migration."""

        async def collect():
            """Retry durable write failures without disturbing planning or exposing private errors."""
            diagnostics = BackgroundLog(_LOGGER, "Daily database measurement")
            while True:
                try:
                    cancellation = await complete_write(
                        history.sample, lambda: datetime.now(UTC), timezone()
                    )
                    if cancellation is not None:
                        raise cancellation
                    diagnostics.success("Daily database measurement worker is available.")
                except (OSError, ValueError, TypeError, KeyError):
                    stopping = asyncio.current_task().cancelling()
                    diagnostics.exception(
                        "Database growth history may have a gap. Live capacity and planning remain available.",
                        retry_seconds=None if stopping else SAMPLE_POLL_SECONDS,
                    )
                    if stopping:
                        raise asyncio.CancelledError from None
                await asyncio.sleep(SAMPLE_POLL_SECONDS)

        worker = asyncio.create_task(collect())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    app.router.add_get("/v1/storage", capacity)
    app.router.add_get("/v1/storage/history", observations)
    app.cleanup_ctx.append(lifecycle)
