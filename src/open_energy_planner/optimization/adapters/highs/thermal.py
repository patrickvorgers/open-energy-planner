"""Native on/off heat storage with proven draw, comfort and ending-stock priorities."""

from datetime import timedelta
from math import exp, expm1, isfinite
from time import perf_counter

import highspy

from open_energy_planner.appliances.thermal.model import ThermalFlow, active_hours
from open_energy_planner.core.units import SECONDS_PER_MINUTE, WATTS_PER_KW

from ....planning_policy import FINE_INTERVAL_MINUTES
from .recovery import set_time_budget

THERMAL_PRIORITY_TOLERANCE_KWH = 1e-7
THERMAL_TIME_TOLERANCE_SECONDS = 1e-6


def declare(context, problem):
    """Declare quarter-shared heaters and exact lumped temperatures for constant segment heat rates.

    Each segment assumes constant electrical input and served thermal draw.
    Unavailable heat remains an explicit unmet part of the declared draw.
    Unmet draw requires an exhausted ending store, never discretionary rejection.
    A zero-heating lower envelope rules out exhaustion before adding its binary.
    Exact exponential loss integration weights both by the same heating factor.
    Physical losses and draw begin at state_at. Heater permission begins at ready_at.
    """
    native = context.native
    quarter_seconds = FINE_INTERVAL_MINUTES * SECONDS_PER_MINUTE
    for work in problem.thermal_work:
        if len(work.demand_kwh) != len(problem.intervals):
            raise ValueError("Thermal demand interval count")
        binding = context.register("thermal:" + work.device_id)
        temperatures = context.variables(binding, "temperature", lb=0, ub=work.maximum_c)
        deficits = context.variables(
            binding, "shortfall", lb=0, ub=work.capacity_kwh_per_c * work.minimum_c
        )
        controls, ac_rows, power_rows, on_rows, unmet_rows = {}, [], [], [], []
        previous = work.initial_c
        passive_temperature = work.initial_c
        for interval_index, step in enumerate(problem.intervals):
            hours = active_hours(work, step)
            unmet_draw = 0
            if hours:
                end = step.start + timedelta(hours=step.duration_h)
                active_start = max(step.start, work.state_at or step.start)
                if active_start >= work.ready_at:
                    quarter = int(active_start.timestamp()) // quarter_seconds
                    if (
                        end.timestamp()
                        > (quarter + 1) * quarter_seconds + THERMAL_TIME_TOLERANCE_SECONDS
                    ):
                        raise ValueError("Thermal control intervals are not exactly refined")
                    if quarter not in controls:
                        controls[quarter] = native.addBinary()
                    on = controls[quarter]
                    power = work.heater_w * on
                    ac = power * hours / WATTS_PER_KW
                else:
                    on, power, ac = 0, 0, 0
                exponent = work.loss_w_per_c / WATTS_PER_KW * hours / work.capacity_kwh_per_c
                decay = exp(-exponent)
                heating_factor = -expm1(-exponent) / exponent if exponent else 1.0
                passive_temperature = max(
                    0,
                    work.ambient_c
                    + (passive_temperature - work.ambient_c) * decay
                    - work.demand_kwh[interval_index] * heating_factor / work.capacity_kwh_per_c,
                )
                if (
                    work.demand_kwh[interval_index] > 0
                    and passive_temperature * work.capacity_kwh_per_c
                    <= THERMAL_PRIORITY_TOLERANCE_KWH
                ):
                    unmet_draw = native.addVariable(lb=0, ub=work.demand_kwh[interval_index])
                    exhausted = native.addBinary()
                    native.addConstr(unmet_draw <= work.demand_kwh[interval_index] * exhausted)
                    native.addConstr(
                        temperatures[interval_index] <= work.maximum_c * (1 - exhausted)
                    )
                expected_temperature = (
                    work.ambient_c
                    + (previous - work.ambient_c) * decay
                    + (work.efficiency * ac - work.demand_kwh[interval_index] + unmet_draw)
                    * heating_factor
                    / work.capacity_kwh_per_c
                )
            else:
                on, power, ac = 0, 0, 0
                expected_temperature = previous
            native.addConstr(temperatures[interval_index] == expected_temperature)
            native.addConstr(
                deficits[interval_index]
                >= work.capacity_kwh_per_c * (work.minimum_c - temperatures[interval_index])
            )
            ac_rows.append(ac)
            power_rows.append(power)
            on_rows.append(on)
            unmet_rows.append(unmet_draw)
            previous = temperatures[interval_index]
        terminal_deficit = native.addVariable(lb=0, ub=work.capacity_kwh_per_c * work.terminal_c)
        native.addConstr(terminal_deficit >= work.capacity_kwh_per_c * (work.terminal_c - previous))
        binding.ports.update(
            ac=tuple(ac_rows),
            power=tuple(power_rows),
            on=tuple(on_rows),
            end_c=temperatures,
            shortfall=deficits,
            unmet_draw=tuple(unmet_rows),
            terminal_shortfall=terminal_deficit,
        )


