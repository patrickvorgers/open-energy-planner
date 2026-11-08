"""Read-only reference chart projections using frozen physics, never another optimizer."""

from copy import deepcopy
from math import isclose, isfinite

from .context import physical_context
from .forecast import comparison
from .model import ENERGY_TOLERANCE_KWH
from .reference import dispatch


def scenario(plan, reference, *, without_battery=False):
    """Expose physical bars only when deterministic replay matches retained reference quantities.

    Old reference documents lack converter flows. Replay their original starting
    stocks and validate every retained grid/stock outcome before presenting them.
    Never overwrite original plans, costs, counters or continuous reference state.
    """
    view = {"input_snapshot": {"problem": deepcopy(plan["input_snapshot"]["problem"])}}
    basis = view["input_snapshot"]["problem"]
    if basis.get("appliances") or basis.get("ev_work") or basis.get("thermal_work"):
        basis["intervals"] = deepcopy(reference["intervals"])
        basis.pop("appliances", None)
    if basis.get("ev_work") or basis.get("thermal_work"):
        view["expected_flows"] = deepcopy(plan["expected_flows"])
        for step, flow in zip(basis["intervals"], plan["expected_flows"], strict=True):
            step["load_kwh"] -= sum(
                row["ac_kwh"] for key in ("ev", "thermal") for row in flow.get(key, [])
            )
    if without_battery:
        basis["batteries"] = []
        basis["terminal_valuations"] = []
    initial = reference["reference_start"]["stored"]
    if initial is not None:
        for unit in basis["batteries"]:
            unit["battery"]["initial_kwh"] = initial[unit["device_id"]]
    physics = physical_context(view)
    stored = {unit.device_id: unit.battery.initial_kwh for unit in physics.batteries}
    physical_flows = []
    for step, recorded in zip(physics.intervals, reference["reference"], strict=True):
        flow = dispatch(physics, step, stored)
        if (
            any(
                not isclose(flow[key], recorded[key], rel_tol=0, abs_tol=ENERGY_TOLERANCE_KWH)
                for key in ("import_kwh", "export_kwh")
            )
            or flow["stored"].keys() != recorded["stored"].keys()
            or any(
                not isclose(
                    value, recorded["stored"][identity], rel_tol=0, abs_tol=ENERGY_TOLERANCE_KWH
                )
                for identity, value in flow["stored"].items()
            )
        ):
            raise ValueError(
                "Original reference physics cannot be reproduced. Bars are unavailable"
            )
        stored = flow["stored"]
        physical_flows.append(
            {
                "grid_import": flow["import_kwh"],
                "grid_export": flow["export_kwh"],
                "batteries": [
                    {"device_id": identity, **values}
                    for identity, values in flow["batteries"].items()
                ],
                "inverters": flow["inverters"],
                "solar": flow["solar"],
                "ev": deepcopy(plan["expected_flows"][len(physical_flows)]["ev"])
                if basis.get("ev_work")
                else [],
                "thermal": deepcopy(plan["expected_flows"][len(physical_flows)]["thermal"])
                if basis.get("thermal_work")
                else [],
            }
        )
    view["expected_flows"] = physical_flows
    reference = deepcopy(reference)
    for costs in (reference.get("reference_costs"), reference.get("ems_costs")):
        if costs is not None:
            amounts = [costs.get(key) for key in ("purchase", "sale", "bonus")]
            costs["electricity_cost"] = (
                amounts[0] - amounts[1] - amounts[2]
                if all(type(value) in (int, float) and isfinite(value) for value in amounts)
                else None
            )
    result = {"comparison": reference, "chart": view}
    if reference.get("financial_issue"):
        result["issue"] = reference["financial_issue"]
    return result


def scenarios(plan, self_consumption):
    """Keep reference outages independent. No-battery starts with the plan's original bonus budgets."""
    result = {}
    for name in ("without_battery", "self_consumption"):
        try:
            reference = (
                comparison(plan, without_battery=True)
                if name == "without_battery"
                else self_consumption
            )
            if reference is None:
                raise ValueError("Continuous self-consumption reference is unavailable")
            result[name] = scenario(plan, reference, without_battery=name == "without_battery")
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            result[name] = {"issue": str(error)}
    baseline = result["without_battery"].get("comparison")
    reference = result["self_consumption"].get("comparison")
    if (
        baseline is not None
        and reference is not None
        and baseline.get("reference_costs") is not None
        and reference.get("reference_costs") is not None
    ):
        result["self_consumption"]["expected_savings_vs_without_battery"] = (
            baseline["reference_costs"]["economic_cost"]
            - reference["reference_costs"]["economic_cost"]
        )
    return result
