"""Read-only accuracy views and authenticated existing-HA Recorder transport."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from aiohttp import web

from open_energy_planner.forecast_quality.bindings import bindings
from open_energy_planner.forecast_quality.history import HistoryArchive
from open_energy_planner.home_assistant.names import display_plan, energy_source_names, source_names
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog, request_error_message

from ..storage.adapters.documents import SqlDocuments
from ..storage.writes import complete_write
from .daily import (
    DAILY_CAPTURE_POLL_SECONDS,
    DailyForecastArchive,
    controlled_consumers,
    daily_evidence,
    day_window,
    forecast_context,
)
from .evidence import evidence
from .radiation import RadiationArchive, radiation_view

# Only these reviewed owned literals may cross the public error boundary.
PUBLIC_VALIDATION_MESSAGES = (
    "Expected bounded immutable hourly history",
    "Expected forecast-history object",
    "Expected normalized energy hours",
    "Export statistics decrease. Confirm a manual baseline",
    "Forecast history request changed",
    "Forecast history source or range changed",
    "Forecast measurement binding changed",
    "History boundary is not a completed hour",
    "History components do not match aggregate meter",
    "History must use UTC hour boundaries",
    "History requires a sensor identity",
    "Incomplete export history. Confirm a manual baseline",
    "Invalid energy history value",
    "Invalid export meter",
    "Invalid history component",
    "Invalid history components",
    "Invalid history fields",
    "Invalid history row",
    "Invalid history size",
    "Invalid history timestamp",
    "Price timestamps require a timezone",
    "Unordered or duplicate history",
    "Unsupported history unit/request",
)

MAX_HISTORY_BODY_BYTES = 1024 * 1024


def capture_sources(plan, demand, solar, catalogue, ev, thermal=None):
    """Freeze source identities outside the solver snapshot without altering its hash or old plans."""
    plan["forecast_quality_sources"] = bindings(
        demand.configuration, solar.configuration, demand.consumers.selected, catalogue
    )
    entities = {row["entity_id"] for row in plan["forecast_quality_sources"]}
    plan["forecast_quality_names"] = {
        entity: name for entity, name in source_names(catalogue).items() if entity in entities
    }
    plan["display_sources"] = {
        **{
            row["source_id"]: row["entity_id"]
            for row in plan.get("comparison_bindings", {}).get("solar", ())
        },
        **{
            row["device_id"]: row["energy_entity"]
            for row in plan.get("comparison_bindings", {}).get("batteries", ())
        },
        **{row["id"]: row["energy_entity"] for row in ev.state["points"]},
        **{row["id"]: row["energy_entity"] for row in (thermal.state["stores"] if thermal else ())},
    }
    problem = plan["input_snapshot"]["problem"]
    active_points = {row["point_id"] for row in problem.get("ev_work", [])}
    plan["forecast_quality_controlled"] = sorted(
        {row["entity_id"] for row in problem.get("appliances", [])}
        | {row["energy_entity"] for row in ev.state["points"] if row["id"] in active_points}
    )

    if thermal:
        active_stores = {row["device_id"] for row in problem.get("thermal_work", [])}
        plan["forecast_quality_controlled"] = sorted(
            set(plan["forecast_quality_controlled"])
            | {
                row["energy_entity"]
                for row in thermal.state["stores"]
                if row["id"] in active_stores
            }
        )


def register(app, plans, demand, solar, catalogue, lock, satellite_factory=None):
    """Reuse the active engine and paired integration, without opening another HA connection."""
    archive = HistoryArchive(SqlDocuments(plans.engine, "demand"))
    daily = DailyForecastArchive(archive.documents)
    radiation = RadiationArchive(SqlDocuments(plans.engine, "solar"))

    if satellite_factory is not None:

        async def satellite_lifecycle(_app):
            """Keep optional satellite fetching independent of planning weather and its state lock."""
            async with satellite_factory() as source:
                radiation.source = source
                try:
                    yield
                finally:
                    radiation.source = None

        app.cleanup_ctx.append(satellite_lifecycle)

    from ..activities.api import EVENTS

    events = app.get(EVENTS)
    daily.events = events

    def timezone():
        """Use only the discovered Home Assistant timezone, never the server or browser timezone."""
        value = (catalogue.value or {}).get("timezone")
        if not value:
            raise web.HTTPServiceUnavailable(text="Home Assistant timezone unavailable")
        return value

    async def capture_lifecycle(_app):
        """Observe midnight independently of solver success and finish durable writes before shutdown."""
        capture_logger = BackgroundLog(logging.getLogger(__name__), "Daily forecast capture")

        async def capture():
            """Retain the last pre-midnight reference set, with failures isolated from planning."""
            while True:
                try:
                    async with lock:
                        zone = (catalogue.value or {}).get("timezone")
                        if zone:
                            now = datetime.now(UTC)
                            context = forecast_context(
                                demand, solar, catalogue.value, plans.state.get("plan")
                            )
                    if zone:
                        if events:
                            from zoneinfo import ZoneInfo

                            local_day = now.astimezone(ZoneInfo(zone)).date()
                            next_midnight = datetime.combine(
                                local_day + timedelta(days=1), datetime.min.time(), ZoneInfo(zone)
                            )
                            events.scheduled(
                                "forecast-quality",
                                "quality",
                                "Archive daily forecast",
                                next_midnight,
                            )
                        cancellation = await complete_write(daily.observe, now, zone, context)
                        if cancellation:
                            raise cancellation
                        capture_logger.success(
                            "The daily forecast watcher is running. Baselines are archived only at an observed midnight."
                        )
                except Exception:
                    capture_logger.exception(
                        "Daily forecast retention is delayed. Planning continues. Check archive storage if this persists.",
                        retry_seconds=DAILY_CAPTURE_POLL_SECONDS,
                    )
                await asyncio.sleep(DAILY_CAPTURE_POLL_SECONDS)

        task = asyncio.create_task(capture())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(capture_lifecycle)

    def pending(now):
        """Capture current authorized metering identities while the caller holds the app state lock."""
        sources = bindings(
            demand.configuration, solar.configuration, demand.consumers.selected, catalogue.value
        )
        return archive.request(now, sources)

    async def receive(request):
        """Complete one validated durable receipt before acknowledging or propagating cancellation."""
        try:
            body = await request.clone(client_max_size=MAX_HISTORY_BODY_BYTES).json()
            if not isinstance(body, dict):
                raise TypeError("Expected forecast-history object")
            async with lock:
                now = datetime.now(UTC)
                expected = pending(now)
                if expected is None or body.get("request_version") != expected["version"]:
                    raise web.HTTPConflict(text="Forecast history request changed")
                cancellation = await complete_write(archive.receive, expected, body, now)
                if cancellation:
                    raise cancellation
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise web.HTTPBadRequest(
                text=request_error_message(
                    error,
                    logger=logging.getLogger(__name__),
                    operation="Forecast history",
                    safe_messages=PUBLIC_VALIDATION_MESSAGES,
                    fallback="Forecast history could not be accepted. Refresh the requested history and inspect application diagnostics.",
                )
            ) from None
        return web.json_response({"accepted": True})

    async def view(request):
        """Read original forecast evidence. Current model status is a separately labeled observation."""
        original = await asyncio.to_thread(plans.get_plan, request.match_info["plan_id"])
        if original is None:
            raise web.HTTPNotFound(text="Original plan not found")
        now = datetime.now(UTC)
        try:
            result = await asyncio.to_thread(
                evidence,
                display_plan(original, catalogue.value),
                archive,
                demand.store.documents,
                solar.store.documents,
                now,
                names=energy_source_names(catalogue.value),
            )
        except (OSError, KeyError, TypeError, ValueError):
            raise web.HTTPServiceUnavailable(text="Forecast-quality evidence unavailable") from None
        for source in result["series"]:
            source.pop("weather_location", None)
        return await response_with_models(result)

    async def response_with_models(result):
        """Keep current model readiness separate from the immutable evaluated prediction."""
        async with lock:
            result.setdefault("timezone", (catalogue.value or {}).get("timezone"))
            result["current_models"] = {
                "household": {
                    "model": demand.forecast.model if demand.forecast else None,
                    "status": demand.model_status,
                    "issue": demand.issue,
                },
                "consumers": demand.consumers.document(),
                "solar": {
                    **solar.document(datetime.now(UTC), series=False),
                    "source_names": {
                        source.source_id: energy_source_names(catalogue.value).get(
                            source.entity_id, f"Solar {index + 1}"
                        )
                        for index, source in enumerate(
                            solar.configuration.sources if solar.configuration else ()
                        )
                    },
                },
            }
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def days(request):
        """Page retained HA calendar days without substituting the latest intraday plan."""
        async with lock:
            zone = timezone()
        before = request.query.get("before")
        try:
            if before is not None:
                day_window(before, zone)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid calendar day") from None
        try:
            result = await asyncio.to_thread(daily.page, datetime.now(UTC), zone, before)
        except (OSError, KeyError, TypeError, ValueError):
            raise web.HTTPServiceUnavailable(text="Daily forecast index unavailable") from None
        return await response_with_models(result)

    async def day_view(request):
        """Evaluate only the frozen midnight baseline and completed-day measurements."""
        async with lock:
            zone = timezone()
        try:
            day_window(request.match_info["day"], zone)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid calendar day") from None
        try:
            record = await asyncio.to_thread(daily.read, request.match_info["day"], zone)
            if record.get("context"):
                context = display_plan(record["context"], catalogue.value)
                labels = {**context["forecast_quality_names"], **context["comparison_names"]}
                context["expected_sources"] = [
                    {**row, "title": labels.get(row["id"], row["title"])}
                    for row in context.get("expected_sources", ())
                ]
                record = {**record, "context": context}
            result = await asyncio.to_thread(
                daily_evidence,
                record,
                archive,
                demand.store.documents,
                solar.store.documents,
                datetime.now(UTC),
                await asyncio.to_thread(
                    controlled_consumers,
                    plans,
                    datetime.fromisoformat(record["start"]),
                    datetime.fromisoformat(record["end"]),
                ),
                names=energy_source_names(catalogue.value),
            )
        except (OSError, KeyError, TypeError, ValueError):
            raise web.HTTPServiceUnavailable(text="Daily forecast evidence unavailable") from None
        start, end = (datetime.fromisoformat(record[key]) for key in ("start", "end"))
        now = datetime.now(UTC)
        for source in result["series"]:
            location = source.pop("weather_location", None)
            if source.get("scope") != "solar" or not location:
                continue
            original = source["radiation"]
            forecast = {
                datetime.fromisoformat(row["start"]): row["forecast_w_m2"]
                for row in original["rows"]
            }
            try:
                satellite, issue = await radiation.read(location, start, end, now)
                source["radiation"] = radiation_view(
                    forecast,
                    satellite,
                    start,
                    end,
                    now,
                    issue=": ".join(filter(None, (original["issue"], issue))) or None,
                )
            except (OSError, KeyError, TypeError, ValueError):
                source["radiation"] = {
                    **original,
                    "issue": "Satellite archive unavailable. Check application storage and refresh.",
                }
        return await response_with_models(result)

    app.router.add_post("/v1/forecast-quality/history", receive)
    app.router.add_get("/v1/forecast-quality/days", days)
    app.router.add_get("/v1/forecast-quality/days/{day}", day_view)
    app.router.add_get("/v1/forecast-quality/{plan_id}", view)
    return pending
