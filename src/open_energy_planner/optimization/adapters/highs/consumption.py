"""Prove corrected minimum grid import before restoring the existing financial objective."""

from math import isfinite
from time import perf_counter

import highspy

from open_energy_planner.optimization.calculation.consumption import (
    CONSUMPTION_PRIORITY_TOLERANCE_KWH,
    battery_inputs,
)

from ....installation.model import InstallationProblem
from .recovery import set_time_budget


def prioritize_consumption(model, problem: InstallationProblem, deadline):
    """Fix a proved kWh optimum within the shared budget. Never trade extra import for money.

    Return (optimum, issue). Failure has no publishable candidate. Terminal
    deficits are per battery: one battery's surplus cannot mask another's loss.
    Restore monetary costs, sense and precision even when optimization fails.
    """
    context, native = model.context, model.context.native
    imports = context.binding("grid").variables["import"]
    deficits = []
    for identity, battery, efficiency in battery_inputs(problem):
        key = "bat:" + identity
        deficit = native.addVariable(lb=0, ub=battery.initial_kwh / efficiency)
        native.addConstr(
            deficit
            >= (battery.initial_kwh - context.binding(key).variables["energy"][-1]) / efficiency
        )
        deficits.append(deficit)
    expression = sum(imports) + sum(deficits)
    objective, sense = native.getObjective()
    option_status, gap = native.getOptionValue("mip_abs_gap")
    if (
        option_status != highspy.HighsStatus.kOk
        or native.setOptionValue("mip_abs_gap", CONSUMPTION_PRIORITY_TOLERANCE_KWH)
        != highspy.HighsStatus.kOk
    ):
        raise ValueError("Rejected consumption priority precision")
    try:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            return None, "consumption_time_limit"
        set_time_budget(native, remaining)
        native.setObjective(expression)
        if native.run() == highspy.HighsStatus.kError:
            return None, "consumption_native_error"
        status = native.getModelStatus()
        if status != highspy.HighsModelStatus.kOptimal:
            return None, "consumption_" + (
                "time_limit"
                if status == highspy.HighsModelStatus.kTimeLimit
                else "infeasible"
                if status == highspy.HighsModelStatus.kInfeasible
                else "unproven"
            )
        optimum = native.getObjectiveValue()
        if not isfinite(optimum):
            return None, "consumption_invalid_objective"
        columns = list(native.getSolution().col_value)
        native.addConstr(expression <= max(0, optimum) + CONSUMPTION_PRIORITY_TOLERANCE_KWH)
    finally:
        native.setObjective(objective, sense)
        if native.setOptionValue("mip_abs_gap", gap) != highspy.HighsStatus.kOk:
            raise ValueError("Rejected original financial precision")
    seed = highspy.HighsSolution()
    seed.col_value, seed.value_valid = columns, True
    if native.setSolution(seed) != highspy.HighsStatus.kOk:
        raise ValueError("Rejected feasible consumption seed")
    return max(0, float(optimum)), None
