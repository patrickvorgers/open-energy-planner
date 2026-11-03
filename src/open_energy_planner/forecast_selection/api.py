"""Authenticated model status and a background lifecycle independent of real commands."""

import asyncio
import logging
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.forecast_quality.policy import FORECAST_MODELS
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog

from ..storage.adapters.documents import SqlDocuments
from ..storage.writes import complete_write
from .runtime import ModelSelection, variants
from .storage import SelectionStore

MODEL_WORKER_POLL_SECONDS = 30


def register(
    app,
    plans,
    settlements,
    contracts,
    counters,
    installation,
    demand,
    solar,
    strategy,
    lock,
    *,
    enabled,
):
    """Compose existing forecasts, SQL stores and native workers without adding another HA connection."""
    runtime = ModelSelection(
        SelectionStore(SqlDocuments(plans.engine, "planning")),
        plans,
        settlements,
        contracts,
        counters,
    )

    def current_choices():
        """Scope preferences to the current physical, contract, strategy and model-feature definitions."""
        if not enabled or not all(
            (installation.current, demand.configuration, solar.configuration, contracts.current)
        ):
            return []
        if (
            installation.issue
            or demand.store.issue
            or solar.store.issue
            or contracts.issue
            or counters.issue
            or strategy.issue
        ):
            return []
        return [
            row
            for row in variants(
                installation.current,
                demand,
                solar,
                contracts.current,
                strategy.current,
                counters.current.version if counters.current else None,
            )
            if row["kind"] == "demand"
        ]

    def apply_preferences():
        """Apply only current-family evidence. Keep baseline fallback immediate and model work off-loop."""
        choices = current_choices()
        chosen = [
            row
            for row in choices
            if runtime.preferences.get(row["family_id"]) and row["candidate"] is not None
        ]
        demand.select_learned(any(row["kind"] == "demand" for row in chosen))
        return choices

    async def state(_request):
        """Expose actual current model names separately from the validated candidate preference."""
        async with lock:
            choices = current_choices()
            active = {"demand": demand.forecast.model if demand.forecast else None}
            active.update(
                {
                    "solar:" + source.source_id: solar.active_model(source.source_id)
                    for source in solar.configuration.sources
                }
                if solar.configuration
                else {}
            )
            document = runtime.document(active)
            for row in document["models"]:
                row["active_model_label"] = FORECAST_MODELS.get(
                    row["active_model"], row["active_model"]
                )
            identities = {row["family_id"] for row in choices}
            document["models"] = [
                row for row in document["models"] if row["family_id"] in identities
            ]
            return web.json_response(document)

    def matches_current_plan(original):
        """Reject old physical/financial definitions while retaining their immutable historical evidence."""
        snapshot = original["input_snapshot"]
        problem = snapshot["problem"]
        current = installation.current
        converters = {
            row.inverter_id: asdict(row)
            for row in (*current.inverters, *solar.configuration.inverters)
        }
        return (
            snapshot.get("contract_version") == contracts.current.version
            and original["strategy"] == strategy.current
            and all(
                row["configuration_version"] == demand.configuration.version
                for row in snapshot.get("demand_evidence", ())
            )
            and all(
                row["configuration_version"] == solar.configuration.version
                for row in snapshot.get("solar_evidence", ())
            )
            and problem["grid_import_limit_w"] == current.import_limit_w
            and problem["grid_export_limit_w"] == current.export_limit_w
            and {row["inverter_id"]: row for row in problem["inverters"]} == converters
            and original["comparison_bindings"]["batteries"]
            == [asdict(row) for row in current.batteries]
            and all(
                row["counter_version"] == (counters.current.version if counters.current else None)
                for row in snapshot.get("bonus_evidence", ())
            )
        )

    async def worker():
        """Coalesce numerical evidence work. Ordinary measurements never cancel a captured pair."""
        comparison_logger = BackgroundLog(logging.getLogger(__name__), "Forecast model comparison")
        while True:
            try:
                async with lock:
                    choices = apply_preferences()
                    original = deepcopy(plans.state["plan"])
                    matched = bool(original and choices and matches_current_plan(original))
                if matched:
                    cancelled = await complete_write(
                        runtime.process, original, choices, datetime.now(UTC)
                    )
                    if cancelled:
                        raise cancelled
                    async with lock:
                        apply_preferences()
                    comparison_logger.success(
                        "Shadow comparison processing is running. Models change only after checked decision evidence."
                    )
            except Exception:
                comparison_logger.exception(
                    "Baseline models are retained. Planning continues independently. Check comparison and storage diagnostics.",
                    retry_seconds=MODEL_WORKER_POLL_SECONDS,
                )
                runtime.preferences = {}
                runtime.issue = "Model comparison unavailable. Inspect application logs"
                async with lock:
                    apply_preferences()
            await asyncio.sleep(MODEL_WORKER_POLL_SECONDS)

    async def lifecycle(_app):
        """Drain owned SQL/process work before the app releases its engine, including repeated cancellation."""
        task = asyncio.create_task(worker())
        try:
            yield
        finally:
            runtime.stop.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    if enabled:
        app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/v1/models", state)
    return apply_preferences
