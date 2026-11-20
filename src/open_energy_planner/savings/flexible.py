"""Price-independent service for Planning references, never executable device commands."""

from dataclasses import replace
from datetime import datetime, timedelta
from math import exp, expm1, fsum

from open_energy_planner.appliances.ev.model import EV_ENERGY_TOLERANCE_KWH, EVWork, delivered_power
from open_energy_planner.appliances.thermal.model import ThermalWork, active_hours
from open_energy_planner.core.units import SECONDS_PER_HOUR, WATTS_PER_KW

from .context import physical_context
from .model import ENERGY_TOLERANCE_KWH

REFERENCE_POLICY = "unoptimized-loads-v1"
REFERENCE_POWER_TOLERANCE_W = 1e-5
# A priority solve and its frozen lower bound each admit service tolerance.
# Independent physical replay retains its own energy roundoff allowance.
REFERENCE_SERVICE_TOLERANCE_KWH = 2 * EV_ENERGY_TOLERANCE_KWH + ENERGY_TOLERANCE_KWH
SERVICE_ISSUE = (
    "The unoptimized reference cannot provide the same charging and comfort service "
    "within the available period and connection limits. Its benefit is unavailable."
)


def _vehicle(document):
    """Restore the original charging request, retaining its exact availability and target."""
    return EVWork(
        **{
            **document,
            "ready_at": datetime.fromisoformat(document["ready_at"]),
            "deadline": datetime.fromisoformat(document["deadline"]),
        }
    )


def _heater(document):
    """Restore the original heat store and its demand without borrowing planned heating."""
    return ThermalWork(
        **{
            **document,
            "ready_at": datetime.fromisoformat(document["ready_at"]),
            "state_at": datetime.fromisoformat(document["state_at"])
            if document.get("state_at")
            else None,
            "demand_kwh": tuple(document["demand_kwh"]),
        }
    )


def _heat(work, step, interval_index, temperature, available_w):
    """Maintain the requested temperature without price lookahead, using mean thermostat duty.

    Conventional thermostat cycling can stop inside a billing segment. The mean
    intake follows the same exponential heat balance as the original model,
    stays within the heater rating and never exceeds the maximum temperature.
    Reserve the full on-power for concurrent loads whenever the thermostat runs.
    These mean flows do not authorize an OEP heater command.
    """
    hours = active_hours(work, step)
    energy = unmet = 0.0
    if hours:
        exponent = work.loss_w_per_c / WATTS_PER_KW * hours / work.capacity_kwh_per_c
        decay = exp(-exponent)
        factor = -expm1(-exponent) / exponent if exponent else 1.0
        passive = (
            work.ambient_c
            + (temperature - work.ambient_c) * decay
            - work.demand_kwh[interval_index] * factor / work.capacity_kwh_per_c
        )
        start = max(step.start, work.state_at or step.start)
        if start >= work.ready_at and work.heater_w <= available_w + REFERENCE_POWER_TOLERANCE_W:
            needed = max(0, work.terminal_c - passive) * work.capacity_kwh_per_c
            energy = min(
                needed / factor / work.efficiency,
                work.heater_w * hours / WATTS_PER_KW,
            )
        temperature = passive + work.efficiency * energy * factor / work.capacity_kwh_per_c
        if temperature < 0:
            unmet = -temperature * work.capacity_kwh_per_c / factor
            temperature = 0.0
    return {
        "device_id": work.device_id,
        "ac_kwh": energy,
        "end_c": temperature,
        "shortfall_kwh": max(0, work.minimum_c - temperature) * work.capacity_kwh_per_c,
        "unmet_draw_kwh": unmet,
        "power_w": energy * WATTS_PER_KW / step.duration_h,
    }


def _charge(work, step, remaining, available_w):
    """Charge immediately at the allowed rating, with an exact final stop at the target.

    The last pulse can end inside a segment. power_w is its mean for the chart,
    while connection admission uses the actual on-power. No prices are read.
    """
    end = step.start + timedelta(hours=step.duration_h)
    hours = (
        max(0, (min(end, work.deadline) - max(step.start, work.ready_at)).total_seconds())
        / SECONDS_PER_HOUR
    )
    power = min(work.maximum_w, max(0, int(available_w + REFERENCE_POWER_TOLERANCE_W)))
    if (
        power < work.minimum_w
        or (work.control == "on_off" and power < work.maximum_w)
        or remaining <= ENERGY_TOLERANCE_KWH
    ):
        power = 0.0
    rate = delivered_power(power, work.delivery_points) / WATTS_PER_KW
    duration = min(hours, remaining / rate) if rate else 0.0
    energy = power * duration / WATTS_PER_KW
    return {
        "session_id": work.session_id,
        "ac_kwh": energy,
        "stored_kwh": rate * duration,
        "power_w": energy * WATTS_PER_KW / step.duration_h,
    }, power if duration else 0.0


