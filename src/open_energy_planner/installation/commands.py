"""Publish independent per-device commands only after full physical and financial validation."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from math import ceil
from uuid import uuid4

from open_energy_planner.core.units import FULL_SOC_PERCENT, WATTS_PER_KW

from ..build_info import RELEASE
from ..planning_policy import command_deadline

PLAN_SCHEMA_VERSION = 3
WHOLE_WATT_TOLERANCE = 1e-05
CALIBRATION_ENERGY_ROUNDOFF_KWH = 1e-07
ZERO_GRID_MAX_EXCHANGE_W = 10


def _can_follow_grid(unit, stored):
    """Exclude batteries that need fixed commands for calibration or reserve recovery."""
    return (
        unit.can_follow_grid
        and not unit.calibration_charge
        and unit.calibration_hold_until is None
        and stored[unit.device_id] >= unit.battery.minimum_kwh
    )


def _group_followers(problem, flows, units):
    """Choose zero-grid followers only when every segment of their control group qualifies."""
    stored = {identity: unit.battery.initial_kwh for identity, unit in units.items()}
    group_followers = {}
    for interval_index, (interval, flow) in enumerate(zip(problem.intervals, flows, strict=True)):
        group = interval.control_group or interval_index
        # Use the same power margin even when replanning shortens the first interval.
        grid_exchange_w = (
            (abs(flow.grid_import) + abs(flow.grid_export)) * WATTS_PER_KW / interval.duration_h
        )
        eligible = set()
        if grid_exchange_w <= ZERO_GRID_MAX_EXCHANGE_W + WHOLE_WATT_TOLERANCE:
            eligible = {
                unit.device_id for unit in problem.batteries if _can_follow_grid(unit, stored)
            }
        # Billing splits must not create a new following/fixed decision inside one original control.
        group_followers.setdefault(group, eligible).intersection_update(eligible)
        stored.update({battery.device_id: battery.end_kwh for battery in flow.batteries})
    return group_followers


def _battery_commands(units, batteries, duration_h, group, followers, calibration_commands):
    """Map terminal DC energy to signed whole watts, keeping one calibration target per group."""
    commands = {}
    for battery in batteries:
        unit = units[battery.device_id]
        power = (battery.discharge_kwh - battery.charge_kwh) * WATTS_PER_KW / duration_h
        if (
            unit.calibration_charge
            and abs(battery.end_kwh - unit.battery.capacity_kwh) <= CALIBRATION_ENERGY_ROUNDOFF_KWH
        ):
            power = -ceil(max(0, -power) - WHOLE_WATT_TOLERANCE)
        if abs(power - round(power)) > WHOLE_WATT_TOLERANCE:
            raise ValueError("Fractional device command")
        if unit.calibration_charge:
            power = calibration_commands.setdefault((group, battery.device_id), round(power))
        following = battery.device_id in followers
        commands[battery.device_id] = {
            "kind": "battery",
            "mode": "follow_grid_target" if following else "fixed_power",
            "power_w": None if following else round(power),
        }
    return commands


def _ev_command_power(work, power_w):
    """Preserve true idle and serialize supported on/off or variable charging power."""
    if abs(power_w) <= WHOLE_WATT_TOLERANCE:
        return 0
    if work.control == "on_off":
        return round(power_w)
    return min(work.maximum_w, max(work.minimum_w, power_w))


def _add_non_battery_commands(problem, flow, commands):
    """Add PV, EV and thermal controls, preserving source identity and collision checks."""
    for source in problem.pv_sources:
        if not source.can_curtail:
            continue
        solar_flow = next(row for row in flow.solar if row.source_id == source.source_id)
        if source.source_id in commands:
            raise ValueError("PV and battery command identities collide")
        commands[source.source_id] = {"kind": "solar", "enabled": solar_flow.enabled}
    for work, vehicle_flow in zip(problem.ev_work, flow.ev, strict=True):
        commands[work.point_id] = {
            "kind": "ev",
            "power_w": _ev_command_power(work, vehicle_flow.power_w),
            "vehicle_id": work.vehicle_id,
            "session_id": work.session_id,
        }
    for work, heating in zip(problem.thermal_work, flow.thermal, strict=True):
        if work.device_id in commands:
            raise ValueError("Thermal command identity collides with another device")
        commands[work.device_id] = {"kind": "thermal", "power_w": round(heating.power_w)}


def _append_step(steps, step, *, require_adjacency):
    """Extend equal controls inside a group or compact them across an already continuous EV plan."""
    if (
        steps
        and (not require_adjacency or steps[-1]["end"] == step["start"])
        and all(steps[-1][key] == step[key] for key in ("devices", "grid_control_mode"))
    ):
        steps[-1]["end"] = step["end"]
    else:
        steps.append(step)


def _plan_steps(problem, flows):
    """Build device steps across billing boundaries, preserving each observable control change."""
    steps = []
    units = {unit.device_id: unit for unit in problem.batteries}
    group_followers = _group_followers(problem, flows, units)
    previous_group = None
    calibration_commands = {}
    for interval_index, (interval, flow) in enumerate(zip(problem.intervals, flows, strict=True)):
        group = interval.control_group or interval_index
        followers = group_followers[group]
        commands = _battery_commands(
            units, flow.batteries, interval.duration_h, group, followers, calibration_commands
        )
        _add_non_battery_commands(problem, flow, commands)
        step = {
            "start": interval.start.isoformat(),
            "end": (interval.start + timedelta(hours=interval.duration_h)).isoformat(),
            "grid_control_mode": "zero_grid" if followers else "inactive",
            "devices": commands,
        }
        if interval.control_group is not None and interval.control_group == previous_group:
            _append_step(steps, step, require_adjacency=True)
        else:
            steps.append(step)
        previous_group = interval.control_group
    if problem.ev_work:
        compact = []
        for step in steps:
            _append_step(compact, step, require_adjacency=False)
        steps = compact
    return steps


def _validate_controlled_components(snapshot, starts):
    """Require owned EV and thermal loads and verify coverage for scheduled appliance work."""
    from open_energy_planner.demand.calculation.composition import validate_components

    problem = snapshot.problem
    ev_sources = {
        tuple(row["work"][key] for key in ("point_id", "vehicle_id", "session_id")): row[
            "energy_entity"
        ]
        for row in snapshot.ev_evidence
    }
    thermal_sources = {
        row["configuration"]["id"]: row["configuration"]["energy_entity"]
        for row in snapshot.thermal_evidence
    }
    if (
        len(ev_sources) != len(snapshot.ev_evidence)
        or set(ev_sources)
        != {(work.point_id, work.vehicle_id, work.session_id) for work in problem.ev_work}
        or len(thermal_sources) != len(snapshot.thermal_evidence)
        or set(thermal_sources) != {work.device_id for work in problem.thermal_work}
    ):
        raise ValueError("Household component ownership unavailable")
    validate_components(
        replace(
            problem, appliances=tuple(work for work in problem.appliances if work.task_id in starts)
        ),
        snapshot.load_components,
        controlled_entities=tuple(ev_sources.values()) + tuple(thermal_sources.values()),
    )


def _ev_context(snapshot):
    """Bind executable EV commands to their original sessions, power bounds and deadlines."""
    return {
        "revision": snapshot.ev_revision,
        "work": [
            {
                **{
                    key: getattr(work, key)
                    for key in (
                        "point_id",
                        "vehicle_id",
                        "session_id",
                        "maximum_w",
                        "minimum_w",
                        "control",
                    )
                },
                "ready_at": work.ready_at.isoformat(),
                "deadline": work.deadline.isoformat(),
            }
            for work in snapshot.problem.ev_work
        ],
    }


def _appliance_schedule(problem, starts):
    """Project only admitted cycles, retaining their exact duration and interval energy."""
    schedule = []
    for work in problem.appliances:
        if work.task_id not in starts:
            continue
        start = starts[work.task_id]
        schedule.append(
            {
                "task_id": work.task_id,
                "entity_id": work.entity_id,
                "start": start.isoformat(),
                "end": (start + timedelta(seconds=work.profile.duration_seconds)).isoformat(),
                "energy_kwh": work.profile.energy_kwh,
                "interval_kwh": [
                    work.profile.energy(
                        start, interval.start, interval.start + timedelta(hours=interval.duration_h)
                    )
                    for interval in problem.intervals
                ],
            }
        )
    return schedule


def _ev_schedule(work_items, flows):
    """Report stored kWh, predicted SoC and remaining demand for each planned EV session."""
    schedule = []
    for work in work_items:
        planned_kwh = sum(
            next(row.stored_kwh for row in flow.ev if row.session_id == work.session_id)
            for flow in flows
        )
        schedule.append(
            {
                "point_id": work.point_id,
                "vehicle_id": work.vehicle_id,
                "session_id": work.session_id,
                "deadline": work.deadline.isoformat(),
                "initial_soc": work.initial_soc,
                "target_soc": work.target_soc,
                "required_kwh": work.required_kwh,
                "planned_kwh": planned_kwh,
                "predicted_soc": work.initial_soc
                + planned_kwh / work.capacity_kwh * FULL_SOC_PERCENT,
                "shortfall_kwh": max(0, work.required_kwh - planned_kwh),
            }
        )
    return schedule


def plan_document(snapshot, result, costs, now):
    """Build a complete compatible envelope with typed commands and immutable physical evidence.

    Require matched EV/thermal source identities and complete component coverage
    for emitted controls. The independently validated result determines which
    optional cycles actually emit a schedule. Omitted cycles do not block ordinary
    household publication. Preserve all original work in the archived snapshot.

    Raises:
        ValueError: Controlled source ownership, coverage or command mapping is invalid.
    """
    problem = snapshot.problem
    plan = result.plan
    starts = dict(plan.appliance_starts)
    _validate_controlled_components(snapshot, starts)
    steps = _plan_steps(problem, plan.flows)
    return {
        "schema": PLAN_SCHEMA_VERSION,
        "release": RELEASE,
        "plan_id": str(uuid4()),
        "config_revision": snapshot.config_revision,
        "generated_at": now.isoformat(),
        "valid_until": min(
            problem.intervals[-1].start + timedelta(hours=problem.intervals[-1].duration_h),
            command_deadline(now),
            snapshot.future_valid_until or command_deadline(now),
        ).isoformat(),
        "strategy": snapshot.strategy,
        "solution_status": result.status.value,
        "input_snapshot_id": snapshot.snapshot_id,
        "input_sha256": snapshot.digest,
        "input_snapshot": snapshot.record(),
        "command_resolution_w": 1,
        "battery_power_side": "battery_terminal_dc",
        "device_commands": True,
        "thermal_context": {
            "revision": snapshot.thermal_revision,
            "devices": [deepcopy(row["configuration"]) for row in snapshot.thermal_evidence],
        },
        "ev_context": _ev_context(snapshot),
        "independent_costs": asdict(costs),
        "expected_flows": [asdict(flow_item) for flow_item in plan.flows],
        "expected_bonuses": [asdict(bonus_result) for bonus_result in plan.export_bonus_results],
        "steps": steps,
        "appliance_schedule": _appliance_schedule(problem, starts),
        "ev_schedule": _ev_schedule(problem.ev_work, plan.flows),
    }


def execution_document(plan):
    """Keep native execution transport bounded, leaving original physical/financial evidence in the archive API."""
    if plan is None:
        return None
    return deepcopy(
        {
            key: value
            for key, value in plan.items()
            if key
            in {
                "schema",
                "release",
                "plan_id",
                "config_revision",
                "generated_at",
                "valid_until",
                "strategy",
                "solution_status",
                "input_snapshot_id",
                "input_sha256",
                "command_resolution_w",
                "battery_power_side",
                "device_commands",
                "steps",
                "ev_context",
                "thermal_context",
            }
        }
    )
