"""Lexicographic reserve restoration within the existing isolated native model."""

from datetime import timedelta
from math import isfinite
from time import perf_counter

import highspy

from ....installation.model import InstallationProblem

RESERVE_PRIORITY_TOLERANCE_KWH = 1e-08


def set_time_budget(native, remaining):
    """Set seconds remaining: HiGHS 1.15.1 MIP uses a fresh timer per run.

    Its getRunTime() reports cumulative time, but the MIP limit is checked on
    the separate per-run timer. Adding prior runtime would overrun our budget.
    Reject an unsupported native option rather than silently dropping the bound.
    """
    if native.setOptionValue("time_limit", remaining) != highspy.HighsStatus.kOk:
        raise ValueError("Rejected remaining solve time limit")


def prioritize_reserve(model, problem: InstallationProblem, deadline):
    """Minimize aggregate unmet reserve at successive original interval ends before money.

    Nonempty reserve_checkpoints restrict priority to those interval ends, so EV
    refinement cannot add new reserve priorities. Empty checkpoints retain every end.

    Opted-in batteries initially below reserve target their configured reserve.
    Active calibration charges target full capacity. Fix each proven optimum
    before advancing and stop once all participating targets are restored.
    Shared limits can require choosing which battery recovers first. Monetary
    optimization breaks ties only inside the proven aggregate shortfall profile.
    The same wall-clock budget covers every stage. An unproven priority returns
    no candidate rather than silently trading reserve recovery for money.

    Args:
        model: Built native model owning the HiGHS context and device bindings.
        problem: Typed batteries, intervals and optional original reserve_checkpoints.
        deadline: Absolute perf_counter() timestamp shared with the money solve.

    Returns:
        None if no battery participates or all priorities were proved. Otherwise
        reserve_recovery_time_limit, reserve_recovery_native_error,
        reserve_recovery_infeasible, reserve_recovery_unproven or
        reserve_recovery_invalid_objective. No candidate escapes those failures.

    Raises:
        ValueError: If HiGHS rejects precision/time options or the feasible recovery seed.
    """
    context, native = model.context, model.context.native
    units = [
        (
            "bat:" + battery_item.device_id,
            battery_item.battery.capacity_kwh
            if battery_item.calibration_charge
            else battery_item.battery.minimum_kwh,
        )
        for battery_item in problem.batteries
        if battery_item.calibration_charge
        or (
            battery_item.battery.recover_reserve
            and battery_item.battery.initial_kwh < battery_item.battery.minimum_kwh
        )
    ]
    if not units:
        return None
    objective, sense = native.getObjective()
    option_status, financial_gap = native.getOptionValue("mip_abs_gap")
    if (
        option_status != highspy.HighsStatus.kOk
        or native.setOptionValue("mip_abs_gap", RESERVE_PRIORITY_TOLERANCE_KWH)
        != highspy.HighsStatus.kOk
    ):
        raise ValueError("Rejected reserve priority precision")
    try:
        checkpoints = set(problem.reserve_checkpoints)
        for interval_index in range(len(problem.intervals)):
            step = problem.intervals[interval_index]
            if checkpoints and step.start + timedelta(hours=step.duration_h) not in checkpoints:
                continue
            shortfall = []
            for key, target in units:
                deficit = native.addVariable(lb=0, ub=target)
                native.addConstr(
                    deficit >= target - context.binding(key).variables["energy"][interval_index]
                )
                shortfall.append(deficit)
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return "reserve_recovery_time_limit"
            set_time_budget(native, remaining)
            native.setObjective(sum(shortfall))
            if native.run() == highspy.HighsStatus.kError:
                return "reserve_recovery_native_error"
            status = native.getModelStatus()
            if status != highspy.HighsModelStatus.kOptimal:
                if status == highspy.HighsModelStatus.kTimeLimit:
                    return "reserve_recovery_time_limit"
                return "reserve_recovery_" + (
                    "infeasible" if status == highspy.HighsModelStatus.kInfeasible else "unproven"
                )
            optimum = native.getObjectiveValue()
            if not isfinite(optimum):
                return "reserve_recovery_invalid_objective"
            columns = list(native.getSolution().col_value)
            native.addConstr(sum(shortfall) <= max(0, optimum) + RESERVE_PRIORITY_TOLERANCE_KWH)
            if optimum <= RESERVE_PRIORITY_TOLERANCE_KWH:
                # Recovery cannot be undone. Make that implied future floor explicit for presolve.
                for key, target in units:
                    for end in context.binding(key).variables["energy"][interval_index + 1 :]:
                        native.addConstr(end >= target - RESERVE_PRIORITY_TOLERANCE_KWH)
                break
    finally:
        native.setObjective(objective, sense)
        if native.setOptionValue("mip_abs_gap", financial_gap) != highspy.HighsStatus.kOk:
            raise ValueError("Rejected original financial precision")
    # Adding the final priority row invalidates HiGHS' automatic incumbent reuse.
    seed = highspy.HighsSolution()
    seed.col_value, seed.value_valid = columns, True
    if native.setSolution(seed) != highspy.HighsStatus.kOk:
        raise ValueError("Rejected feasible reserve recovery seed")
    return None
