"""Value a visible slice of frozen advice without changing its flows or valuation basis."""

from dataclasses import replace
from datetime import datetime, timedelta
from math import fsum, isfinite

from ..core.units import SECONDS_PER_HOUR
from ..optimization.export_bonus import ExportBonus, settle
from .accounting import inventory
from .context import physical_context
from .model import ENERGY_TOLERANCE_KWH

WINDOW_COST_ISSUE = (
    "Expected costs for this period are unavailable. Original plan evidence is incomplete."
)


def _energy(value):
    """Reject missing or invalid archived quantities, allowing only physical roundoff below zero."""
    if type(value) not in (int, float) or not isfinite(value) or value < -ENERGY_TOLERANCE_KWH:
        raise ValueError(WINDOW_COST_ISSUE)
    return max(0, value)


def _optimizer_rows(plan, problem):
    """Read each original battery endpoint and independently account for discharge wear."""
    result = []
    for flow in plan["expected_flows"]:
        batteries = {row["device_id"]: row for row in flow["batteries"]}
        if len(batteries) != len(flow["batteries"]) or batteries.keys() != {
            unit.device_id for unit in problem.batteries
        }:
            raise ValueError(WINDOW_COST_ISSUE)
        result.append(
            {
                "import_kwh": flow["grid_import"],
                "export_kwh": flow["grid_export"],
                "stored": {key: row["end_kwh"] for key, row in batteries.items()},
                "wear": fsum(
                    unit.battery.cycle_fee
                    * _energy(batteries[unit.device_id]["discharge_kwh"])
                    / unit.battery.discharge_efficiency
                    / unit.battery.capacity_kwh
                    for unit in problem.batteries
                ),
            }
        )
    return result


def _costs(plan, problem, rows, initial, start, end, reference_start=None):
    """Slice mean frozen flows and chronological bonus credits, using one inventory curve at both edges.

    Interior endpoints interpolate stored energy within the original constant-flow
    interval. Earlier export still consumes bonus room outside the selected window.
    A cap reached inside an interval pays its earliest exported volume only.
    """
    aligned = list(zip(problem.intervals, rows, strict=True))
    exported = tuple(_energy(row["export_kwh"]) for _, row in aligned)
    bonuses = []
    for definition in plan["input_snapshot"]["problem"].get("export_bonuses", ()):
        prepared = ExportBonus(
            **{
                **definition,
                **{key: tuple(definition[key]) for key in ("rates", "caps_kwh", "periods")},
            }
        )
        if reference_start and reference_start["stored"] is not None:
            prepared = replace(
                prepared,
                initial_kwh=reference_start["counters"][
                    prepared.rule_id + "|" + prepared.periods[0]
                ],
            )
        bonuses.append((prepared.rates, settle(prepared, exported).quota_kwh))
    purchase, sale, bonus, wear = [], [], [], []
    previous = dict(initial)
    opening = closing = None
    for index, (step, row) in enumerate(aligned):
        right = step.start + timedelta(hours=step.duration_h)
        stored = {key: _energy(value) for key, value in row["stored"].items()}
        if stored.keys() != previous.keys():
            raise ValueError(WINDOW_COST_ISSUE)
        left, stop = max(start, step.start), min(end, right)
        if left < stop:
            begin_fraction = (left - step.start) / (right - step.start)
            end_fraction = (stop - step.start) / (right - step.start)
            fraction = end_fraction - begin_fraction
            if opening is None:
                opening = {
                    key: previous[key] + (value - previous[key]) * begin_fraction
                    for key, value in stored.items()
                }
            closing = {
                key: previous[key] + (value - previous[key]) * end_fraction
                for key, value in stored.items()
            }
            purchase.append(_energy(row["import_kwh"]) * step.buy_price * fraction)
            sale.append(exported[index] * step.sell_price * fraction)
            wear.append(_energy(row["wear"]) * fraction)
            bonus.extend(
                rate[index]
                * (
                    min(exported[index] * end_fraction, credits[index])
                    - min(exported[index] * begin_fraction, credits[index])
                )
                for rate, credits in bonuses
            )
        previous = stored
    if opening is None or closing is None:
        raise ValueError(WINDOW_COST_ISSUE)
    electricity = fsum(purchase) - fsum(sale) - fsum(bonus)
    change = inventory(problem, closing) - inventory(problem, opening)
    return {
        "purchase": fsum(purchase),
        "sale": fsum(sale),
        "bonus": fsum(bonus),
        "wear": fsum(wear),
        "electricity_cost": electricity,
        "inventory_change": change,
        "economic_cost": electricity + fsum(wear) - change,
    }


def window_summary(plan, scenarios, start, end):
    """Return comparable expected amounts for the covered part of an aware increasing window.

    Keep the original terminal valuation, bonus budgets, strategy and reference
    histories. No optimization, live prices, measurements or persistence is used.
    Missing scenario evidence stays unavailable independently of other scenarios.
    """
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.utcoffset() is None
        or end.utcoffset() is None
        or start >= end
    ):
        raise ValueError("Choose an increasing, timezone-aware planning period")
    intervals = plan["input_snapshot"]["problem"]["intervals"]
    first = datetime.fromisoformat(intervals[0]["start"])
    last = intervals[-1]
    final = datetime.fromisoformat(last["start"]) + timedelta(hours=last["duration_h"])
    start, end = max(start, first), min(end, final)
    result = {
        "period": None
        if start >= end
        else {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_h": (end - start).total_seconds() / SECONDS_PER_HOUR,
        },
        "scenarios": {},
        "valuation_basis": "original_plan",
    }
    for name in ("optimizer", "without_battery", "self_consumption"):
        entry = result["scenarios"][name] = {}
        if start >= end:
            entry["issue"] = (
                "No planned intervals in the selected period. Select an earlier plan or wait for a new plan."
            )
            continue
        try:
            problem = physical_context(plan)
            initial = {unit.device_id: unit.battery.initial_kwh for unit in problem.batteries}
            reference_start = None
            if name == "optimizer":
                rows = _optimizer_rows(plan, problem)
            else:
                reference = scenarios[name]["comparison"]
                if reference.get("financial_issue"):
                    raise ValueError(WINDOW_COST_ISSUE)
                rows = reference["reference"]
                reference_start = reference["reference_start"]
                initial = (
                    reference_start["stored"] if reference_start["stored"] is not None else initial
                )
                if name == "without_battery":
                    problem = replace(problem, batteries=(), terminal_valuations=())
                    initial = {}
            entry["costs"] = _costs(plan, problem, rows, initial, start, end, reference_start)
        except (ValueError, KeyError, TypeError, OverflowError):
            entry["issue"] = WINDOW_COST_ISSUE
    baseline = result["scenarios"]["without_battery"].get("costs")
    for name in ("optimizer", "self_consumption"):
        entry = result["scenarios"][name]
        costs = entry.get("costs")
        entry["benefit_vs_without_battery"] = (
            baseline["economic_cost"] - costs["economic_cost"] if baseline and costs else None
        )
    return result
