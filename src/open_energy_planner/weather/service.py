"""Supply one admitted live weather run and bounded original-run evidence to consumers."""

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from functools import partial

from open_energy_planner.activities.api import EVENTS
from open_energy_planner.activities.events import Health, Outcome
from open_energy_planner.core.units import MINUTES_PER_HOUR, SECONDS_PER_MINUTE
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog
from open_energy_planner.storage.writes import complete_write

from .history import weather_factory as default_history_factory
from .model import version
from .provider import OpenMeteoWeatherSource, live_weather_factory
from .run import MAX_LIVE_RUN_AGE_HOURS

WEATHER_RETRY_MINUTES = 30
WEATHER_REFRESH_HOURS = 1
WEATHER_RECEIPT_AGE_HOURS = 2
WEATHER_RETRY_SECONDS = WEATHER_RETRY_MINUTES * SECONDS_PER_MINUTE
WEATHER_REFRESH_SECONDS = WEATHER_REFRESH_HOURS * MINUTES_PER_HOUR * SECONDS_PER_MINUTE
_LOGGER = logging.getLogger(__name__)


class LiveWeather:
    """Publish durable weather evidence before making one accepted run available to consumers."""

    def __init__(self, store):
        """Borrow the weather namespace used by the provider cache and forecast provenance."""
        self.store = store
        self.clear()

    def clear(self):
        """Revoke evidence admitted for a previous location."""
        self.rows, self.service, self.reference = (), {}, None

    async def accept(self, rows, service):
        """Separate receipt time from run identity and archive new content only once."""
        rows, service = tuple(rows), dict(service or {})
        if not rows:
            self.clear()
            return (), {}, None, None, None
        received_at = rows[-1].received_at
        until = (
            datetime.fromisoformat(service["initialized_at"])
            + timedelta(hours=MAX_LIVE_RUN_AGE_HOURS)
            if service
            else received_at + timedelta(hours=WEATHER_RECEIPT_AGE_HOURS)
        )
        unchanged = (
            len(rows) == len(self.rows)
            and {key: value for key, value in service.items() if key != "received_at"}
            == {key: value for key, value in self.service.items() if key != "received_at"}
            and all(
                previous == replace(new, received_at=previous.received_at)
                for previous, new in zip(self.rows, rows, strict=True)
            )
        )
        reference = self.reference
        if not unchanged:
            evidence = json.loads(json.dumps([asdict(row) for row in rows], default=str))
            reference = version(evidence)
            cancellation = await complete_write(
                self.store.archive_series, "weather", reference, evidence
            )
            if cancellation:
                raise cancellation
        if datetime.now(UTC) >= until:
            raise ValueError("Live weather expired before publication")
        if not unchanged:
            self.rows, self.service, self.reference = rows, service, reference
        return self.rows, self.service, self.reference, until, received_at


