"""Composition root for isolated shadow planning and realized-command replay."""

import logging
from dataclasses import replace
from datetime import timedelta

from open_energy_planner.optimization.adapters.process.worker import (
    WORKER_OVERHEAD_SECONDS,
    configure_worker_threads,
)
from open_energy_planner.runtime.diagnostics.logging import configure_logging

from ..installation.basis import EnergySeries
from ..installation.commands import plan_document
from ..installation.validation import validate
from ..optimization.domain import DEFAULT_REPLAY_TIME_LIMIT_SECONDS, SolveOptions, Status
from ..optimization.export_bonus import ExportBonus, settle
from ..planning import InputSnapshot
from ..savings.context import physical_context
from ..savings.model import SETTLEMENT_INTERVAL, PendingSettlement
from ..savings.runtime import quarter
from .evaluation import score
from .policy import COMPARISON_SCENARIOS, MAX_REALIZATION_FRAMES, SELECTION_POLICY

SHADOW_SOLVE_TIME_LIMIT_SECONDS = 10
SHADOW_CAPTURE_TIMEOUT_SECONDS = (
    len(COMPARISON_SCENARIOS) * SHADOW_SOLVE_TIME_LIMIT_SECONDS + WORKER_OVERHEAD_SECONDS
)
SHADOW_EVALUATION_TIMEOUT_SECONDS = (
    len(COMPARISON_SCENARIOS) * MAX_REALIZATION_FRAMES * DEFAULT_REPLAY_TIME_LIMIT_SECONDS
    + WORKER_OVERHEAD_SECONDS
)


def capture_pair(original, variant, now):
    """Solve both models before realization, sharing original physics and all other forecasts.

    Start at the next complete quarter with the original plan's projected stock
    and bonus counters at that boundary, equal for both variants. This deliberately
    omits the initial partial interval instead of fabricating fine metering.
    """
    from ..optimization.adapters.highs import HighsSolver

    controlled = original["input_snapshot"]["problem"]
    if controlled.get("ev_work") or controlled.get("appliances") or controlled.get("thermal_work"):
        raise ValueError("Controlled-load realization is required for new model comparisons")
    problem = physical_context(original)
    start = quarter(now) + SETTLEMENT_INTERVAL
    indices = [index for index, step in enumerate(problem.intervals) if step.start >= start]
    if not indices or problem.intervals[indices[0]].start != start:
        raise ValueError("A current original plan with a full-quarter boundary is required")
    first = indices[0]
    if not first:
        raise ValueError("Opening projected stock boundary is missing")
    stored = {
        row["device_id"]: row["end_kwh"]
        for row in original["expected_flows"][first - 1]["batteries"]
    }
    bonuses, opening = [], {}
    for definition in original["input_snapshot"]["problem"].get("export_bonuses", ()):
        rule = ExportBonus(
            **{
                **definition,
                **{key: tuple(definition[key]) for key in ("rates", "caps_kwh", "periods")},
                "prevent_battery_export": tuple(definition.get("prevent_battery_export", ())),
            }
        )
        prior = settle(rule, tuple(row["grid_export"] for row in original["expected_flows"]))
        initial = (
            prior.counters_kwh[first - 1] if rule.periods[first] == rule.periods[first - 1] else 0
        )
        bonuses.append(
            replace(
                rule,
                initial_kwh=initial,
                rates=rule.rates[first:],
                caps_kwh=rule.caps_kwh[first:],
                periods=rule.periods[first:],
                prevent_battery_export=rule.prevent_battery_export[first:],
            )
        )
        opening[rule.rule_id + "|" + rule.periods[first]] = initial
    physical = replace(
        problem,
        intervals=problem.intervals[first:],
        batteries=tuple(
            replace(unit, battery=replace(unit.battery, initial_kwh=stored[unit.device_id]))
            for unit in problem.batteries
        ),
        export_bonuses=tuple(bonuses),
    )
    result = {
        "policy": SELECTION_POLICY,
        "original_plan_id": original["plan_id"],
        "issued_at": now.isoformat(),
        "start": start.isoformat(),
        "end": (
            physical.intervals[-1].start + timedelta(hours=physical.intervals[-1].duration_h)
        ).isoformat(),
        "family_id": variant["family_id"],
        "kind": variant["kind"],
        "source_id": variant.get("source_id"),
        "strategy": original["strategy"],
        "contract_version": original["input_snapshot"]["contract_version"],
        "start_stored": stored,
        "opening_counters": opening,
    }
    for scenario in COMPARISON_SCENARIOS:
        forecast = variant[scenario]
        if forecast.issued_at > now:
            raise ValueError("Future forecast knowledge is forbidden")
        rows = (
            forecast.steps
            if variant["kind"] == "demand"
            else tuple(row for row in forecast.energies if row.source_id == variant["source_id"])
        )
        series = EnergySeries(rows, "kwh" if variant["kind"] == "demand" else "dc_kwh")
        intervals = []
        for step in physical.intervals:
            amount = series.integral(step.start, step.start + timedelta(hours=step.duration_h))
            if variant["kind"] == "demand":
                intervals.append(replace(step, load_kwh=amount))
            else:
                pv = dict(step.pv_sources)
                pv[variant["source_id"]] = amount
                intervals.append(
                    replace(step, pv_kwh=sum(pv.values()), pv_sources=tuple(pv.items()))
                )
        inputs = replace(physical, intervals=tuple(intervals))
        solved = HighsSolver().solve(
            inputs, SolveOptions(SHADOW_SOLVE_TIME_LIMIT_SECONDS, original["strategy"])
        )
        if solved.status != Status.OPTIMAL or solved.plan is None:
            raise ValueError("Both shadow plans require proved optimal solutions")
        costs = validate(inputs, solved.plan)
        snapshot = InputSnapshot(
            "shadow",
            original["config_revision"],
            now,
            "shadow",
            forecast.version,
            "frozen",
            inputs,
            strategy=original["strategy"],
        )
        document = plan_document(snapshot, solved, costs, now)
        result[scenario] = {
            "forecast_version": forecast.version,
            "model": forecast.model,
            "steps": document["steps"],
        }
    return result


def calculate_in_child(payload, sender):
    """Bound numerical work in one spawned process. Send plain evidence or an explicit failure."""
    configure_worker_threads()
    configure_logging()
    try:
        from ..optimization.adapters.highs.replay import dispatch

        if payload["operation"] == "capture":
            result = capture_pair(payload["original"], payload["variant"], payload["now"])
        elif payload["operation"] == "evaluate":
            result = score(
                payload["capture"],
                payload["original"],
                payload["records"],
                payload["market_rows"],
                payload["contract"],
                dispatch,
                payload["seeds"],
            )
        else:
            raise ValueError("Unknown model selection operation")
        sender.send({"status": "ok", "value": result})
    except (PendingSettlement, TimeoutError) as error:
        sender.send({"status": "pending", "issue": str(error)})
    except (ValueError, TypeError, KeyError) as error:
        sender.send({"status": "failed", "issue": str(error)})
    except Exception:
        logging.getLogger(__name__).exception("Shadow model calculation failed")
        sender.send(
            {"status": "failed", "issue": "Shadow calculation failed. Inspect application logs"}
        )
    finally:
        sender.close()
