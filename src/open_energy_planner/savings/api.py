"""Authenticated settlement ingress and read-only app overview. Calculations remain app-owned."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from aiohttp import web

from open_energy_planner.core.units import BYTES_PER_KIB
from open_energy_planner.history.wire import parse_history
from open_energy_planner.home_assistant.names import display_plan
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog

from ..storage.adapters.documents import SqlDocuments
from ..storage.writes import complete_write
from .evidence import evidence
from .forecast import comparison
from .repricing import PlanValuations
from .runtime import SavingsRuntime, quarter
from .scenarios import scenarios
from .storage.adapters.sqlalchemy import SettlementStore

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

    valuations = PlanValuations(runtime.documents, plans)

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
                raise web.HTTPBadRequest(text=str(error)) from None
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
            body = await request.clone(client_max_size=BYTES_PER_KIB * BYTES_PER_KIB).json()
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
            ):
                runtime.bonus.issue = "Historical bonus history unavailable: " + body["error"]
                return web.json_response({"accepted": False})
            try:
                history = parse_history(body)
                cancelled = await complete_write(
                    runtime.bonus.receive, pending, history, datetime.now(UTC)
                )
                if cancelled:
                    raise cancelled
            except (ValueError, TypeError, KeyError) as error:
                runtime.bonus.issue = "Historical bonus history is incomplete or invalid"
                raise web.HTTPBadRequest(text=str(error)) from None
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
            result = await asyncio.to_thread(
                runtime.overview, start, end, request.query.get("revision")
            )
            plan_names = {}
            for interval in result["intervals"]:
                plan_id = interval.get("plan_id")
                if plan_id and plan_id not in plan_names:
                    original = await asyncio.to_thread(plans.get_plan, plan_id)
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
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(
                text="Choose an increasing range of aligned, timezone-aware dates"
            ) from None
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def expected(request):
        """Return original advice and separate successful repricing without activation.

        Unavailable comparisons expose their comparison_issue through
        scenarios["self_consumption"]["issue"], never invented reference flows."""
        plan = await asyncio.to_thread(plans.get_plan, request.match_info["plan_id"])
        if plan is None:
            raise web.HTTPNotFound(text="Original plan not found")
        plan = display_plan(plan, catalogue.value)
        result = {
            "plan": plan,
            "evidence": await asyncio.to_thread(evidence, plan),
            **(await asyncio.to_thread(valuations.latest, plan["plan_id"])),
            "current_contract_version": contracts.current.version if contracts.current else None,
        }
        try:
            if plan.get("reference_forecast_issue"):
                result["comparison_issue"] = plan["reference_forecast_issue"]
            else:
                result["comparison"] = plan.get("reference_forecast") or await asyncio.to_thread(
                    comparison, plan
                )
        except (ValueError, KeyError, TypeError) as error:
            result["comparison_issue"] = str(error)
        result["scenarios"] = await asyncio.to_thread(scenarios, plan, result.get("comparison"))
        if result.get("comparison_issue"):
            result["scenarios"]["self_consumption"]["issue"] = result["comparison_issue"]
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
        while True:
            try:
                async with lock:
                    contract, book = contracts.current, counters.current
                async with mutation_lock:
                    cancelled = await complete_write(
                        runtime.process, contract, datetime.now(UTC), book
                    )
                    if cancelled:
                        raise cancelled
                    cancelled = await complete_write(
                        valuations.process, contract, book, datetime.now(UTC)
                    )
                    if cancelled:
                        raise cancelled
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
            except Exception:
                runtime.issue = "Settlement processing failed. Inspect application logs"
                runtime.attention = True
                last_state = runtime.issue, runtime.attention
                settlement_logger.exception(
                    "The last successful financial results are retained. Planning continues independently. Check settlement and storage diagnostics.",
                    retry_seconds=SAVINGS_WORKER_SECONDS,
                )
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
