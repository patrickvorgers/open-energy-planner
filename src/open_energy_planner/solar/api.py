"""Solar HTTP and background lifecycle composition, separate from forecast calculations."""

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial

from aiohttp import web

from open_energy_planner.activities.api import EVENTS
from open_energy_planner.activities.events import Health, Outcome
from open_energy_planner.core.units import BYTES_PER_KIB, MINUTES_PER_HOUR, SECONDS_PER_MINUTE
from open_energy_planner.home_assistant.names import source_names
from open_energy_planner.installation.forecast_policy import next_update
from open_energy_planner.notifications.api import NOTIFICATIONS
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog, request_error_message
from open_energy_planner.solar.storage.history import parse_report
from open_energy_planner.storage.writes import complete_write

from .basis import forecast_usable
from .curtailment import FeedbackRequestError
from .ha_forecasts import parse_ha_predictions
from .notifications import explain_solar_problem, solar_forecast_notifications
from .weather import OpenMeteoSolarSource
from .weather import solar_weather_factory as default_solar_weather_factory
from .wire import parse_configuration

SOLAR_WORKER_POLL_SECONDS = 5
WEATHER_RETRY_MINUTES = 30
WEATHER_REFRESH_HOURS = 1
MAX_HA_FORECAST_BODY_BYTES = 32 * BYTES_PER_KIB * BYTES_PER_KIB
MAX_FEEDBACK_BODY_BYTES = BYTES_PER_KIB * BYTES_PER_KIB
PUBLIC_VALIDATION_MESSAGES = (
    "Complete the Home Assistant Energy dashboard first",
    "Device membership is managed in the Home Assistant Energy dashboard",
    "Wait for complete Home Assistant panel settings",
    "Panel settings are managed in Home Assistant. Reload before saving",
    "The solar forecast link changed. Reload before saving",
)

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

    def oldest_history():
        """Request weather back to the earliest accepted meter hour, preserving bounded provider reads."""
        return min(
            (history.hours[0].start for history in runtime.histories.values() if history.hours),
            default=None,
        )

    if weather_factory is default_solar_weather_factory:
        weather_factory = partial(
            weather_factory, store=runtime.store, history_start=oldest_history
        )

    def activity_context(configuration):
        """Name all captured PV sources without exposing opaque source identities or location."""
        names = source_names(membership.value if membership is not None else None)
        labels = (
            dict.fromkeys(
                names.get(source.entity_id, "Solar production") for source in configuration.sources
            )
            if configuration is not None
            else {}
        )
        return {"label": ", ".join(labels) or "Solar production"}

    def current_notifications():
        """Read cached forecast availability. Unreadable configuration cannot confirm recovery."""
        # Configuration restoration owns this lock in a worker. Retain prior notices until it finishes.
        if lock.locked() or runtime.store.issue:
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
            if any(
                source.unrestricted_history_end is not None
                and datetime.fromisoformat(source.unrestricted_history_end) > datetime.now(UTC)
                for source in candidate.sources
            ):
                raise ValueError("Historical production confirmation cannot cover the future")
            if any(
                source.learning_start is not None
                and datetime.fromisoformat(source.learning_start) > datetime.now(UTC)
                for source in candidate.sources
            ):
                raise ValueError("Solar learning start cannot be in the future")
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid solar configuration") from None
        async with lock:
            try:
                membership.validate_membership("solar", candidate)
                membership.validate_panel_settings(
                    candidate, runtime.configuration, datetime.now(UTC)
                )
            except ValueError as error:
                raise web.HTTPBadRequest(
                    text=request_error_message(
                        error,
                        logger=_LOGGER,
                        operation="Solar configuration validation",
                        safe_messages=PUBLIC_VALIDATION_MESSAGES,
                        fallback="Cannot validate solar settings. Reload the Home Assistant Energy configuration before saving again.",
                    )
                ) from None
            current = runtime.configuration.version if runtime.configuration else None
            if body.get("expected_version") != current:
                raise web.HTTPConflict(text="Solar configuration changed. Reload before saving")
            if candidate.version != current:
                await persist(runtime.save_configuration, candidate, contract_input=False)
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
            report = None if error else parse_report(body)
            values = report.history if report else None
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid solar history") from None
        async with lock:
            pending = runtime.request(datetime.now(UTC))
            if pending is None or body.get("request_version") != pending["version"]:
                raise web.HTTPConflict(text="Solar history request changed")
            try:
                cancellation = await complete_write(
                    partial(
                        runtime.receive,
                        body["request_version"],
                        values,
                        datetime.now(UTC),
                        error=error,
                        report=report,
                    )
                )
                if cancellation:
                    raise cancellation
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid solar history source/range") from None
        return await snapshot(request)

    async def feedback(request):
        """Accept only configured feedback through the authenticated Home Assistant peer route."""
        try:
            try:
                body = await request.clone(client_max_size=MAX_FEEDBACK_BODY_BYTES).json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise web.HTTPBadRequest(text="Invalid solar curtailment feedback JSON") from None
            async with lock:
                try:
                    cancellation = await complete_write(
                        runtime.receive_feedback, body, datetime.now(UTC)
                    )
                except FeedbackRequestError:
                    raise web.HTTPBadRequest(
                        text="Invalid or outdated solar curtailment feedback"
                    ) from None
                if cancellation:
                    raise cancellation
            return await settings(request)
        except web.HTTPException:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize unknown failures even after commit.
            raise web.HTTPServiceUnavailable(
                text=request_error_message(
                    error,
                    logger=_LOGGER,
                    operation="Solar curtailment feedback update",
                    fallback="Curtailment feedback could not be updated or displayed. Retained evidence is preserved. Check storage and retry the background refresh.",
                )
            ) from None

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
        events = app.get(EVENTS)

        async def update():
            """Install only results matching current immutable inputs, with bounded retry on failure."""
            while True:
                job = None
                activity = None
                try:
                    async with lock:
                        recovery = runtime.feedback_restore_job(datetime.now(UTC))
                    if recovery:
                        recovered = await asyncio.to_thread(
                            runtime.read_feedback, recovery[1], datetime.now(UTC)
                        )
                        async with lock:
                            runtime.install_feedback_restore(recovery, recovered)
                    async with lock:
                        job = runtime.job(datetime.now(UTC))
                        context = activity_context(job[1]) if job else None
                    if job:
                        activity = (
                            await events.start(
                                "solar", "training", "Update solar forecast", context=context
                            )
                            if events
                            else None
                        )
                        result = await worker(runtime, job)
                        async with lock:
                            installed = runtime.install(result, datetime.now(UTC))
                            if installed:
                                if runtime.forecast is not None:
                                    forecast_logger.success("A new solar forecast is available.")
                                if live:
                                    live.refresh()
                            if activity:
                                usable = installed and forecast_usable(
                                    runtime.configuration,
                                    runtime.forecast,
                                    runtime.location,
                                    datetime.now(UTC),
                                )
                        if activity:
                            await events.finish(
                                activity,
                                Outcome.COMPLETED
                                if usable
                                else Outcome.FAILED
                                if installed
                                else Outcome.SKIPPED,
                                "A new solar forecast is available."
                                if usable
                                else "No new usable solar forecast was installed. Existing forecasts retain their original expiry.",
                                usable=usable,
                            )
                            events.availability(
                                "solar",
                                Health.AVAILABLE if usable else Health.DEGRADED,
                                "Solar forecast available."
                                if usable
                                else "Check solar forecast validity.",
                            )
                except Exception:
                    if events and job and activity in events.running:
                        await events.finish(
                            activity,
                            Outcome.FAILED,
                            "Solar forecast update failed. Retaining only valid forecasts. Planning still requires usable solar data.",
                        )
                    async with lock:
                        failure_recorded = runtime.failed(job, datetime.now(UTC)) if job else False
                        retry_seconds = (
                            (runtime.retry_after - datetime.now(UTC)).total_seconds()
                            if failure_recorded
                            and runtime.retry_after
                            and runtime.retry_after > datetime.now(UTC)
                            else SOLAR_WORKER_POLL_SECONDS
                        )
                    forecast_logger.exception(
                        "Retaining only valid forecasts. Planning still requires usable solar data.",
                        retry_seconds=max(SOLAR_WORKER_POLL_SECONDS, round(retry_seconds)),
                    )
                if events:
                    async with lock:
                        current = datetime.now(UTC)
                        usable = bool(
                            runtime.configuration
                            and runtime.configuration.sources
                            and not runtime.store.issue
                            and forecast_usable(
                                runtime.configuration, runtime.forecast, runtime.location, current
                            )
                        )
                        no_sources = bool(
                            runtime.configuration
                            and not runtime.configuration.sources
                            and not runtime.store.issue
                        )
                        availability = (
                            Health.AVAILABLE
                            if usable or no_sources
                            else Health.UNAVAILABLE
                            if runtime.configuration
                            else Health.UNKNOWN
                        )
                        detail = (
                            "No solar forecast is required for this installation."
                            if no_sources
                            else "A current solar forecast is available."
                            if usable
                            else "Waiting for a usable solar forecast."
                        )
                        retry = (
                            runtime.retry_after
                            if not no_sources
                            and runtime.retry_after
                            and runtime.retry_after > current
                            and runtime.failed_key == runtime.key(current)
                            else None
                        )
                        due = next_update(current, retry) if usable or retry else None
                        context = activity_context(runtime.configuration)
                    events.availability("solar", availability, detail)
                    events.scheduled(
                        "solar",
                        "training",
                        "Update solar forecast",
                        due,
                        context=context,
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
                events = app.get(EVENTS)

                async def update_weather():
                    """Fetch only for configured PV and the latest HA location, independent of solver cadence."""
                    last_location, due = None, datetime.min.replace(tzinfo=UTC)
                    last_history_start = None
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
                            history_start = oldest_history()
                            if history_start != last_history_start:
                                last_history_start, due = history_start, now
                        if configured and location and now >= due:
                            due = now + timedelta(minutes=WEATHER_RETRY_MINUTES)
                            activity = (
                                await events.start(
                                    "solar-weather",
                                    "weather",
                                    "Fetch solar weather",
                                    context={"label": "Solar production"},
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
                                        and runtime.configuration
                                        and runtime.configuration.sources
                                    ):
                                        runtime.set_weather(
                                            location,
                                            weather,
                                            service=source.service
                                            if isinstance(source, OpenMeteoSolarSource)
                                            else None,
                                        )
                                        if isinstance(source, OpenMeteoSolarSource):
                                            runtime.set_forecast_runs(
                                                location, source.forecast_runs
                                            )
                                        installed = True
                                        if weather:
                                            weather_logger.success(
                                                f"Received {len(weather)} weather hours. Next refresh in {WEATHER_REFRESH_HOURS * MINUTES_PER_HOUR:g} minutes."
                                            )
                                        else:
                                            weather_logger.unavailable(
                                                "Retaining only valid forecasts. Planning still requires usable solar data.",
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
                                        "Solar weather was discarded because the installation changed."
                                        if not installed
                                        else "Solar weather received."
                                        if weather
                                        else "No usable solar weather returned. Retaining only valid forecasts. Planning still requires usable solar data.",
                                        usable=installed and bool(weather),
                                    )
                                    if installed:
                                        events.availability(
                                            "solar-weather",
                                            Health.AVAILABLE if weather else Health.DEGRADED,
                                            "Solar weather available."
                                            if weather
                                            else "No new usable solar weather. Check forecast validity.",
                                        )
                            except Exception as error:
                                weather_logger.exception(
                                    "Retaining only valid forecasts. Planning still requires usable solar data.",
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
                                        "Solar weather refresh failed. Retaining only valid forecasts. Planning still requires usable solar data.",
                                    )
                                    events.availability(
                                        "solar-weather",
                                        Health.DEGRADED,
                                        "Solar weather refresh failed. Check solar forecast validity.",
                                    )
                        if events:
                            if configured and location:
                                events.scheduled(
                                    "solar-weather",
                                    "weather",
                                    "Fetch solar weather",
                                    due,
                                    context={"label": "Solar production"},
                                )
                            else:
                                events.withdraw("solar-weather")
                        await asyncio.sleep(SOLAR_WORKER_POLL_SECONDS)

                async def update_original_runs():
                    """Import small historical original-run batches independently of current forecast publication."""
                    history_logger = BackgroundLog(_LOGGER, "Historical solar forecast refresh")
                    while True:
                        now = datetime.now(UTC)
                        async with lock:
                            location = live.report.location if live.report else None
                            configured = bool(
                                runtime.configuration and runtime.configuration.sources
                            )
                        if configured and location:
                            try:
                                runs = await source.import_runs(location, now)
                                async with lock:
                                    if (
                                        live.report
                                        and live.report.location == location
                                        and runtime.configuration
                                        and runtime.configuration.sources
                                    ):
                                        runtime.set_forecast_runs(location, runs)
                            except Exception as error:
                                history_logger.exception(
                                    "Current solar forecasts remain available. Missing historical original forecasts are retried after the import backoff.",
                                    expected=isinstance(error, ValueError),
                                    reason="invalid original forecast data"
                                    if isinstance(error, ValueError)
                                    else None,
                                )
                        await asyncio.sleep(SOLAR_WORKER_POLL_SECONDS)

                task = asyncio.create_task(update_weather())
                history_task = (
                    asyncio.create_task(update_original_runs())
                    if isinstance(source, OpenMeteoSolarSource)
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
    app.router.add_get("/v1/solar", settings)
    app.router.add_put("/v1/solar", configure)
    app.router.add_post("/v1/solar/history", history)
    app.router.add_post("/v1/solar/feedback", feedback)
    app.router.add_post("/v1/solar/ha-forecasts", ha_forecasts)
