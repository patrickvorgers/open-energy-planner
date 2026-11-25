"""Solar HTTP and background lifecycle composition, separate from forecast calculations."""

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial

from aiohttp import web

from open_energy_planner.activities.api import EVENTS
from open_energy_planner.activities.events import Health, Outcome
from open_energy_planner.core.units import BYTES_PER_KIB
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
from .wire import parse_configuration, validate_configuration_dates

SOLAR_WORKER_POLL_SECONDS = 5
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


def register(app, runtime, live, lock, persist, snapshot, membership):
    """Wire authenticated solar routes and its background model worker into the app."""

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
        """Read cached forecast availability without waiting for configuration work.

        Unreadable configuration or a held Solar configuration lock returns None.
        Neither state confirms recovery.
        """
        if not runtime.configuration_lock.acquire(blocking=False):
            return None
        try:
            if runtime.store.issue:
                return None
            return solar_forecast_notifications(
                runtime.configuration,
                runtime.forecast,
                live.report.location if live and live.report else None,
                datetime.now(UTC),
                runtime.issue,
            )
        finally:
            runtime.configuration_lock.release()

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
            validate_configuration_dates(candidate, datetime.now(UTC))
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid solar configuration") from None
        async with lock:
            try:
                membership.validate_membership("solar", candidate)
                membership.validate_panel_settings(
                    candidate, runtime.store.current, datetime.now(UTC)
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
            current = runtime.store.current.version if runtime.store.current else None
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
            if request.headers.get("X-OEP-View") == "commands":
                return web.json_response(
                    {"accepted": True, "diagnostics": runtime.history_diagnostics()}
                )
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
                        if runtime.store.current is not None and runtime.store.issue:
                            cancellation = await complete_write(
                                runtime.restore_committed_configuration
                            )
                            if cancellation is not None:
                                raise cancellation
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
                                    await live.refresh_async()
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
    app.router.add_get("/v1/solar", settings)
    app.router.add_put("/v1/solar", configure)
    app.router.add_post("/v1/solar/history", history)
    app.router.add_post("/v1/solar/feedback", feedback)
    app.router.add_post("/v1/solar/ha-forecasts", ha_forecasts)