def energy(context, problem, interval_index):
    """Expose each heater's AC intake once to the shared electrical bus."""
    return sum(
        context.binding("thermal:" + work.device_id).ports["ac"][interval_index]
        for work in problem.thermal_work
    )


def extract(context, problem, interval_index, values):
    """Detach actual temperatures and physical deficits rather than exposing native slack."""

    def value(expression):
        """Resolve a model expression or an inactive segment's numeric constant."""
        if isinstance(expression, (int, float)):
            return float(expression)
        if isinstance(expression, highspy.highs_var):
            return float(values[int(expression)])
        return float(expression.evaluate(values))

    flows = []
    for work in problem.thermal_work:
        ports = context.binding("thermal:" + work.device_id).ports
        end_temperature = value(ports["end_c"][interval_index])
        on = value(ports["on"][interval_index]) > 0.5
        flows.append(
            ThermalFlow(
                work.device_id,
                value(ports["ac"][interval_index]) if on else 0.0,
                end_temperature,
                max(0.0, work.minimum_c - end_temperature) * work.capacity_kwh_per_c,
                work.heater_w if on else 0.0,
                value(ports["unmet_draw"][interval_index]),
            )
        )
    return tuple(flows)


def prioritize_service(model, problem, deadline):
    """Prove least unmet draw, integrated comfort deficit and final stock deficit before money.

    Every run consumes the existing absolute solver deadline. Positive optima
    require native proof. A feasible zero deficit is already globally optimal
    within the absolute tolerance because every deficit has a zero lower bound.
    Restore native precision, objective target and financial objective on exit.
    """
    works = problem.thermal_work
    if not works:
        return None
    native = model.context.native
    objective, sense = native.getObjective()
    saved_options = {}
    priority_options = {
        "mip_abs_gap": THERMAL_PRIORITY_TOLERANCE_KWH / 10,
        "mip_rel_gap": 0.0,
        "objective_target": -highspy.kHighsInf,
    }
    for option in priority_options:
        status, previous = native.getOptionValue(option)
        if status != highspy.HighsStatus.kOk:
            raise ValueError("Thermal priority option is unavailable")
        saved_options[option] = previous
    try:
        for option, setting in priority_options.items():
            if native.setOptionValue(option, setting) != highspy.HighsStatus.kOk:
                raise ValueError("Thermal priority option was rejected")
        unmet_draw = sum(
            model.context.binding("thermal:" + work.device_id).ports["unmet_draw"][interval_index]
            for work in works
            for interval_index in range(len(problem.intervals))
        )
        comfort = sum(
            model.context.binding("thermal:" + work.device_id).ports["shortfall"][interval_index]
            * step.duration_h
            for work in works
            for interval_index, step in enumerate(problem.intervals)
        )
        ending = sum(
            model.context.binding("thermal:" + work.device_id).ports["terminal_shortfall"]
            for work in works
        )
        for expression in (unmet_draw, comfort, ending):
            if isinstance(expression, (int, float)):
                continue
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return "thermal_service_time_limit"
            native.setObjective(expression)
            set_time_budget(native, remaining)
            if native.run() == highspy.HighsStatus.kError:
                return "thermal_service_error"
            optimum = native.getObjectiveValue()
            solution = native.getSolution()
            feasible = (
                solution.value_valid
                and native.getInfo().primal_solution_status
                == highspy.SolutionStatus.kSolutionStatusFeasible
            )
            if not feasible or not isfinite(optimum):
                return "thermal_service_unproven"
            if (
                native.getModelStatus() != highspy.HighsModelStatus.kOptimal
                and optimum > THERMAL_PRIORITY_TOLERANCE_KWH
            ):
                return (
                    "thermal_service_time_limit"
                    if native.getModelStatus() == highspy.HighsModelStatus.kTimeLimit
                    else "thermal_service_unproven"
                )
            native.addConstr(expression <= max(0.0, optimum) + THERMAL_PRIORITY_TOLERANCE_KWH)
            seed = highspy.HighsSolution()
            seed.col_value, seed.value_valid = list(solution.col_value), True
            if native.setSolution(seed) != highspy.HighsStatus.kOk:
                raise ValueError("Thermal priority incumbent was rejected")
    finally:
        native.setObjective(objective, sense)
        for option, previous in saved_options.items():
            if native.setOptionValue(option, previous) != highspy.HighsStatus.kOk:
                raise ValueError("Thermal priority option could not be restored")
    return None
