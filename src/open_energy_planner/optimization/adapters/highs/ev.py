"""Exact EV power windows and early stops. Native expressions never enter the app domain."""

from datetime import timedelta
from itertools import pairwise
from time import perf_counter

import highspy

from open_energy_planner.appliances.ev.model import (
    DELIVERED_POWER_POINTS,
    EV_ENERGY_TOLERANCE_KWH,
    EVFlow,
    delivered_power,
    tail_options,
    windows,
)
from open_energy_planner.core.units import SECONDS_PER_HOUR, WATTS_PER_KW

from .recovery import set_time_budget


def overlap_hours(left, right, start, end):
    """Integrate an explicit active period over a billing segment without averaging its command."""
    return max(0, (min(right, end) - max(left, start)).total_seconds()) / SECONDS_PER_HOUR


def full_power(context, work):
    """Declare a fixed on/off level or continuous AC watts within one physical conversion band."""
    native = context.native
    on = native.addBinary()
    power = native.addVariable(lb=0, ub=work.maximum_w)
    if work.control == "on_off":
        native.addConstr(power == work.maximum_w * on)
        return on, power, delivered_power(work.maximum_w) * on
    selected, powers, stored = [], [], []
    for (left, delivered_left), (right, delivered_right) in pairwise(DELIVERED_POWER_POINTS):
        lower, upper = max(work.minimum_w, left), min(work.maximum_w, right)
        if lower > upper:
            continue
        choice = native.addBinary()
        band_power = native.addVariable(lb=0, ub=upper)
        native.addConstr(band_power >= lower * choice)
        native.addConstr(band_power <= upper * choice)
        slope = (delivered_right - delivered_left) / (right - left)
        intercept = delivered_left - slope * left
        selected.append(choice)
        powers.append(band_power)
        stored.append(slope * band_power + intercept * choice)
    native.addConstr(sum(selected) == on)
    native.addConstr(power == sum(powers))
    return on, power, sum(stored)


def declare(context, problem):
    """Own each session's full-window controls plus at most one explicitly timed final pulse."""
    native = context.native
    for work in problem.ev_work:
        binding = context.register("ev:" + work.session_id)
        if work.required_kwh <= EV_ENERGY_TOLERANCE_KWH:
            binding.ports.update(
                ac=(0,) * len(problem.intervals),
                stored=(0,) * len(problem.intervals),
                power=(0,) * len(problem.intervals),
                on=(0,) * len(problem.intervals),
            )
            continue
        options = tail_options(work, problem.intervals)
        controls = []
        tail_choices = []
        for window in windows(work, problem.intervals):
            full_on, power, stored_power = full_power(context, work)
            tails = tuple(
                (tail_power, seconds, native.addBinary())
                for tail_power, seconds in options
                if seconds <= window.duration_h * SECONDS_PER_HOUR
            )
            native.addConstr(full_on + sum(choice for _power, _seconds, choice in tails) <= 1)
            tail_choices.extend(choice for _power, _seconds, choice in tails)
            controls.append((window, full_on, power, stored_power, tails))
        if tail_choices:
            native.addConstr(sum(tail_choices) <= 1)
        ac_rows, stored_rows, power_rows, on_rows = [], [], [], []
        for step in problem.intervals:
            end = step.start + timedelta(hours=step.duration_h)
            ac, stored, command, active_controls = [], [], [], []
            for window, full_on, power, stored_power, tails in controls:
                hours = overlap_hours(step.start, end, window.start, window.end)
                if hours == 0:
                    continue
                if abs(hours - step.duration_h) > EV_ENERGY_TOLERANCE_KWH:
                    raise ValueError("EV active window is not exactly refined")
                ac.append(power * hours / WATTS_PER_KW)
                stored.append(stored_power * hours / WATTS_PER_KW)
                command.append(power)
                active_controls.append(full_on)
                for tail_power, seconds, choice in tails:
                    active = overlap_hours(
                        step.start, end, window.start, window.start + timedelta(seconds=seconds)
                    )
                    if active and abs(active - step.duration_h) > EV_ENERGY_TOLERANCE_KWH:
                        raise ValueError("EV stop is not exactly refined")
                    ac.append(tail_power * active / WATTS_PER_KW * choice)
                    stored.append(delivered_power(tail_power) * active / WATTS_PER_KW * choice)
                    if active:
                        command.append(tail_power * choice)
                        active_controls.append(choice)
            ac_rows.append(sum(ac))
            stored_rows.append(sum(stored))
            power_rows.append(sum(command))
            on_rows.append(sum(active_controls))
        binding.ports.update(
            ac=tuple(ac_rows), stored=tuple(stored_rows), power=tuple(power_rows), on=tuple(on_rows)
        )
        if controls:
            native.addConstr(sum(stored_rows) <= work.required_kwh)


