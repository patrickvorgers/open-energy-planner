"""Revalue frozen plan flows under corrected contracts without rerunning an optimizer."""

import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.runtime.diagnostics.logging import request_error_message

from ..contracts.compilation import BonusCompiler
from ..contracts.export_meter import export_identity
from ..contracts.model import TariffEvidence
from ..installation.model import slice_interval
from ..installation.valuation import derive
from .accounting import bill, inventory
from .context import physical_context
from .forecast import comparison

REPRICING_BATCH_SIZE = 20
METHOD = "frozen-flow-contract-reprice-v1"
_LOGGER = logging.getLogger(__name__)
REPRICING_FAILURE_MESSAGE = (
    "Plan repricing evidence is unavailable. Check archived inputs and contract coverage. "
    "The last successful valuation is retained."
)


@dataclass(frozen=True)
class BillingWindow:
    """Temporal and initial-meter evidence shared by expected billing and measured interval billing."""

    start: datetime
    end: datetime
    meter_start: dict


def key(value):
    """Use bounded content identities for immutable valuation versions and lookup pointers."""
    return sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def corrected_tariffs(evidence, contract):
    """Keep original published/forecast wholesale knowledge and split only at corrected retail period boundaries."""
    result = []
    for item in evidence:
        row = dict(item)
        start, end = (
            datetime.fromisoformat(row[name]) if isinstance(row[name], str) else row[name]
            for name in ("start", "end")
        )
        cursor = start
        for index, period in enumerate(contract.periods):
            right = min(end, period.effective_until or end)
            if period.effective_from <= cursor < right:
                result.append(
                    TariffEvidence(
                        **{
                            **row,
                            "start": cursor,
                            "end": right,
                            "buy_price": period.import_formula.evaluate(row["market_price"]),
                            "sell_price": period.export_formula.evaluate(row["market_price"]),
                            "contract_version": contract.version,
                            "period_index": index,
                        }
                    )
                )
                cursor = right
        if cursor != end:
            raise ValueError("Corrected contract does not cover original plan prices")
    return tuple(result)


def initial_counters(plan, contract, book, original_reference):
    """Reuse original actual/reference budgets. New bonus families require historical seed evidence."""
    start = datetime.fromisoformat(plan["input_snapshot"]["problem"]["intervals"][0]["start"])
    actual = {}
    evidence = plan["input_snapshot"].get("bonus_evidence", ())
    components = [
        row
        for row in plan["input_snapshot"].get("measurement_evidence", ())
        if row.get("key") == "grid.export_energy"
        or row.get("key", "").startswith("grid.export_energy.")
    ]
    captured_at = plan["input_snapshot"].get("measured_at")
    reading = (
        {
            "entity_id": export_identity(row["entity_id"] for row in components),
            "value": sum(row["value"] for row in components),
            "observed_at": min(captured_at or row["observed_at"] for row in components),
        }
        if components
        else None
    )
    for identity, versions in BonusCompiler().families(contract).items():
        period = versions[0].period_start(start)
        original = next(
            (
                row_item
                for row_item in evidence
                if row_item["rule_id"] == identity
                and datetime.fromisoformat(row_item["period_start"]) == period
            ),
            None,
        )
        if original:
            amount = original["initial_kwh"]
        else:
            seed = (
                next(
                    (
                        seed_item
                        for seed_item in book.seeds
                        if seed_item.rule_id == identity and seed_item.period_start == period
                    ),
                    None,
                )
                if book
                else None
            )
            if (
                not seed
                or (seed.statistics and not seed.statistics.physical_settlement_ready)
                or not reading
                or seed.entity_id != reading["entity_id"]
                or seed.observed_at > datetime.fromisoformat(reading["observed_at"])
            ):
                raise ValueError("Original bonus baseline is unavailable for this corrected rule")
            amount = seed.used_kwh + reading["value"] - seed.meter_kwh
            if len(components) > 1:
                meters = dict(seed.component_meters)
                if set(meters) != {row["entity_id"] for row in components} or any(
                    row["value"] < meters[row["entity_id"]] for row in components
                ):
                    raise ValueError("Original component meter baseline is unavailable or reset")
            if amount < 0:
                raise ValueError("Meter reset prevents historical expected bonus valuation")
        actual[identity + "|" + period.isoformat()] = amount
    origin = original_reference.get("reference_start", {}) if original_reference else {}
    if origin.get("stored") is not None:
        reference = origin.get("counters", {})
        if not actual.keys() <= reference.keys():
            raise ValueError("Original continuous reference bonus baseline is unavailable")
        return actual, {name: reference[name] for name in actual}
    return actual, dict(actual)


