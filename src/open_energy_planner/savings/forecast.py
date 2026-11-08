"""Comparable original expectations using frozen plan inputs, never hindsight optimization."""

import json
from dataclasses import asdict, replace

from ..optimization.export_bonus import ExportBonus, settle
from .accounting import inventory
from .context import physical_context
from .model import POLICY_VERSION
from .reference import dispatch


def comparison(plan, reference_start=None, *, without_battery=False):
    """Compare future strategies from the plan's measured stock and frozen inputs.

    An explicit reference_start replays legacy archived expectations. New plans
    share the EMS initial stock and bonus budgets, independently of settlement.
    """
    if {work["task_id"] for work in plan["input_snapshot"]["problem"].get("appliances", [])} != {
        row["task_id"] for row in plan.get("appliance_schedule", [])
    }:
        raise ValueError("Unscheduled appliance work is not a comparable completed service")
    problem = physical_context(plan)
    actual_problem = problem
    if plan["input_snapshot"]["problem"].get("appliances"):
        problem = physical_context(plan, immediate_appliances=True)
    if without_battery:
        problem = replace(problem, batteries=(), terminal_valuations=())
        reference_start = None
    actual_start = {battery.device_id: battery.battery.initial_kwh for battery in problem.batteries}
    reference_start = reference_start or {
        "stored": None,
        "counters": {},
        "status": "measured_common_start",
        "estimated": False,
    }
    stored = (
        dict(reference_start["stored"]) if reference_start["stored"] is not None else actual_start
    )
    start_value = inventory(problem, stored)
    actual_start_value = inventory(
        actual_problem,
        {unit.device_id: unit.battery.initial_kwh for unit in actual_problem.batteries},
    )
    purchase = sale = wear = 0.0
    reference = []
    for step in problem.intervals:
        flow = dispatch(problem, step, stored)
        stored = flow["stored"]
        purchase += flow["import_kwh"] * step.buy_price
        sale += flow["export_kwh"] * step.sell_price
        wear += flow["wear"]
        reference.append({"start": step.start.isoformat(), "duration_h": step.duration_h, **flow})
    bonus = 0.0
    financial_issue = reference_start.get("financial_issue")
    for definition in plan["input_snapshot"]["problem"].get("export_bonuses", ()):
        prepared = ExportBonus(
            **{
                **definition,
                **{key: tuple(definition[key]) for key in ("rates", "caps_kwh", "periods")},
            }
        )
        if reference_start["stored"] is not None:
            key = prepared.rule_id + "|" + prepared.periods[0]
            if key not in reference_start["counters"]:
                financial_issue = "Continuous reference bonus state is missing"
                continue
            prepared = replace(prepared, initial_kwh=reference_start["counters"][key])
        settled = settle(prepared, tuple(row["export_kwh"] for row in reference))
        bonus += sum(
            rate * volume for rate, volume in zip(prepared.rates, settled.quota_kwh, strict=True)
        )
    actual = plan["independent_costs"]
    end_value = inventory(problem, stored)
    ref_cost = purchase - sale - bonus + wear - (end_value - start_value)
    ems_cost = actual["cash_cost"] - (actual["terminal_value"] - actual_start_value)
    return {
        "policy": POLICY_VERSION,
        "scenario": "without_battery" if without_battery else "self_consumption",
        "reference_start": reference_start,
        "reference": reference,
        "financial_issue": financial_issue,
        "expected_savings": None if financial_issue else ref_cost - ems_cost,
        "reference_costs": None
        if financial_issue
        else {
            "purchase": purchase,
            "sale": sale,
            "bonus": bonus,
            "wear": wear,
            "electricity_cost": purchase - sale - bonus,
            "inventory_change": end_value - start_value,
            "economic_cost": ref_cost,
        },
        "ems_costs": {
            "purchase": actual["import_cost"],
            "sale": actual["export_income"],
            "bonus": actual["bonus_income"],
            "wear": actual["wear_cost"],
            "electricity_cost": actual["import_cost"]
            - actual["export_income"]
            - actual["bonus_income"],
            "inventory_change": actual["terminal_value"] - actual_start_value,
            "economic_cost": ems_cost,
        },
        "intervals": json.loads(
            json.dumps(
                [asdict(interval) for interval in problem.intervals],
                default=lambda value: value.isoformat(),
            )
        ),
    }