def energy(context, problem, interval_index):
    """Join each connected vehicle's AC consumption to the common bus exactly once."""
    return sum(
        context.binding("ev:" + work.session_id).ports["ac"][interval_index]
        for work in problem.ev_work
    )


def extract(context, problem, interval_index, values):
    """Detach selected controls. Round binary off choices to zero, including their numerical leakage."""

    def value(expression):
        """Avoid asking the native evaluator to resolve a constant empty/off expression."""
        return (
            float(expression)
            if isinstance(expression, (int, float))
            else float(expression.evaluate(values))
        )

    flows = []
    for work in problem.ev_work:
        ports = context.binding("ev:" + work.session_id).ports
        if value(ports["on"][interval_index]) > 0.5:
            flows.append(
                EVFlow(
                    work.session_id,
                    value(ports["ac"][interval_index]),
                    value(ports["stored"][interval_index]),
                    value(ports["power"][interval_index]),
                )
            )
        else:
            flows.append(EVFlow(work.session_id, 0.0, 0.0, 0.0))
    return tuple(flows)


def prioritize_service(model, problem, deadline):
    """Prove maximum useful charge by earliest departure before consumption/money optimization.

    Existing stationary reserve recovery and admitted appliance service retain
    their priority. Equal-departure vehicles maximize total useful battery kWh.
    All stages share the same solver deadline and preserve the financial objective.
    """
    if not problem.ev_work:
        return None
    native = model.context.native
    objective, sense = native.getObjective()
    option_status, previous_target = native.getOptionValue("objective_target")
    if option_status != highspy.HighsStatus.kOk:
        raise ValueError("EV service objective target is unavailable")
    try:
        for departure in sorted({work.deadline for work in problem.ev_work}):
            expression = sum(
                sum(model.context.binding("ev:" + work.session_id).ports["stored"])
                for work in problem.ev_work
                if work.deadline == departure
            )
            if isinstance(expression, (int, float)):
                continue
            maximum_service = sum(
                work.required_kwh for work in problem.ev_work if work.deadline == departure
            )
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return "ev_service_time_limit"
            native.setObjective(-expression)
            if (
                native.setOptionValue(
                    "objective_target", -maximum_service + EV_ENERGY_TOLERANCE_KWH
                )
                != highspy.HighsStatus.kOk
            ):
                raise ValueError("EV service objective target was rejected")
            set_time_budget(native, remaining)
            if native.run() == highspy.HighsStatus.kError:
                return "ev_service_error"
            solution = native.getSolution()
            complete = (
                solution.value_valid
                and native.getInfo().primal_solution_status
                == highspy.SolutionStatus.kSolutionStatusFeasible
                and -native.getObjectiveValue() >= maximum_service - EV_ENERGY_TOLERANCE_KWH
            )
            if native.getModelStatus() != highspy.HighsModelStatus.kOptimal and not complete:
                return "ev_service_unproven"
            native.addConstr(expression >= -native.getObjectiveValue() - EV_ENERGY_TOLERANCE_KWH)
            seed = highspy.HighsSolution()
            seed.col_value, seed.value_valid = list(solution.col_value), True
            if native.setSolution(seed) != highspy.HighsStatus.kOk:
                raise ValueError("EV priority incumbent was rejected")
    finally:
        native.setOptionValue("objective_target", previous_target)
        native.setObjective(objective, sense)
    return None