def register(
    app,
    live,
    lock,
    weather_factory,
    store,
    oldest_history,
    needs_live_weather,
    receive_live_weather,
    needs_original_runs,
    receive_original_runs,
    poll_seconds,
):
    """Own provider refreshes while domain consumers receive normalized evidence under the app lock."""
    if weather_factory is not None and weather_factory is live_weather_factory:
        weather_factory = partial(weather_factory, store=store, history_start=oldest_history)
    archive = LiveWeather(store)
    if weather_factory is not None and live is not None:

        async def weather_lifecycle(_app):
            """Refresh shared forecast weather hourly. Retry failures after 30 minutes and detect site changes."""
            async with weather_factory() as source:
                weather_logger = BackgroundLog(_LOGGER, "Live weather refresh")
                events = app.get(EVENTS)

                async def update_weather():
                    """Fetch one live run for configured forecasts at the current HA location."""
                    last_location, due = None, datetime.min.replace(tzinfo=UTC)
                    last_history_start = None
                    while True:
                        now = datetime.now(UTC)
                        async with lock:
                            location = live.report.location if live.report else None
                            configured = needs_live_weather()
                            if location != last_location:
                                archive.clear()
                                receive_live_weather(location, (), {}, None, None, None)
                                last_location, due = location, now
                            history_start = oldest_history()
                            if history_start != last_history_start:
                                last_history_start, due = history_start, now
                        if configured and location and now >= due:
                            due = now + timedelta(minutes=WEATHER_RETRY_MINUTES)
                            activity = (
                                await events.start(
                                    "solar-weather",
                                    "weather",
                                    "Fetch live weather",
                                    context={"label": "Live weather"},
                                )
                                if events
                                else None
                            )
                            try:
                                weather = await source.read(location, now)
                                installed = False
                                async with lock:
                                    if (
                                        live.report
                                        and live.report.location == location
                                        and needs_live_weather()
                                    ):
                                        (
                                            rows,
                                            service,
                                            reference,
                                            until,
                                            received_at,
                                        ) = await archive.accept(
                                            weather, getattr(source, "service", None)
                                        )
                                        receive_live_weather(
                                            location, rows, service, reference, until, received_at
                                        )
                                        if (
                                            isinstance(source, OpenMeteoWeatherSource)
                                            and needs_original_runs()
                                        ):
                                            await receive_original_runs(
                                                location, source.forecast_runs
                                            )
                                        installed = True
                                        if weather:
                                            weather_logger.success(
                                                f"Received {len(weather)} weather hours. Next refresh in {WEATHER_REFRESH_HOURS * MINUTES_PER_HOUR:g} minutes."
                                            )
                                        else:
                                            weather_logger.unavailable(
                                                "Existing forecasts keep their original validity.",
                                                reason="no usable current weather hours were returned",
                                                retry_seconds=WEATHER_REFRESH_HOURS
                                                * MINUTES_PER_HOUR
                                                * SECONDS_PER_MINUTE,
                                            )
                                due = now + timedelta(hours=WEATHER_REFRESH_HOURS)
                                if activity:
                                    await events.finish(
                                        activity,
                                        Outcome.SKIPPED
                                        if not installed
                                        else Outcome.COMPLETED
                                        if weather
                                        else Outcome.FAILED,
                                        "Live weather was discarded because the installation changed."
                                        if not installed
                                        else "Live weather received."
                                        if weather
                                        else "No usable live weather returned. Existing forecasts keep their original validity.",
                                        usable=installed and bool(weather),
                                    )
                                    if installed:
                                        events.availability(
                                            "solar-weather",
                                            Health.AVAILABLE if weather else Health.DEGRADED,
                                            "Live weather available."
                                            if weather
                                            else "No new usable live weather. Check forecast validity.",
                                        )
                            except Exception as error:
                                if asyncio.current_task().cancelling():
                                    raise asyncio.CancelledError from error
                                weather_logger.exception(
                                    "Existing forecasts keep their original validity.",
                                    retry_seconds=WEATHER_RETRY_MINUTES * SECONDS_PER_MINUTE,
                                    expected=isinstance(error, ValueError),
                                    reason="invalid weather data"
                                    if isinstance(error, ValueError)
                                    else None,
                                )
                                if activity and activity in events.running:
                                    await events.finish(
                                        activity,
                                        Outcome.FAILED,
                                        "Live weather refresh failed. Existing forecasts keep their original validity.",
                                    )
                                    events.availability(
                                        "solar-weather",
                                        Health.DEGRADED,
                                        "Live weather refresh failed. Check forecast validity.",
                                    )
                        if events:
                            if configured and location:
                                events.scheduled(
                                    "solar-weather",
                                    "weather",
                                    "Fetch live weather",
                                    due,
                                    context={"label": "Live weather"},
                                )
                            else:
                                events.withdraw("solar-weather")
                        await asyncio.sleep(poll_seconds)

                async def update_original_runs():
                    """Import small historical original-run batches independently of current forecast publication."""
                    history_logger = BackgroundLog(_LOGGER, "Historical weather run import")
                    while True:
                        now = datetime.now(UTC)
                        async with lock:
                            location = live.report.location if live.report else None
                            configured = needs_original_runs()
                        if configured and location:
                            try:
                                runs = await source.import_runs(location, now)
                                async with lock:
                                    if (
                                        live.report
                                        and live.report.location == location
                                        and needs_original_runs()
                                    ):
                                        await receive_original_runs(location, runs)
                            except Exception as error:
                                if asyncio.current_task().cancelling():
                                    raise asyncio.CancelledError from error
                                history_logger.exception(
                                    "Current solar forecasts remain available. Missing historical original forecasts are retried after the import backoff.",
                                    expected=isinstance(error, ValueError),
                                    reason="invalid original forecast data"
                                    if isinstance(error, ValueError)
                                    else None,
                                )
                        await asyncio.sleep(poll_seconds)

                task = asyncio.create_task(update_weather())
                history_task = (
                    asyncio.create_task(update_original_runs())
                    if isinstance(source, OpenMeteoWeatherSource)
                    else None
                )
                try:
                    yield
                finally:
                    task.cancel()
                    if history_task is not None:
                        history_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    if history_task is not None:
                        with suppress(asyncio.CancelledError):
                            await history_task

        app.cleanup_ctx.append(weather_lifecycle)


