"""Model contiguous appliance cycles jointly with storage, prices, bonuses and shared grid limits."""

from datetime import timedelta

from open_energy_planner.appliances.cycles.model import candidates


def declare(context, problem):
    """Allocate one binary start choice per feasible absolute time, never independent phase switches."""
    for work in problem.appliances:
        binding = context.register("task:" + work.task_id)
        starts = candidates(work, context.intervals)
        binding.variables["start"] = tuple(context.native.addBinary() for _ in starts)
        binding.ports["starts"] = starts
        if work.fixed_start is None:
            binding.variables["missed"] = (context.native.addBinary(),)
            context.native.addConstr(
                sum(binding.variables["start"]) + binding.variables["missed"][0] == 1
            )
        elif starts:
            context.native.addConstr(sum(binding.variables["start"]) == 1)
        else:
            raise ValueError("Appliance deadline has no feasible candidate")


def energy(context, problem, interval_index):
    """Return AC cycle energy integrated over this exact billing interval."""
    step = context.intervals[interval_index]
    end = step.start + timedelta(hours=step.duration_h)
    return sum(
        work.profile.energy(start, step.start, end) * selected
        for work in problem.appliances
        for start, selected in zip(
            context.binding("task:" + work.task_id).ports["starts"],
            context.binding("task:" + work.task_id).variables["start"],
            strict=True,
        )
    )


def extract(context, problem):
    """Detach the one chosen start per task. Independent validation checks feasibility and accounting."""
    result = []
    for work in problem.appliances:
        binding = context.binding("task:" + work.task_id)
        chosen = [
            start
            for start, selected in zip(
                binding.ports["starts"], binding.variables["start"], strict=True
            )
            if context.native.val(selected) > 0.5
        ]
        if len(chosen) > 1 or not chosen and work.fixed_start is not None:
            raise ValueError("Missing appliance start choice")
        if chosen:
            result.append((work.task_id, chosen[0]))
    return tuple(result)


def prioritize_tasks(model, problem, deadline):
    """Prove maximum feasible task admission before monetary/consumption optimization, sharing its time budget."""
    from time import perf_counter

    import highspy

    from .recovery import set_time_budget

    missed = [
        model.context.binding("task:" + work.task_id).variables["missed"][0]
        for work in problem.appliances
        if work.fixed_start is None
    ]
    if not missed:
        return None
    native = model.context.native
    objective, sense = native.getObjective()
    try:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            return "appliance_admission_time_limit"
        native.setObjective(sum(missed))
        set_time_budget(native, remaining)
        if native.run() == highspy.HighsStatus.kError:
            return "appliance_admission_error"
        if native.getModelStatus() != highspy.HighsModelStatus.kOptimal:
            return "appliance_admission_unproven"
        native.addConstr(sum(missed) == round(native.getObjectiveValue()))
    finally:
        native.setObjective(objective, sense)
    return None