def corrected_problem(plan, contract):
    """Revalue original physical inputs and future wholesale evidence under corrected retail rules."""
    if plan.get("currency") and plan["currency"] != contract.currency:
        raise ValueError("A contract currency change cannot reinterpret original market prices")
    if plan.get("market_area") and plan["market_area"] != contract.market_area:
        raise ValueError("A bidding-area change cannot reinterpret original market prices")
    problem = physical_context(plan)
    evidence = plan["input_snapshot"].get("tariff_evidence", ())
    if not evidence:
        raise ValueError("Original wholesale tariff evidence is missing")
    tariffs = corrected_tariffs(evidence, contract)
    intervals = []
    for step in problem.intervals:
        cursor = step.start
        end = step.start + timedelta(hours=step.duration_h)
        for rate in tariffs:
            left, right = max(step.start, rate.start), min(end, rate.end)
            if left < right:
                if left != cursor:
                    raise ValueError("Original tariff evidence has a gap or overlap")
                intervals.append(
                    replace(
                        slice_interval(step, left, right),
                        buy_price=rate.buy_price,
                        sell_price=rate.sell_price,
                    )
                )
                cursor = right
        if cursor != end:
            raise ValueError("Original price evidence cannot cover plan flows")
    adjusted = replace(problem, intervals=tuple(intervals), terminal_valuations=())
    original_curves = plan["input_snapshot"]["problem"].get("terminal_valuations", ())
    future = (
        corrected_tariffs(original_curves[0].get("tariff_evidence", ()), contract)
        if original_curves
        else ()
    )
    adjusted = replace(adjusted, terminal_valuations=derive(adjusted, future))
    return adjusted, tariffs


def reprice(plan, contract, book, now):
    """Price identical original EMS/reference quantities using corrected rules, original prices and revalued inventory."""
    problem = physical_context(plan)
    adjusted, tariffs = corrected_problem(plan, contract)
    original_reference = plan.get("reference_forecast")
    if original_reference is None and not plan.get("reference_forecast_issue"):
        original_reference = comparison(plan)
    if original_reference and original_reference.get("financial_issue"):
        original_reference = None
    actual_counters, reference_counters = initial_counters(plan, contract, book, original_reference)
    starting_counters = {"ems": dict(actual_counters), "reference": dict(reference_counters)}
    location_data = plan["input_snapshot"].get("site_location")
    location = SiteLocation(**location_data) if location_data else None
    prices = [
        {
            "start": row_item.start.isoformat(),
            "end": row_item.end.isoformat(),
            "price": row_item.market_price,
            "version": row_item.source_version,
        }
        for row_item in tariffs
    ]
    totals = {
        name: {"purchase": 0.0, "sale": 0.0, "bonus": 0.0, "grid_cash": 0.0}
        for name in ("ems", "reference")
    }
    for index, (step, flow) in enumerate(
        zip(problem.intervals, plan["expected_flows"], strict=True)
    ):
        window = BillingWindow(step.start, step.start + timedelta(hours=step.duration_h), {})
        actual = bill(
            contract,
            window,
            flow["grid_import"],
            flow["grid_export"],
            actual_counters,
            (),
            prices,
            location,
        )
        for name in totals["ems"]:
            totals["ems"][name] += actual[name]
        if original_reference:
            ref = original_reference["reference"][index]
            result = bill(
                contract,
                window,
                ref["import_kwh"],
                ref["export_kwh"],
                reference_counters,
                (),
                prices,
                location,
            )
            for name in totals["reference"]:
                totals["reference"][name] += result[name]
    actual_start = {battery.device_id: battery.battery.initial_kwh for battery in problem.batteries}
    actual_end = {
        battery["device_id"]: battery["end_kwh"]
        for battery in plan["expected_flows"][-1].get("batteries", ())
    }
    totals["ems"].update(
        wear=plan["independent_costs"]["wear_cost"],
        inventory_change=inventory(adjusted, actual_end) - inventory(adjusted, actual_start),
    )
    if original_reference:
        start = original_reference.get("reference_start", {}).get("stored")
        ref_start = start if start is not None else actual_start
        totals["reference"].update(
            wear=original_reference["reference_costs"]["wear"],
            inventory_change=inventory(adjusted, original_reference["reference"][-1]["stored"])
            - inventory(adjusted, ref_start),
        )
    for name in ("ems", "reference"):
        if name == "reference" and original_reference is None:
            continue
        totals[name]["economic_cost"] = (
            totals[name]["grid_cash"] + totals[name]["wear"] - totals[name]["inventory_change"]
        )
    return {
        "plan_id": plan["plan_id"],
        "contract_version": contract.version,
        "original_contract_version": plan["input_snapshot"].get("contract_version"),
        "counter_version": book.version if book else None,
        "evaluated_at": now.isoformat(),
        "method": METHOD,
        "currency": contract.currency,
        "ems_costs": totals["ems"],
        "reference_costs": totals["reference"] if original_reference else None,
        "expected_savings": totals["reference"]["economic_cost"] - totals["ems"]["economic_cost"]
        if original_reference
        else None,
        "initial_counters": starting_counters,
        "ending_counters": {"ems": actual_counters, "reference": reference_counters},
        "terminal_values": [
            {
                name: getattr(terminal_valuation, name)
                for name in (
                    "device_id",
                    "reserve_kwh",
                    "usable_kwh",
                    "rate",
                    "window_start",
                    "window_end",
                    "price_basis",
                    "method",
                )
            }
            for terminal_valuation in adjusted.terminal_valuations
        ],
    }


