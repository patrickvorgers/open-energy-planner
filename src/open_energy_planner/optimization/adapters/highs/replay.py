"""Replay one frozen command against realized inputs using the existing physical model."""

from dataclasses import replace
from time import perf_counter

import highspy

from open_energy_planner.core.units import WATTS_PER_KW

from ....installation.validation import validate
from ...domain import DEFAULT_REPLAY_TIME_LIMIT_SECONDS
from .installation import InstallationModelFactory
from .recovery import prioritize_reserve, set_time_budget
from .solver import NATIVE_OPTIONS

COMMAND_DEVIATION_TOLERANCE_KWH = 1e-8


def dispatch(problem, step, stored, commands):
    """Follow fixed battery targets first, then zero-grid regulation within all physical limits.

    Each call sees exactly one realized interval, never subsequent measurements or
    prices. Reserve recovery has its existing first priority. Targets are clamped
    by the unchanged AC/DC model. Infeasible or unproved recourse yields no evidence.
    Monetary settlement is separate. No solver monetary bound is exposed for this
    command-deviation objective.
    """
    physical = replace(
        problem,
        intervals=(replace(step, buy_price=0, sell_price=0),),
        batteries=tuple(
            replace(unit, battery=replace(unit.battery, initial_kwh=stored[unit.device_id]))
            for unit in problem.batteries
        ),
        export_bonuses=(),
        terminal_valuations=(),
    )
    model = InstallationModelFactory().build(physical)
    native = model.context.native
    for name, value in NATIVE_OPTIONS.items():
        if native.setOptionValue(name, value) != highspy.HighsStatus.kOk:
            raise ValueError("Replay native option rejected")
    deadline = perf_counter() + DEFAULT_REPLAY_TIME_LIMIT_SECONDS
    issue = prioritize_reserve(model, physical, deadline)
    if issue == "reserve_recovery_time_limit":
        raise TimeoutError("Replay reserve recovery time limit")
    if issue:
        raise ValueError(issue)
    for source in physical.pv_sources:
        if source.can_curtail:
            enabled = commands[source.source_id]["enabled"]
            if type(enabled) is not bool:
                raise ValueError("Replay requires an explicit PV command")
            native.addConstr(
                model.context.binding("pv:" + source.source_id).variables["on"][0] == enabled
            )
    deviations, following = [], False
    for unit in physical.batteries:
        command = commands[unit.device_id]
        if command["mode"] == "follow_grid_target" and unit.can_follow_grid:
            following = True
            continue
        if command["mode"] != "fixed_power":
            raise ValueError("Replay battery command mode is unsupported")
        target = command["power_w"] * step.duration_h / WATTS_PER_KW
        variables = model.context.binding("bat:" + unit.device_id).variables
        signed = variables["discharge"][0] - variables["charge"][0]
        deviation = native.addVariable(lb=0)
        native.addConstr(deviation >= signed - target)
        native.addConstr(deviation >= target - signed)
        deviations.append(deviation)

    def minimize(objective):
        """Require a proved current-step optimum before accepting any realized flow."""
        remaining = deadline - perf_counter()
        if remaining <= 0:
            raise TimeoutError("Replay time limit")
        native.setObjective(objective)
        set_time_budget(native, remaining)
        native_status = native.run()
        model_status = native.getModelStatus()
        if model_status == highspy.HighsModelStatus.kTimeLimit:
            raise TimeoutError("Replay native time limit")
        if (
            native_status == highspy.HighsStatus.kError
            or model_status != highspy.HighsModelStatus.kOptimal
        ):
            raise ValueError("Replay physical recourse was not proved")

    fixed = sum(deviations, 0 * model.context.binding("grid").variables["import"][0])
    minimize(fixed)
    if following:
        if deviations:
            native.addConstr(fixed <= native.getObjectiveValue() + COMMAND_DEVIATION_TOLERANCE_KWH)
        grid = model.context.binding("grid").variables
        minimize(grid["import"][0] + grid["export"][0])
    plan = model.extract()
    costs = validate(physical, plan)
    return plan.flows[0], costs.wear_cost
