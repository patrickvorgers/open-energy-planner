"""Reconstruct typed physics from original plan evidence without reoptimizing it."""

from dataclasses import replace
from datetime import datetime, timedelta

from open_energy_planner.solar.calculation.model import SolarInverter

from ..installation.model import BatteryState, InstallationProblem, PVUnit
from ..installation.valuation import TerminalValuation
from ..optimization.domain import Battery, Interval


def physical_context(plan, *, immediate_appliances=False):
    """Read only installation physics. Tariffs, counter states and measurements have separate owners."""
    data = plan["input_snapshot"]["problem"]
    intervals = tuple(
        Interval(
            **{
                **row,
                "start": datetime.fromisoformat(row["start"]),
                "pv_sources": tuple(tuple(pair) for pair in row.get("pv_sources", ())),
            }
        )
        for row in data["intervals"]
    )
    if data.get("ev_work"):
        flows = plan["expected_flows"]
        if len(flows) != len(intervals):
            raise ValueError("EV service evidence is incomplete")
        intervals = tuple(
            replace(step, load_kwh=step.load_kwh + sum(row["ac_kwh"] for row in flow["ev"]))
            for step, flow in zip(intervals, flows, strict=True)
        )
    if data.get("thermal_work"):
        flows = plan["expected_flows"]
        if len(flows) != len(intervals):
            raise ValueError("Thermal service evidence is incomplete")
        intervals = tuple(
            replace(step, load_kwh=step.load_kwh + sum(row["ac_kwh"] for row in flow["thermal"]))
            for step, flow in zip(intervals, flows, strict=True)
        )
    if data.get("appliances"):
        from open_energy_planner.appliances.cycles.model import CycleProfile

        schedules = {row["task_id"]: row for row in plan.get("appliance_schedule", [])}
        for work in data["appliances"]:
            if not immediate_appliances and work["task_id"] not in schedules:
                continue
            cycle = CycleProfile(
                tuple(work["profile"]["power_w"]),
                work["profile"]["samples"],
                work["profile"]["evidence"],
            )
            start = (
                datetime.fromisoformat(work["fixed_start"])
                if work.get("fixed_start")
                else max(datetime.fromisoformat(work["ready_at"]), intervals[0].start)
                if immediate_appliances
                else datetime.fromisoformat(schedules[work["task_id"]]["start"])
            )
            intervals = tuple(
                replace(
                    step,
                    load_kwh=step.load_kwh
                    + cycle.energy(
                        start, step.start, step.start + timedelta(hours=step.duration_h)
                    ),
                )
                for step in intervals
            )
    curves = tuple(
        TerminalValuation(
            **{
                **row,
                "window_start": datetime.fromisoformat(row["window_start"]),
                "window_end": datetime.fromisoformat(row["window_end"]),
                "tariff_evidence": (),
            }
        )
        for row in data.get("terminal_valuations", ())
    )
    return InstallationProblem(
        intervals=intervals,
        inverters=tuple(SolarInverter(**row) for row in data["inverters"]),
        batteries=tuple(
            BatteryState(**{**row, "battery": Battery(**row["battery"])})
            for row in data["batteries"]
        ),
        pv_sources=tuple(PVUnit(**row) for row in data["pv_sources"]),
        grid_import_limit_w=data["grid_import_limit_w"],
        grid_export_limit_w=data["grid_export_limit_w"],
        terminal_valuations=curves,
    )
