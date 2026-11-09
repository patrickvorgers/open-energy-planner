"""Authenticated settlement ingress and read-only app overview. Calculations remain app-owned."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from aiohttp import web

from open_energy_planner.contracts.bonus_statistics import (
    MAX_STATISTICS_REPORT_BYTES,
    parse_statistics,
)
from open_energy_planner.history.wire import parse_history
from open_energy_planner.home_assistant.names import display_plan, energy_source_names
from open_energy_planner.runtime.diagnostics.logging import (
    BackgroundLog,
    failure_trace,
    request_error_message,
)

from ..storage.adapters.documents import SqlDocuments
from ..storage.adapters.lifecycle.retention import recompress, thin
from ..storage.writes import complete_write
from .daily import TIMEZONE_PAUSE_MESSAGE, DailySavings, SavingsTimezoneChanged
from .evidence import evidence
from .forecast import comparison
from .repricing import PlanValuations
from .runtime import SavingsRuntime, quarter
from .scenarios import scenarios
from .storage.adapters.sqlalchemy import SettlementStore
from .windows import window_summary

# Only these reviewed owned literals may cross the public error boundary.
PUBLIC_VALIDATION_MESSAGES = (
    "Expected bounded immutable hourly history",
    "Expected normalized energy hours",
    "Export statistics decrease. Confirm a manual baseline",
    "Grid import measurement is required",
    "Grid settlement meters do not match Energy configuration",
    "History boundary is not a completed hour",
    "History components changed",
    "History components do not match aggregate meter",
    "History must use UTC hour boundaries",
    "History requires a sensor identity",
    "History source changed",
    "Incomplete export history. Confirm a manual baseline",
    "Invalid energy history value",
    "Invalid export meter",
    "Invalid history component",
    "Invalid history components",
    "Invalid history fields",
    "Invalid history row",
    "Invalid history size",
    "Invalid history timestamp",
    "Invalid settlement batch",
    "Invalid settlement channels",
    "Invalid settlement energy",
    "Invalid settlement frame",
    "Invalid settlement identity",
    "Invalid settlement interval",
    "Invalid settlement response",
    "Price timestamps require a timezone",
    "Settlement batch has gaps or overlaps",
    "Settlement channels do not match requested Energy bindings",
    "Settlement frame does not end on a requested quarter boundary",
    "Settlement request changed",
    "Settlement response outside requested coverage",
    "Unordered or duplicate history",
    "Unsupported history unit/request",
)

SAVINGS_WORKER_SECONDS = 30
_LOGGER = logging.getLogger(__name__)


def register(
    app,
    plans,
    contracts,
    counters,
    installation,
    solar,
    demand,
    lock,
    catalogue,
    location,
    *,
    mutation_lock: asyncio.Lock,
):
    """Compose bounded financial work and trusted HA transport on the app engine.

    The caller owns both locks: lock protects current app state. mutation_lock
    serializes financial writes/publication with price-cache acceptance. Provider
    fetching never holds that mutation lock. It is separate from control state
    so slow settlement cannot hold plan publication or HA snapshot reads.
    Return the capture-request callback and runtime for the composition root.
    """
    runtime = SavingsRuntime(
        SettlementStore(plans.engine), SqlDocuments(plans.engine, "savings"), plans
    )
    daily = DailySavings(runtime)

    valuations = PlanValuations(runtime.documents, plans)
    from ..activities.api import EVENTS

    runtime.events = app.get(EVENTS)
    valuations.events = app.get(EVENTS)

    def pending():
        """Capture currently requested immutable bindings. Historic frames retain their own context."""
        return runtime.request(
            datetime.now(UTC),
            installation.current,
            solar.configuration,
            demand.configuration,
            counters.current.seeds if counters.current else (),
            location(),
        )

    async def capture_request():
        """Keep all database reads outside HTTP event-loop execution."""
        try:
            return await asyncio.to_thread(pending)
        except Exception:
            runtime.issue = "Settlement capture unavailable. Inspect application logs"
            runtime.attention = True
            _LOGGER.exception("Settlement request failed. Planning remains independent")
            return None

    async def receive(request):
        """Validate the current peer request before saving a bounded batch. Never control hardware."""
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="Settlement body must be JSON") from None
        async with mutation_lock:
            async with lock:
                expected = await capture_request()
                seeds = counters.current.seeds if counters.current else ()
                site = location()
            if expected is None:
                raise web.HTTPConflict(text="No settlement capture is pending")
            try:
                cancelled = await complete_write(
                    runtime.receive, expected, body, datetime.now(UTC), seeds, site
                )
                if cancelled:
                    raise cancelled
            except (ValueError, TypeError, KeyError) as error:
                raise web.HTTPBadRequest(
                    text=request_error_message(
                        error,
                        logger=logging.getLogger(__name__),
                        operation="Settlement history",
                        safe_messages=PUBLIC_VALIDATION_MESSAGES,
                        fallback="Settlement history could not be accepted. Refresh the requested history and inspect application diagnostics.",
                    )
                ) from None
        return web.json_response({"accepted": True})

    def bonus_history_request():
        """Expose the worker's immutable historical request only while its contract/counter versions match."""
        pending = runtime.bonus.pending
        if (
            pending is None
            or contracts.current is None
            or pending.contract_version != contracts.current.version
            or pending.counter_version != (counters.current.version if counters.current else None)
        ):
            return None
        return {"version": pending.version, **pending.document()}

    async def receive_bonus_history(request):
        """Accept bounded native history for an old period, without mutating current confirmations.

        The version binds the historical source members, frame boundary, contract
        and current counter revision. Validation and persistence run off-loop under
        the same mutation lock as financial replay. Missing hours stay missing.
        """
        try:
            body = await request.clone(client_max_size=MAX_STATISTICS_REPORT_BYTES).json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Historical bonus body must be JSON") from None
        async with mutation_lock:
            pending = runtime.bonus.pending
            expected = bonus_history_request()
            if (
                expected is None
                or not isinstance(body, dict)
                or body.get("request_version") != expected["version"]
            ):
                raise web.HTTPConflict(text="Historical bonus request changed")
            if set(body) == {"request_version", "error"} and body["error"] in (
                "recorder_unavailable",
                "history_unavailable",
                "unsupported_statistics",
                "history_missing",
            ):
                runtime.bonus.issue = "Historical bonus history unavailable: " + body["error"]
                return web.json_response({"accepted": False})
            try:
                if pending.statistics_sum:
                    cancelled = await complete_write(
                        runtime.bonus.receive_statistics,
                        pending,
                        parse_statistics(body, pending),
                        counters.current,
                        datetime.now(UTC),
                    )
                else:
                    history = parse_history(body)
                    cancelled = await complete_write(
                        runtime.bonus.receive, pending, history, datetime.now(UTC)
                    )
                if cancelled:
                    raise cancelled
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                runtime.bonus.issue = "Historical bonus history is incomplete or invalid"
                raise web.HTTPBadRequest(
                    text=request_error_message(
                        error,
                        logger=logging.getLogger(__name__),
                        operation="Settlement history",
                        safe_messages=PUBLIC_VALIDATION_MESSAGES,
                        fallback="Settlement history could not be accepted. Refresh the requested history and inspect application diagnostics.",
                    )
                ) from None
        return web.json_response({"accepted": True})

    async def overview(request):
        """Read one explicitly bounded period without computing or resetting comparison state."""
        try:
            now = datetime.now(UTC)
            start = datetime.fromisoformat(
                request.query.get("start", quarter(now).replace(hour=0, minute=0).isoformat())
            )
            end = datetime.fromisoformat(
                request.query.get(
                    "end", (quarter(now).replace(hour=0, minute=0) + timedelta(days=1)).isoformat()
                )
            )
            if (
                start.utcoffset() is None
                or end.utcoffset() is None
                or end <= start
                or start != quarter(start)
                or end != quarter(end)
            ):
                raise ValueError
            use_daily = not request.query.get("revision") and await asyncio.to_thread(
                runtime.documents.exists, "daily-progress.json"
            )
            if use_daily:
                result = await asyncio.to_thread(daily.overview, start, end)
                current_names = energy_source_names(catalogue.value)
                for day in result["days"]:
                    day["device_names"] = {
                        **day.get("device_names", {}),
                        **{
                            identity: current_names[entity]
                            for identity, entity in day.get("display_sources", {}).items()
                            if entity in current_names
                        },
                    }
                return web.json_response(result, headers={"Cache-Control": "no-store"})
            result = await asyncio.to_thread(
                runtime.overview, start, end, request.query.get("revision")
            )
            plan_names = {}
            for interval in result["intervals"]:
                plan_id = interval.get("plan_id")
                if plan_id and plan_id not in plan_names:
                    original = await asyncio.to_thread(plans.get_context, plan_id)
                    plan_names[plan_id] = (display_plan(original, catalogue.value) or {}).get(
                        "comparison_names", {}
                    )
                interval["device_names"] = {
                    **interval.get("device_names", {}),
                    **plan_names.get(plan_id, {}),
                }
        except FileNotFoundError:
            raise web.HTTPNotFound(text="Evaluation version not found") from None
        except OSError:
            raise web.HTTPServiceUnavailable(text="Settlement archive unavailable") from None
        except (ValueError, TypeError) as error:
            raise web.HTTPBadRequest(
                text=request_error_message(
                    error,
                    logger=_LOGGER,
                    operation="Daily savings period",
                    safe_messages=("Choose a period with one historical currency",),
                    fallback="Choose an increasing range of aligned, timezone-aware dates",
                )
            ) from None
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def expected(request):
        """Read original comparisons or a financial period without activating advice.

        Without period parameters, return the original plan, evidence, scenarios
        and separate successful repricing. An unavailable comparison exposes its
        comparison_issue through scenarios["self_consumption"]["issue"], never
        invented reference flows.

        Supplying either query bound requires both start and end as increasing,
        timezone-aware ISO timestamps. This mode returns only window_summary's
        period, scenario amounts/issues and original valuation basis. It omits
        the plan, evidence and separate valuation data.

        Raises:
            web.HTTPBadRequest: A period bound is missing, invalid, naive or
                non-increasing (400).
            web.HTTPNotFound: The selected original plan does not exist (404).
            web.HTTPServiceUnavailable: Period calculation cannot read usable
                original evidence (503). Its explanation never exposes raw errors.
        """
        window = None
        if "start" in request.query or "end" in request.query:
            try:
                start, end = (
                    datetime.fromisoformat(request.query[key]) for key in ("start", "end")
                )
                if start.utcoffset() is None or end.utcoffset() is None or start >= end:
                    raise ValueError
                window = start, end
            except (KeyError, ValueError):
                raise web.HTTPBadRequest(
                    text="Choose an increasing, timezone-aware planning period"
                ) from None
        plan = await asyncio.to_thread(plans.get_plan, request.match_info["plan_id"])
        if plan is None:
            raise web.HTTPNotFound(text="Original plan not found")
        plan = display_plan(plan, catalogue.value)
        result = {}
        if not window:
            result = {
                "plan": plan,
                "evidence": await asyncio.to_thread(evidence, plan),
                **(await asyncio.to_thread(valuations.latest, plan["plan_id"])),
                "current_contract_version": contracts.current.version
                if contracts.current
                else None,
            }
        try:
            if plan.get("reference_forecast_issue"):
                result["comparison_issue"] = plan["reference_forecast_issue"]
            else:
                result["comparison"] = plan.get("reference_forecast") or await asyncio.to_thread(
                    comparison, plan
                )
        except (ValueError, KeyError, TypeError) as error:
            result["comparison_issue"] = request_error_message(
                error,
                logger=_LOGGER,
                operation="Original plan comparison",
                fallback="Original plan comparison is unavailable. Inspect application diagnostics. Measure-based settlement remains independent.",
            )
        result["scenarios"] = await asyncio.to_thread(scenarios, plan, result.get("comparison"))
        if result.get("comparison_issue"):
            result["scenarios"]["self_consumption"]["issue"] = result["comparison_issue"]
        if window:
            try:
                result = await asyncio.to_thread(window_summary, plan, result["scenarios"], *window)
            except (ValueError, KeyError, TypeError, OverflowError) as error:
                raise web.HTTPServiceUnavailable(
                    text=request_error_message(
                        error,
                        logger=_LOGGER,
                        operation="Planning period costs",
                        fallback="Expected costs for this period are unavailable. Inspect application diagnostics.",
                    )
                ) from None
        import json

        from ..planning import encode

        return web.Response(
            text=json.dumps(result, default=encode, allow_nan=False),
            content_type="application/json",
        )

    async def worker():
        """Advance at most one day per round. Keep errors visible without stopping planning."""
        settlement_logger = BackgroundLog(_LOGGER, "Settlement processing")
        last_state = None
        last_maintenance_failure = None
        while True:
            financial_ready = False
            try:
                async with lock:
                    contract, book, site = contracts.current, counters.current, location()
                async with mutation_lock:
                    cancelled = await complete_write(
                        daily.process, contract, book, datetime.now(UTC), site
                    )
                    if cancelled:
                        raise cancelled
                    cancelled = await complete_write(
                        valuations.process, contract, book, datetime.now(UTC)
                    )
                    if cancelled:
                        raise cancelled
                financial_ready = True
                state = runtime.issue, runtime.attention
                if state != last_state:
                    if runtime.issue:
                        _LOGGER.log(
                            logging.WARNING if runtime.attention else logging.INFO,
                            "Settlement %s: %s. Planning continues independently. Next check in %d seconds.",
                            "needs attention" if runtime.attention else "waiting",
                            runtime.issue,
                            SAVINGS_WORKER_SECONDS,
                        )
                    elif last_state is not None and last_state[0] is not None:
                        _LOGGER.info(
                            "Settlement evidence is usable again. Chronological processing continues."
                        )
                    last_state = state
                if runtime.issue is None:
                    settlement_logger.success(
                        "Measured settlement processing is running independently of planning."
                    )
            except SavingsTimezoneChanged:
                runtime.issue, runtime.attention = TIMEZONE_PAUSE_MESSAGE, True
                if last_state != (runtime.issue, runtime.attention):
                    _LOGGER.warning("Settlement paused: %s", TIMEZONE_PAUSE_MESSAGE)
                last_state = runtime.issue, runtime.attention
            except Exception:
                runtime.issue = "Settlement processing failed. Inspect application logs"
                runtime.attention = True
                last_state = runtime.issue, runtime.attention
                settlement_logger.exception(
                    "The last successful financial results are retained. Planning continues independently. Check settlement and storage diagnostics.",
                    retry_seconds=SAVINGS_WORKER_SECONDS,
                )
            if financial_ready:
                try:
                    async with mutation_lock:
                        if site:
                            cancelled = await complete_write(
                                thin, plans, runtime.documents, datetime.now(UTC), site.timezone
                            )
                            if cancelled:
                                raise cancelled
                        cancelled = await complete_write(recompress, runtime.documents)
                        if cancelled:
                            raise cancelled
                    if last_maintenance_failure is not None:
                        _LOGGER.info(
                            "Archive maintenance recovered. Bounded background processing continues."
                        )
                    last_maintenance_failure = None
                except Exception as error:  # noqa: BLE001 - Isolate maintenance failures and log only sanitized traces.
                    trace = failure_trace(error)
                    failure = type(error), trace
                    _LOGGER.log(
                        logging.ERROR if failure != last_maintenance_failure else logging.DEBUG,
                        "Archive maintenance failed (%s). Unprocessed records remain retained. Daily results and planning continue independently. Inspect storage diagnostics. Next check in %d seconds.\n%s",
                        type(error).__name__,
                        SAVINGS_WORKER_SECONDS,
                        trace if failure != last_maintenance_failure else "",
                    )
                    last_maintenance_failure = failure
            await asyncio.sleep(SAVINGS_WORKER_SECONDS)

    async def evaluations(request):
        """Return one bounded page of original/corrected financial evaluation descriptors."""
        try:
            versions, before = await asyncio.to_thread(
                runtime.evaluation_versions, request.query.get("before")
            )
        except FileNotFoundError:
            raise web.HTTPNotFound(text="Evaluation version not found") from None
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid evaluation cursor") from None
        return web.json_response({"versions": versions, "next_evaluation": before})

    async def lifecycle(_app):
        """Cancel only between drained off-loop operations before the shared engine is disposed."""
        task = asyncio.create_task(worker())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app.cleanup_ctx.append(lifecycle)
    app.router.add_post("/v1/settlement", receive)
    app.router.add_post("/v1/settlement/bonus-history", receive_bonus_history)
    app.router.add_get("/v1/savings", overview)
    app.router.add_get("/v1/evaluations", evaluations)
    app.router.add_get("/v1/plans/{plan_id}/comparison", expected)
    return capture_request, runtime, bonus_history_request