def register_history(
    app,
    live,
    lock,
    weather_factory,
    documents,
    oldest_history,
    needs_history,
    receive_history,
    poll_seconds,
):
    """Supply completed historical temperature hours to training independently of live forecasts."""
    if weather_factory is None or live is None:
        return
    if weather_factory is default_history_factory:
        weather_factory = partial(weather_factory, documents, history_start=oldest_history)
    events = app[EVENTS]

    async def weather_lifecycle(_app):
        """Fetch temperature independently from planning, with hourly success and 30-minute failure retries."""
        async with weather_factory() as weather_source:
            weather_logger = BackgroundLog(_LOGGER, "Historical temperature refresh")

            async def update_weather():
                """Use HA's current location. Missing weather leaves calendar models available."""
                last_location = None
                while True:
                    delay = WEATHER_RETRY_SECONDS
                    async with lock:
                        location = live.report.location if live.report else None
                        configured = needs_history()
                        if location != last_location:
                            receive_history(())
                            last_location = location
                    if configured and location is not None:
                        activity = await events.start(
                            "temperature-weather",
                            "weather",
                            "Fetch historical temperatures",
                            context={"label": "Household consumption"},
                        )
                        try:
                            temperatures = await weather_source.read(location, datetime.now(UTC))
                            installed = False
                            async with lock:
                                if (
                                    needs_history()
                                    and live.report
                                    and live.report.location == location
                                ):
                                    receive_history(temperatures)
                                    installed = True
                                    if temperatures:
                                        weather_logger.success(
                                            f"Received {len(temperatures)} temperature hours. Next refresh in {WEATHER_REFRESH_SECONDS / SECONDS_PER_MINUTE:g} minutes."
                                        )
                                    else:
                                        weather_logger.unavailable(
                                            "Calendar consumption forecasting remains available without temperature data.",
                                            reason="no usable temperature hours",
                                            retry_seconds=WEATHER_REFRESH_SECONDS,
                                        )
                            delay = WEATHER_REFRESH_SECONDS
                            await events.finish(
                                activity,
                                Outcome.SKIPPED
                                if not installed
                                else Outcome.COMPLETED
                                if temperatures
                                else Outcome.FAILED,
                                "Historical temperatures were discarded because the location or demand configuration changed."
                                if not installed
                                else "Historical temperatures received."
                                if temperatures
                                else "No usable temperature hours returned. Calendar consumption forecasting remains available.",
                                usable=installed and bool(temperatures),
                            )
                            if installed:
                                events.availability(
                                    "temperature-weather",
                                    Health.AVAILABLE if temperatures else Health.DEGRADED,
                                    "Historical temperatures available."
                                    if temperatures
                                    else "Calendar consumption forecasting remains available without temperature data.",
                                )
                        except Exception as error:
                            if asyncio.current_task().cancelling():
                                raise asyncio.CancelledError from error
                            await events.finish(
                                activity,
                                Outcome.FAILED,
                                "Weather refresh failed. Calendar consumption forecasting remains available. Existing forecasts keep their original expiry.",
                            )
                            events.availability(
                                "temperature-weather",
                                Health.DEGRADED,
                                "Calendar consumption forecasting remains available without fresh temperature data.",
                            )
                            weather_logger.exception(
                                "Existing valid forecasts retain their expiry. Calendar consumption forecasting does not require fresh temperature data.",
                                retry_seconds=WEATHER_RETRY_SECONDS,
                                expected=isinstance(error, ValueError),
                                reason="invalid weather data"
                                if isinstance(error, ValueError)
                                else None,
                            )
                    else:
                        delay = poll_seconds
                    async with lock:
                        active = bool(needs_history() and live.report and live.report.location)
                    if active:
                        events.scheduled(
                            "temperature-weather",
                            "weather",
                            "Fetch historical temperatures",
                            datetime.now(UTC) + timedelta(seconds=delay),
                            context={"label": "Household consumption"},
                        )
                    else:
                        events.withdraw("temperature-weather")
                    await asyncio.sleep(delay)

            task = asyncio.create_task(update_weather())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app.cleanup_ctx.append(weather_lifecycle)
