"""Independent arithmetic and command-shape checks for vehicle charging candidates."""

from datetime import timedelta
from math import isfinite

from open_energy_planner.appliances.ev.model import delivered_power, tail_options, windows
from open_energy_planner.core.units import SECONDS_PER_HOUR, WATTS_PER_KW

EV_COMMAND_WATT_TOLERANCE = 1e-5


def validate(problem, plan, tolerance):
    """Reject wrong vehicles, fabricated energy, out-of-window charging and unattainable setpoints."""
    work_by_id = {work.session_id: work for work in problem.ev_work}
    energy = dict.fromkeys(work_by_id, 0.0)
    commands = {identity: [] for identity in work_by_id}

    def require(condition, reason):
        """Fail closed on an inconsistent candidate before any HA command can be published."""
        if not condition:
            raise ValueError(reason)

    for step, flow in zip(problem.intervals, plan.flows, strict=True):
        rows = {row.session_id: row for row in flow.ev}
        require(len(rows) == len(flow.ev) and set(rows) == set(work_by_id), "EV identities")
        end = step.start + timedelta(hours=step.duration_h)
        for identity, row in rows.items():
            work = work_by_id[identity]
            require(
                all(
                    isfinite(value) and value >= -tolerance
                    for value in (row.ac_kwh, row.stored_kwh, row.power_w)
                ),
                "EV finite nonnegative charge",
            )
            require(
                row.power_w <= work.maximum_w + EV_COMMAND_WATT_TOLERANCE,
                "EV power limit",
            )
            if row.power_w > EV_COMMAND_WATT_TOLERANCE:
                require(step.start >= work.ready_at and end <= work.deadline, "EV time window")
                require(
                    row.power_w + EV_COMMAND_WATT_TOLERANCE >= work.minimum_w, "EV minimum power"
                )
                if work.control == "on_off":
                    require(
                        abs(row.power_w - work.maximum_w) <= EV_COMMAND_WATT_TOLERANCE,
                        "EV fixed power",
                    )
            require(
                abs(row.ac_kwh - row.power_w * step.duration_h / WATTS_PER_KW) <= tolerance,
                "EV AC energy",
            )
            require(
                abs(
                    row.stored_kwh
                    - delivered_power(max(0, row.power_w)) * step.duration_h / WATTS_PER_KW
                )
                <= tolerance,
                "EV conversion energy",
            )
            energy[identity] += row.stored_kwh
            require(energy[identity] <= work.required_kwh + tolerance, "EV target overcharge")
            commands[identity].append((step, row.power_w))
    for identity, work in work_by_id.items():
        partial_stops = 0
        allowed_stops = tail_options(work, problem.intervals)
        for window in windows(work, problem.intervals):
            rows = [
                (step, power)
                for step, power in commands[identity]
                if window.start <= step.start < window.end
            ]
            active = [(step, power) for step, power in rows if power > EV_COMMAND_WATT_TOLERANCE]
            if not active:
                continue
            first_power = active[0][1]
            require(
                active[0][0].start == window.start
                and all(
                    abs(power - first_power) <= EV_COMMAND_WATT_TOLERANCE for _step, power in active
                ),
                "EV command group",
            )
            stop = active[-1][0].start + timedelta(hours=active[-1][0].duration_h)
            active_hours = sum(step.duration_h for step, _power in active)
            require(
                abs(active_hours - (stop - window.start).total_seconds() / SECONDS_PER_HOUR)
                <= tolerance,
                "EV interrupted command group",
            )
            if stop != window.end:
                partial_stops += 1
                require(
                    any(
                        abs(first_power - power) <= EV_COMMAND_WATT_TOLERANCE
                        and abs((stop - window.start).total_seconds() - seconds)
                        <= tolerance * SECONDS_PER_HOUR
                        for power, seconds in allowed_stops
                    ),
                    "EV partial stop",
                )
        require(partial_stops <= 1, "EV multiple partial stops")
