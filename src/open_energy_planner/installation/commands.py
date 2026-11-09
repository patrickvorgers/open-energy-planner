"""Publish independent per-device commands only after full physical and financial validation."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from uuid import uuid4

from open_energy_planner.core.units import FULL_SOC_PERCENT, WATTS_PER_KW

from ..build_info import RELEASE
from ..planning_policy import command_deadline

WHOLE_WATT_TOLERANCE = 1e-05
ZERO_GRID_TOLERANCE_KWH = 1e-07


def plan_document(snapshot, result, costs, now):
    """Build a complete compatible envelope with typed commands and immutable physical evidence.

    Require matched EV/thermal source identities and complete component coverage
    for emitted controls. The independently validated result determines which
    optional cycles actually emit a schedule. Omitted cycles do not block ordinary
    household publication. Preserve all original work in the archived snapshot.

    Raises:
        ValueError: Controlled source ownership, coverage or command mapping is invalid.
    """
    from open_energy_planner.demand.calculation.composition import validate_components

    problem = snapshot.problem
    starts = dict(result.plan.appliance_starts)
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
    steps = []
    stored = {unit.device_id: unit.battery.initial_kwh for unit in problem.batteries}
    group_followers = {}
    for interval_index, (interval, flow) in enumerate(
        zip(problem.intervals, result.plan.flows, strict=True)
    ):
        group = interval.control_group or interval_index
        # Following is a cost-neutral execution choice only for an independently
        # validated zero-import/export result. Keep its forecast battery flows,
        # losses, costs and limits unchanged. Fast physical regulation stays in HA.
        eligible = {
            battery.device_id
            for battery in problem.batteries
            if battery.can_follow_grid
            and stored[battery.device_id] >= battery.battery.minimum_kwh
            and abs(flow.grid_import) <= ZERO_GRID_TOLERANCE_KWH
            and abs(flow.grid_export) <= ZERO_GRID_TOLERANCE_KWH
        }
        # Billing splits must not create a new following/fixed decision inside one original control.
        group_followers.setdefault(group, eligible).intersection_update(eligible)
        stored.update({battery.device_id: battery.end_kwh for battery in flow.batteries})
    previous_group = None
    for interval_index, (interval, flow) in enumerate(
        zip(problem.intervals, result.plan.flows, strict=True)
    ):
        commands = {}
        followers = group_followers[interval.control_group or interval_index]
        for battery in flow.batteries:
            power = (
                (battery.discharge_kwh - battery.charge_kwh) * WATTS_PER_KW / interval.duration_h
            )
            if abs(power - round(power)) > WHOLE_WATT_TOLERANCE:
                raise ValueError("Fractional device command")
            following = battery.device_id in followers
            commands[battery.device_id] = {
                "kind": "battery",
                "mode": "follow_grid_target" if following else "fixed_power",
                "power_w": None if following else round(power),
            }
        for source in problem.pv_sources:
            if source.can_curtail:
                solar_flow = next(
                    solar_flow_entry
                    for solar_flow_entry in flow.solar
                    if solar_flow_entry.source_id == source.source_id
                )
                if source.source_id in commands:
                    raise ValueError("PV and battery command identities collide")
                commands[source.source_id] = {"kind": "solar", "enabled": solar_flow.enabled}
        for work, vehicle_flow in zip(problem.ev_work, flow.ev, strict=True):
            commands[work.point_id] = {
                "kind": "ev",
                "power_w": 0
                if abs(vehicle_flow.power_w) <= WHOLE_WATT_TOLERANCE
                else round(vehicle_flow.power_w)
                if work.control == "on_off"
                else min(work.maximum_w, max(work.minimum_w, vehicle_flow.power_w)),
                "vehicle_id": work.vehicle_id,
                "session_id": work.session_id,
            }
        for work, heating in zip(problem.thermal_work, flow.thermal, strict=True):
            if work.device_id in commands:
                raise ValueError("Thermal command identity collides with another device")
            commands[work.device_id] = {"kind": "thermal", "power_w": round(heating.power_w)}
        step = {
            "start": interval.start.isoformat(),
            "end": (interval.start + timedelta(hours=interval.duration_h)).isoformat(),
            "grid_active": bool(followers),
            "devices": commands,
        }
        if (
            steps
            and interval.control_group is not None
            and interval.control_group == previous_group
            and steps[-1]["end"] == step["start"]
            and all(steps[-1][key] == step[key] for key in ("devices", "grid_active"))
        ):
            steps[-1]["end"] = step["end"]
        else:
            steps.append(step)
        previous_group = interval.control_group
    if problem.ev_work:
        compact = []
        for step in steps:
            if compact and all(compact[-1][key] == step[key] for key in ("devices", "grid_active")):
                compact[-1]["end"] = step["end"]
            else:
                compact.append(step)
        steps = compact
    return {
        "schema": 2,
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
        "ev_context": {
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
                for work in problem.ev_work
            ],
        },
        "independent_costs": asdict(costs),
        "expected_flows": [asdict(flow_item) for flow_item in result.plan.flows],
        "expected_bonuses": [
            asdict(bonus_result) for bonus_result in result.plan.export_bonus_results
        ],
        "steps": steps,
        "appliance_schedule": [
            {
                "task_id": work.task_id,
                "entity_id": work.entity_id,
                "start": starts[work.task_id].isoformat(),
                "end": (
                    starts[work.task_id] + timedelta(seconds=work.profile.duration_seconds)
                ).isoformat(),
                "energy_kwh": work.profile.energy_kwh,
                "interval_kwh": [
                    work.profile.energy(
                        starts[work.task_id],
                        step.start,
                        step.start + timedelta(hours=step.duration_h),
                    )
                    for step in problem.intervals
                ],
            }
            for work in problem.appliances
            if work.task_id in starts
        ],
        "ev_schedule": [
            {
                "point_id": work.point_id,
                "vehicle_id": work.vehicle_id,
                "session_id": work.session_id,
                "deadline": work.deadline.isoformat(),
                "initial_soc": work.initial_soc,
                "target_soc": work.target_soc,
                "required_kwh": work.required_kwh,
                "planned_kwh": sum(
                    next(row.stored_kwh for row in flow.ev if row.session_id == work.session_id)
                    for flow in result.plan.flows
                ),
                "predicted_soc": work.initial_soc
                + sum(
                    next(row.stored_kwh for row in flow.ev if row.session_id == work.session_id)
                    for flow in result.plan.flows
                )
                / work.capacity_kwh
                * FULL_SOC_PERCENT,
                "shortfall_kwh": max(
                    0,
                    work.required_kwh
                    - sum(
                        next(row.stored_kwh for row in flow.ev if row.session_id == work.session_id)
                        for flow in result.plan.flows
                    ),
                ),
            }
            for work in problem.ev_work
        ],
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
