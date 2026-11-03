"""Solar HTTP and background lifecycle composition, separate from forecast calculations."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from aiohttp import web

from open_energy_planner.core.units import BYTES_PER_KIB, MINUTES_PER_HOUR, SECONDS_PER_MINUTE
from open_energy_planner.history.wire import parse_history
from open_energy_planner.notifications.api import NOTIFICATIONS
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog

from .ha_forecasts import parse_ha_predictions
from .notifications import explain_solar_problem, solar_forecast_notifications
from .wire import parse_configuration

SOLAR_WORKER_POLL_SECONDS = 5
WEATHER_RETRY_MINUTES = 30
WEATHER_REFRESH_HOURS = 1
MAX_HA_FORECAST_BODY_BYTES = 32 * BYTES_PER_KIB * BYTES_PER_KIB

_LOGGER = logging.getLogger(__name__)


async def worker(runtime, job):
    """Drain immutable archive writes before propagating request/shutdown cancellation."""
    task = asyncio.create_task(asyncio.to_thread(runtime.build, job))
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancelled = error
    if cancelled is not None:
        raise cancelled
    return result


def register(app, runtime, live, lock, persist, snapshot, weather_factory, membership):
    """Wire authenticated routes and one coalesced model/weather lifecycle into the existing app."""

    def current_notifications():
        """Read cached forecast availability. Unreadable configuration cannot confirm recovery."""
        if runtime.store.issue:
            return None
        return solar_forecast_notifications(
            runtime.configuration,
            runtime.forecast,
            live.report.location if live and live.report else None,
            datetime.now(UTC),
            runtime.issue,
        )

    app[NOTIFICATIONS].register("solar", current_notifications, explain=explain_solar_problem)

    async def settings(request):
        """Read cached status. Full interval detail is explicitly requested outside the HA sync payload."""
        async with lock:
            return web.json_response(
                runtime.document(datetime.now(UTC), series=request.query.get("series") == "1")
            )

    async def configure(request):
        """Commit only changed definitions with compare-and-set protection and no contract activation."""
        if live is None:
            raise web.HTTPConflict(text="Solar configuration requires Home Assistant mode")
        try:
            body = await request.json()
            candidate = parse_configuration(body)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid solar configuration") from None
        async with lock:
            try:
                membership.validate_membership("solar", candidate)
            except ValueError as error:
                raise web.HTTPBadRequest(text=str(error)) from None
            current = runtime.configuration.version if runtime.configuration else None
            if body.get("expected_version") != current:
                raise web.HTTPConflict(text="Solar configuration changed. Reload before saving")
            if candidate.version != current:
                await persist(
                    runtime.store.save, candidate, runtime.configure, contract_input=False
                )
        return await settings(request)

    async def history(request):
        """Accept one bounded Recorder response against the exact source/configuration request."""
        try:
            body = await request.clone(client_max_size=BYTES_PER_KIB * BYTES_PER_KIB).json()
            if not isinstance(body, dict):
                raise TypeError("Invalid solar history")
            error = body.get("error")
            if error is not None and (
                set(body) != {"request_version", "error"}
                or error
                not in ("recorder_unavailable", "history_unavailable", "unsupported_statistics")
            ):
                raise ValueError("Unsupported solar history failure")
            values = None if error else parse_history(body)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid solar history") from None
        async with lock:
            pending = runtime.request(datetime.now(UTC))
            if pending is None or body.get("request_version") != pending["version"]:
                raise web.HTTPConflict(text="Solar history request changed")
            try:
                runtime.receive(body["request_version"], values, datetime.now(UTC), error=error)
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid solar history source/range") from None
        return await snapshot(request)

    async def ha_forecasts(request):
        """Accept only current selected Energy providers, independently of changing physical meter values."""
        if live is None:
            raise web.HTTPConflict(text="HA forecasts require Home Assistant mode")
        try:
            body = await request.clone(client_max_size=MAX_HA_FORECAST_BODY_BYTES).json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid HA solar forecast") from None
        async with lock:
            try:
                hints = (membership.value or {}).get("suggestions", {})
                predictions = parse_ha_predictions(
                    body, hints.get("solar_configurations", []), datetime.now(UTC)
                )
            except (ValueError, TypeError, KeyError, OverflowError):
                raise web.HTTPBadRequest(
                    text="Invalid HA solar forecast or changed Energy association"
                ) from None
            current = {
                entity: tuple((row.version, row.provider_refreshed_at) for row in rows)
                for entity, rows in runtime.ha_predictions.items()
            }
            incoming = {
                entity: tuple((row.version, row.provider_refreshed_at) for row in rows)
                for entity, rows in predictions.items()
            }
            if incoming != current:
                await persist(
                    runtime.save_ha, predictions, runtime.receive_ha, contract_input=False
                )
        return web.json_response({"accepted": True})

    async def lifecycle(_app):
        """Keep network waits and numerical work outside the shared app state lock."""
        forecast_logger = BackgroundLog(_LOGGER, "Solar forecast update")

        async def update():
            """Install only results matching current immutable inputs, with bounded retry on failure."""
            while True:
                job = None
                try:
                    async with lock:
                        job = runtime.job(datetime.now(UTC))
                    if job:
                        result = await worker(runtime, job)
                        async with lock:
                            if runtime.install(result, datetime.now(UTC)):
                                if runtime.forecast is not None:
                                    forecast_logger.success("A new solar forecast is available.")
                                if live:
                                    live.refresh()
                except Exception:
                    async with lock:
                        if job:
                            runtime.failed(job, datetime.now(UTC))
                        retry_seconds = (
                            (runtime.retry_after - datetime.now(UTC)).total_seconds()
                            if runtime.retry_after and runtime.retry_after > datetime.now(UTC)
                            else SOLAR_WORKER_POLL_SECONDS
                        )
                    forecast_logger.exception(
                        "Retaining only valid forecasts. Planning still requires usable solar data.",
                        retry_seconds=max(SOLAR_WORKER_POLL_SECONDS, round(retry_seconds)),
                    )
                await asyncio.sleep(SOLAR_WORKER_POLL_SECONDS)

        task = asyncio.create_task(update())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(lifecycle)
    if weather_factory is not None and live is not None:

        async def weather_lifecycle(_app):
            """Refresh common solar weather hourly. Retry failures after 30 minutes and detect site changes."""
            async with weather_factory() as source:
                weather_logger = BackgroundLog(_LOGGER, "Solar weather refresh")

                async def update_weather():
                    """Fetch only for configured PV and the latest HA location, independent of solver cadence."""
                    last_location, due = None, datetime.min.replace(tzinfo=UTC)
                    while True:
                        now = datetime.now(UTC)
                        async with lock:
                            location = live.report.location if live.report else None
                            configured = bool(
                                runtime.configuration and runtime.configuration.sources
                            )
                            if location != last_location:
                                runtime.set_weather(location, ())
                                last_location, due = location, now
                        if configured and location and now >= due:
                            due = now + timedelta(minutes=WEATHER_RETRY_MINUTES)
                            try:
                                weather = await source.read(location, now)
                                async with lock:
                                    if live.report and live.report.location == location:
                                        runtime.set_weather(location, weather)
                                        weather_logger.success(
                                            f"Received {len(weather)} weather hours. Next refresh in {WEATHER_REFRESH_HOURS * MINUTES_PER_HOUR:g} minutes."
                                        )
                                due = now + timedelta(hours=WEATHER_REFRESH_HOURS)
                            except Exception as error:
                                weather_logger.exception(
                                    "Retaining only valid forecasts. Planning still requires usable solar data.",
                                    retry_seconds=WEATHER_RETRY_MINUTES * SECONDS_PER_MINUTE,
                                    expected=isinstance(error, ValueError),
                                    reason="invalid weather data"
                                    if isinstance(error, ValueError)
                                    else None,
                                )
                        await asyncio.sleep(SOLAR_WORKER_POLL_SECONDS)

                task = asyncio.create_task(update_weather())
                try:
                    yield
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        app.cleanup_ctx.append(weather_lifecycle)
    app.router.add_get("/v1/solar", settings)
    app.router.add_put("/v1/solar", configure)
    app.router.add_post("/v1/solar/history", history)
    app.router.add_post("/v1/solar/ha-forecasts", ha_forecasts)
