"""Independent heat-balance arithmetic and electrical command validation."""

from datetime import timedelta
from math import exp, isfinite

from open_energy_planner.core.units import SECONDS_PER_HOUR, SECONDS_PER_MINUTE, WATTS_PER_KW
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES

THERMAL_COMMAND_WATT_TOLERANCE = 1e-5
SMALL_DECAY_EXPONENT = 1e-6
THERMAL_TIME_TOLERANCE_SECONDS = 1e-6


def validate(problem, plan, tolerance):
    """Reject fabricated heat, wrong devices, unbounded temperatures and inconsistent quarter controls.

    Recompute exponential cooling and constant-rate heating/served draw independently
    from the native builder. The small-exponent polynomial avoids cancellation
    in (1 - exp(-exponent)) / exponent without sharing the builder's helper.
    Unmet draw requires no remaining heat above the tank's zero lower bound.
    """
    works = {work.device_id: work for work in getattr(problem, "thermal_work", ())}
    previous = {identity: work.initial_c for identity, work in works.items()}
    controls = {}
    quarter_seconds = FINE_INTERVAL_MINUTES * SECONDS_PER_MINUTE

    def require(condition, reason):
        """Fail closed before publication when independent physical evidence disagrees."""
        if not condition:
            raise ValueError(reason)

    for interval_index, (step, flow) in enumerate(zip(problem.intervals, plan.flows, strict=True)):
        rows = {row.device_id: row for row in flow.thermal}
        require(
            len(rows) == len(flow.thermal) and rows.keys() == works.keys(), "Thermal identities"
        )
        end = step.start + timedelta(hours=step.duration_h)
        for identity, row in rows.items():
            work = works[identity]
            require(
                all(
                    type(value) in (int, float) and isfinite(value)
                    for value in (
                        row.ac_kwh,
                        row.end_c,
                        row.shortfall_kwh,
                        row.power_w,
                        row.unmet_draw_kwh,
                    )
                ),
                "Thermal finite physical evidence",
            )
            require(
                row.ac_kwh >= -tolerance
                and row.shortfall_kwh >= -tolerance
                and -tolerance <= row.unmet_draw_kwh <= work.demand_kwh[interval_index] + tolerance
                and -tolerance <= row.end_c <= work.maximum_c + tolerance,
                "Thermal physical bounds",
            )
            require(
                row.unmet_draw_kwh <= tolerance or row.end_c * work.capacity_kwh_per_c <= tolerance,
                "Thermal unmet draw requires exhausted heat",
            )
            require(
                abs(row.power_w) <= THERMAL_COMMAND_WATT_TOLERANCE
                or abs(row.power_w - work.heater_w) <= THERMAL_COMMAND_WATT_TOLERANCE,
                "Thermal fixed heater power",
            )
            active_start = max(step.start, work.state_at or step.start)
            duration_hours = max(0, (end - active_start).total_seconds())
            duration_hours /= SECONDS_PER_HOUR
            if duration_hours:
                if active_start >= work.ready_at:
                    quarter = int(active_start.timestamp()) // quarter_seconds
                    require(
                        end.timestamp()
                        <= (quarter + 1) * quarter_seconds + THERMAL_TIME_TOLERANCE_SECONDS,
                        "Thermal refined control interval",
                    )
                    control_key = (identity, quarter)
                    control_power = controls.setdefault(control_key, row.power_w)
                    require(
                        abs(row.power_w - control_power) <= THERMAL_COMMAND_WATT_TOLERANCE,
                        "Thermal quarter control",
                    )
                else:
                    require(
                        abs(row.power_w) <= THERMAL_COMMAND_WATT_TOLERANCE,
                        "Thermal heating before readiness",
                    )
                decay_exponent = (
                    work.loss_w_per_c * duration_hours / WATTS_PER_KW / work.capacity_kwh_per_c
                )
                decay = exp(-decay_exponent)
                heating_factor = (
                    1 - decay_exponent / 2 + decay_exponent * decay_exponent / 6
                    if decay_exponent < SMALL_DECAY_EXPONENT
                    else (1 - decay) / decay_exponent
                )
                expected_temperature = (
                    work.ambient_c
                    + (previous[identity] - work.ambient_c) * decay
                    + (
                        work.efficiency * row.ac_kwh
                        - work.demand_kwh[interval_index]
                        + row.unmet_draw_kwh
                    )
                    * heating_factor
                    / work.capacity_kwh_per_c
                )
            else:
                require(
                    abs(row.power_w) <= THERMAL_COMMAND_WATT_TOLERANCE,
                    "Thermal heating before state admission",
                )
                require(
                    abs(row.unmet_draw_kwh) <= tolerance,
                    "Thermal unmet draw before state admission",
                )
                expected_temperature = previous[identity]
            require(
                abs(row.ac_kwh - row.power_w * duration_hours / WATTS_PER_KW) <= tolerance,
                "Thermal AC energy",
            )
            require(
                abs(row.end_c - expected_temperature) * work.capacity_kwh_per_c <= tolerance,
                "Thermal heat balance",
            )
            expected_shortfall = max(0.0, work.minimum_c - row.end_c) * work.capacity_kwh_per_c
            require(
                abs(row.shortfall_kwh - expected_shortfall) <= tolerance,
                "Thermal physical shortfall",
            )
            previous[identity] = row.end_c
