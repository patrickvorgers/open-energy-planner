"""Independent arithmetic checks for multi-inverter physics, saturation and financial settlement."""

from dataclasses import fields
from datetime import timedelta
from math import isfinite

from open_energy_planner.core.units import WATTS_PER_KW

from ..optimization.domain import Costs
from ..optimization.export_bonus import settle
from .model import InstallationPlan

DEFAULT_VALIDATION_TOLERANCE = 1e-07
WHOLE_WATT_TOLERANCE = 1e-05


def validate(problem, plan, tolerance=DEFAULT_VALIDATION_TOLERANCE):
    """Recompute conservation, limits, reserve, whole commands and costs without importing solver code."""

    def require(condition, reason):
        """Reject invalid or incomplete evidence before it can become an HA command."""
        if not condition:
            raise ValueError(reason)

    def close(left_value, right_value):
        """Compare interval quantities using the same absolute numerical acceptance tolerance."""
        return (
            isfinite(left_value)
            and isfinite(right_value)
            and abs(left_value - right_value) <= tolerance
        )

    require(
        isinstance(plan, InstallationPlan) and len(plan.flows) == len(problem.intervals),
        "Installation plan shape",
    )
    from open_energy_planner.appliances.ev.validation import validate as validate_ev

    validate_ev(problem, plan, tolerance)
    from open_energy_planner.appliances.thermal.validation import validate as validate_thermal

    validate_thermal(problem, plan, tolerance)
    starts = dict(plan.appliance_starts)
    require(
        len(starts) == len(plan.appliance_starts)
        and set(starts) <= {work.task_id for work in problem.appliances},
        "Appliance start identities",
    )
    for work in problem.appliances:
        if work.task_id not in starts:
            require(work.fixed_start is None, "Committed appliance disappeared")
            continue
        start = starts[work.task_id]
        require(start.utcoffset() is not None, "Aware appliance start")
        if work.fixed_start is not None:
            require(start == work.fixed_start, "Committed appliance start changed")
        else:
            from open_energy_planner.appliances.cycles.model import (
                DURATION_ALLOWANCE_SECONDS,
                PROFILE_STEP_SECONDS,
            )

            require(
                start >= max(work.ready_at, problem.intervals[0].start)
                and start.timestamp() % PROFILE_STEP_SECONDS == 0
                and start
                + timedelta(seconds=work.profile.duration_seconds + DURATION_ALLOWANCE_SECONDS)
                <= work.deadline
                and start + timedelta(seconds=work.profile.duration_seconds)
                <= problem.intervals[-1].start + timedelta(hours=problem.intervals[-1].duration_h),
                "Appliance time window",
            )
    previous = {
        battery_entry.device_id: battery_entry.battery.initial_kwh
        for battery_entry in problem.batteries
    }
    purchase = sale = wear = 0
    groups = {}
    settled = tuple(
        settle(export_bonus, tuple(flow_item.grid_export for flow_item in plan.flows))
        for export_bonus in problem.export_bonuses
    )
    for interval_index, (step, flow) in enumerate(zip(problem.intervals, plan.flows, strict=True)):
        duration_hours = step.duration_h
        battery_flows = {battery_entry.device_id: battery_entry for battery_entry in flow.batteries}
        inverter_flows = {
            inverter_flow.inverter_id: inverter_flow for inverter_flow in flow.inverters
        }
        solar_flows = {solar_flow.source_id: solar_flow for solar_flow in flow.solar}
        require(
            len(battery_flows) == len(flow.batteries) == len(problem.batteries)
            and set(battery_flows) == set(previous),
            "Battery identities",
        )
        require(
            len(inverter_flows) == len(flow.inverters) == len(problem.inverters)
            and set(inverter_flows)
            == {inverter_item.inverter_id for inverter_item in problem.inverters},
            "Inverter identities",
        )
        require(
            len(solar_flows) == len(flow.solar) == len(problem.pv_sources)
            and set(solar_flows) == {pv_source.source_id for pv_source in problem.pv_sources},
            "PV identities",
        )
        for value, limit in (
            (flow.grid_import, problem.grid_import_limit_w * duration_hours / WATTS_PER_KW),
            (flow.grid_export, problem.grid_export_limit_w * duration_hours / WATTS_PER_KW),
        ):
            require(isfinite(value) and -tolerance <= value <= limit + tolerance, "Grid bound")
        require(min(flow.grid_import, flow.grid_export) <= tolerance, "Grid direction")
        for bonus, result in zip(problem.export_bonuses, settled, strict=True):
            if not bonus.prevent_battery_export or not bonus.prevent_battery_export[interval_index]:
                continue
            previous_counter = (
                bonus.initial_kwh
                if interval_index == 0
                else result.counters_kwh[interval_index - 1]
                if bonus.periods[interval_index] == bonus.periods[interval_index - 1]
                else 0
            )
            if bonus.caps_kwh[interval_index] - previous_counter > tolerance:
                require(
                    min(flow.grid_export, sum(battery.discharge_kwh for battery in flow.batteries))
                    <= tolerance,
                    "Battery export during protected bonus window",
                )
        require(
            close(
                flow.grid_import
                + sum(inverter_flow.export_kwh for inverter_flow in inverter_flows.values()),
                step.load_kwh
                + sum(vehicle.ac_kwh for vehicle in flow.ev)
                + sum(store.ac_kwh for store in flow.thermal)
                + sum(
                    work.profile.energy(
                        starts[work.task_id],
                        step.start,
                        step.start + timedelta(hours=step.duration_h),
                    )
                    for work in problem.appliances
                    if work.task_id in starts
                )
                + flow.grid_export
                + sum(inverter_flow.import_kwh for inverter_flow in inverter_flows.values()),
            ),
            "AC conservation",
        )
        commands = []
        for unit in problem.batteries:
            battery, battery_flow = unit.battery, battery_flows[unit.device_id]
            require(
                all(
                    isfinite(value_item) and value_item >= -tolerance
                    for value_item in (
                        battery_flow.charge_kwh,
                        battery_flow.discharge_kwh,
                        battery_flow.end_kwh,
                    )
                ),
                "Finite battery energy",
            )
            require(
                battery_flow.charge_kwh
                <= battery.charge_limit_w * duration_hours / WATTS_PER_KW + tolerance
                and battery_flow.discharge_kwh
                <= battery.discharge_limit_w * duration_hours / WATTS_PER_KW + tolerance
                and min(battery_flow.charge_kwh, battery_flow.discharge_kwh) <= tolerance,
                "Battery terminal bounds",
            )
            require(
                min(battery.minimum_kwh, previous[unit.device_id]) - tolerance
                <= battery_flow.end_kwh
                <= battery.capacity_kwh + tolerance,
                "Battery reserve",
            )
            require(
                close(
                    battery_flow.end_kwh,
                    previous[unit.device_id]
                    + battery_flow.charge_kwh * battery.charge_efficiency
                    - battery_flow.discharge_kwh / battery.discharge_efficiency,
                ),
                "Stored energy conservation",
            )
            previous[unit.device_id] = battery_flow.end_kwh
            power = (
                (battery_flow.discharge_kwh - battery_flow.charge_kwh)
                * WATTS_PER_KW
                / duration_hours
            )
            require(abs(power - round(power)) <= WHOLE_WATT_TOLERANCE, "Whole-watt command")
            commands.append((unit.device_id, round(power)))
            wear += (
                battery.wear_cost_per_kwh
                * battery_flow.discharge_kwh
                / battery.discharge_efficiency
            )
        for source in problem.pv_sources:
            solar_flow = solar_flows[source.source_id]
            available = dict(step.pv_sources)[source.source_id]
            require(
                type(solar_flow.enabled) is bool and (source.can_curtail or solar_flow.enabled),
                "PV control permission",
            )
            require(
                isfinite(solar_flow.used_kwh)
                and -tolerance <= solar_flow.used_kwh <= available + tolerance,
                "PV available energy",
            )
            if not solar_flow.enabled:
                require(abs(solar_flow.used_kwh) <= tolerance, "Disabled PV produces")
            elif available - solar_flow.used_kwh > tolerance:
                require(inverter_flows[source.inverter_id].clipping, "Undeclared PV clipping")
            commands.append((source.source_id, solar_flow.enabled))
        for inverter in problem.inverters:
            inverter_flow = inverter_flows[inverter.inverter_id]
            cap = inverter.ac_limit_w * duration_hours / WATTS_PER_KW
            require(
                type(inverter_flow.clipping) is bool
                and all(
                    isfinite(value_item) and -tolerance <= value_item <= cap + tolerance
                    for value_item in (inverter_flow.import_kwh, inverter_flow.export_kwh)
                )
                and min(inverter_flow.import_kwh, inverter_flow.export_kwh) <= tolerance,
                "Converter bounds",
            )
            local = [
                (battery_flows[battery_entry.device_id], battery_entry.dc_efficiency)
                for battery_entry in problem.batteries
                if battery_entry.inverter_id == inverter.inverter_id
            ]
            require(
                close(
                    sum(
                        solar_flows[pv_source.source_id].used_kwh
                        for pv_source in problem.pv_sources
                        if pv_source.inverter_id == inverter.inverter_id
                    )
                    + sum(
                        battery_flow_entry.discharge_kwh * efficiency
                        for battery_flow_entry, efficiency in local
                    )
                    + inverter_flow.import_kwh * inverter.efficiency,
                    sum(
                        battery_flow_entry.charge_kwh / efficiency
                        for battery_flow_entry, efficiency in local
                    )
                    + inverter_flow.export_kwh / inverter.efficiency,
                ),
                "DC conservation",
            )
            if inverter_flow.clipping:
                require(
                    close(inverter_flow.export_kwh, cap), "Clipping requires saturated AC output"
                )
                for unit in problem.batteries:
                    if unit.inverter_id == inverter.inverter_id:
                        battery = battery_flows[unit.device_id]
                        require(
                            close(battery.end_kwh, unit.battery.capacity_kwh)
                            or close(
                                battery.charge_kwh,
                                unit.battery.charge_limit_w * duration_hours / WATTS_PER_KW,
                            ),
                            "Clipping requires saturated storage acceptance",
                        )
        if step.control_group:
            require(
                groups.setdefault(step.control_group, commands) == commands,
                "Control group command changed",
            )
        purchase += step.buy_price * flow.grid_import
        sale += step.sell_price * flow.grid_export
    require(len(settled) == len(plan.export_bonus_results), "Missing bonus evidence")
    revenue = 0
    for rule, expected, reported in zip(
        problem.export_bonuses, settled, plan.export_bonus_results, strict=True
    ):
        require(
            expected.rule_id == reported.rule_id
            and len(expected.quota_kwh) == len(reported.quota_kwh)
            and len(expected.counters_kwh) == len(reported.counters_kwh),
            "Bonus result shape",
        )
        require(
            all(
                close(claimed_bonus, expected_bonus)
                for claimed_bonus, expected_bonus in zip(
                    (*expected.quota_kwh, *expected.counters_kwh),
                    (*reported.quota_kwh, *reported.counters_kwh),
                    strict=True,
                )
            ),
            "Bonus settlement",
        )
        revenue += sum(
            rate * quota_kwh for rate, quota_kwh in zip(rule.rates, expected.quota_kwh, strict=True)
        )
    cash = purchase - sale - revenue + wear
    terminal = sum(curve.value(previous[curve.device_id]) for curve in problem.terminal_valuations)
    costs = Costs(purchase, sale, revenue, wear, cash, terminal, cash - terminal)
    require(
        all(
            close(getattr(costs, field.name), getattr(plan.costs, field.name))
            for field in fields(costs)
        ),
        "Financial totals",
    )
    return costs