class PlanValuations:
    """Bounded background contract-correction queue with immutable outcomes and success-only pointers."""

    def __init__(self, documents, plans):
        """Borrow the active database document adapter and original plan archive."""
        self.documents, self.plans = documents, plans
        self.events = None

    def process(self, contract, book, now):
        """Reprice newest plans first, at most twenty per round. Never replace a successful result with failure."""
        if contract is None:
            return
        generation = key([contract.version, book.version if book else None, METHOD])
        queue = (
            self.documents.read("repricing.json") if self.documents.exists("repricing.json") else {}
        )
        if queue.get("generation") != generation:
            queue = {"generation": generation, "before": None, "complete": False}
        if queue["complete"]:
            return
        rows = self.plans.list_plans(limit=REPRICING_BATCH_SIZE, before=queue["before"])
        for row in rows:
            plan = self.plans.get_plan(row["plan_id"])
            if (
                isinstance(plan.get("input_snapshot"), dict)
                and plan["input_snapshot"].get("contract_version") == contract.version
            ):
                continue
            activity_context = (
                self.events.thread_attempt(
                    "contract-valuations",
                    "settlement",
                    "Recalculate plan value",
                    failure="Plan repricing failed. The last successful valuation is retained.",
                )
                if self.events
                else nullcontext()
            )
            with activity_context as activity:
                identity = key([row["plan_id"], generation])
                pointer = "planvalue-" + key(row["plan_id"]) + ".json"
                status_document = "valueissue-" + key(row["plan_id"]) + ".json"
                try:
                    result = reprice(plan, contract, book, now)
                    result = json.loads(
                        json.dumps(result, default=lambda value: value.isoformat(), allow_nan=False)
                    )
                except (ValueError, KeyError, TypeError) as error:
                    self.documents.write(
                        status_document,
                        {
                            "contract_version": contract.version,
                            "issue": request_error_message(
                                error,
                                logger=_LOGGER,
                                operation="Plan repricing",
                                fallback=REPRICING_FAILURE_MESSAGE,
                            ),
                            "evaluated_at": now.isoformat(),
                        },
                    )
                    if activity:
                        from ..activities.events import Outcome

                        self.events.finish_thread(
                            activity,
                            Outcome.FAILED,
                            "Plan repricing failed. The last successful valuation is retained.",
                        )
                    continue
                with self.documents.transaction():
                    name = "valuation-" + identity + ".json"
                    if not self.documents.exists(name):
                        self.documents.write(name, result)
                    self.documents.write(pointer, name)
                    self.documents.write(
                        status_document, {"contract_version": contract.version, "issue": None}
                    )
                if activity:
                    from ..activities.events import Outcome

                    self.events.finish_thread(
                        activity,
                        Outcome.COMPLETED,
                        "The updated plan valuation was saved. The original plan remains unchanged.",
                        usable=True,
                    )
        queue.update(
            before=rows[-1]["plan_id"] if rows else queue["before"],
            complete=len(rows) < REPRICING_BATCH_SIZE,
        )
        self.documents.write("repricing.json", queue)

    def latest(self, plan_id):
        """Read the latest successful repricing separately from the latest attempted correction."""
        pointer = "planvalue-" + key(plan_id) + ".json"
        status_document = "valueissue-" + key(plan_id) + ".json"
        status = (
            self.documents.read(status_document) if self.documents.exists(status_document) else None
        )
        if status and status.get("issue"):
            status = {**status, "issue": REPRICING_FAILURE_MESSAGE}
        return {
            "valuation": self.documents.read(self.documents.read(pointer))
            if self.documents.exists(pointer)
            else None,
            "valuation_status": status,
        }