def reference_context(plan):
    """Return non-price-driven loads and detached EV/thermal chart rows for a new plan.

    Cycles use their earliest permitted start, retaining fixed running starts.
    Thermostats have first claim on available grid plus usable AC solar power.
    Vehicles then charge in deadline/identity order. Use full on-power when
    admitting concurrent devices, rather than overlapping averaged final pulses.
    Missing service makes the financial comparison unavailable instead of
    crediting the reference for not charging or heating. Original plans are read
    without mutation. Legacy callers retain their separate fixed-service path.
    """
    data = plan["input_snapshot"]["problem"]
    stripped = {
        "input_snapshot": {"problem": {**data, "ev_work": (), "thermal_work": ()}},
        "appliance_schedule": plan.get("appliance_schedule", ()),
    }
    problem = physical_context(stripped, immediate_appliances=True)
    vehicles = sorted(
        (_vehicle(document) for document in data.get("ev_work", ())),
        key=lambda work: (work.deadline, work.session_id),
    )
    heaters = sorted(
        (_heater(document) for document in data.get("thermal_work", ())),
        key=lambda work: work.device_id,
    )
    remaining = {work.session_id: work.required_kwh for work in vehicles}
    temperatures = {work.device_id: work.initial_c for work in heaters}
    intervals, services = [], []
    for interval_index, step in enumerate(problem.intervals):
        solar = fsum(
            min(
                fsum(
                    dict(step.pv_sources)[source.source_id]
                    for source in problem.pv_sources
                    if source.inverter_id == inverter.inverter_id
                )
                * inverter.efficiency,
                inverter.ac_limit_w * step.duration_h / WATTS_PER_KW,
            )
            for inverter in problem.inverters
        )
        available_w = max(
            0,
            problem.grid_import_limit_w + (solar - step.load_kwh) * WATTS_PER_KW / step.duration_h,
        )
        thermal = []
        for work in heaters:
            row = _heat(work, step, interval_index, temperatures[work.device_id], available_w)
            temperatures[work.device_id] = row["end_c"]
            thermal.append(row)
            if row["ac_kwh"] > ENERGY_TOLERANCE_KWH:
                available_w = max(0, available_w - work.heater_w)
        ev = []
        for work in vehicles:
            row, on_power = _charge(work, step, remaining[work.session_id], available_w)
            remaining[work.session_id] -= row["stored_kwh"]
            available_w = max(0, available_w - on_power)
            ev.append(row)
        services.append({"ev": ev, "thermal": thermal})
        intervals.append(
            replace(
                step,
                load_kwh=step.load_kwh + fsum(row["ac_kwh"] for row in (*ev, *thermal)),
            )
        )
    return replace(problem, intervals=tuple(intervals)), services


def service_issue(plan, services):
    """Refuse a monetary benefit when charging, comfort or required ending stock are incomparable."""
    original = plan["expected_flows"]
    data = plan["input_snapshot"]["problem"]
    for work in data.get("ev_work", ()):
        identity = work["session_id"]
        totals = [
            fsum(
                row["stored_kwh"]
                for flow in rows
                for row in flow.get("ev", ())
                if row["session_id"] == identity
            )
            for rows in (services, original)
        ]
        if abs(totals[0] - totals[1]) > REFERENCE_SERVICE_TOLERANCE_KWH:
            return SERVICE_ISSUE
    for work in data.get("thermal_work", ()):
        identity = work["device_id"]
        paired = [
            next(row for row in flow["thermal"] if row["device_id"] == identity)
            for rows in (services, original)
            for flow in rows
        ]
        baseline, optimized = paired[: len(services)], paired[len(services) :]
        for key in ("shortfall_kwh", "unmet_draw_kwh"):
            totals = [
                fsum(
                    row[key] * (step["duration_h"] if key == "shortfall_kwh" else 1)
                    for row, step in zip(rows, data["intervals"], strict=True)
                )
                for rows in (baseline, optimized)
            ]
            if abs(totals[0] - totals[1]) > REFERENCE_SERVICE_TOLERANCE_KWH:
                return SERVICE_ISSUE
        deficits = [
            max(0, work["terminal_c"] - rows[-1]["end_c"]) * work["capacity_kwh_per_c"]
            for rows in (baseline, optimized)
        ]
        if abs(deficits[0] - deficits[1]) > REFERENCE_SERVICE_TOLERANCE_KWH:
            return SERVICE_ISSUE
    return None
