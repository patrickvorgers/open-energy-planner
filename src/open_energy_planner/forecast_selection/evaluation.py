"""Settle frozen shadow decisions with realized metering, never hindsight optimization."""

from dataclasses import replace
from datetime import datetime
from math import fsum

from open_energy_planner.core.units import SECONDS_PER_HOUR, WATTS_PER_KW
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.optimization.calculation.consumption import evaluate_consumption

from ..installation.model import InstallationPlan
from ..optimization.domain import Costs, Interval
from ..savings.accounting import bill, inventory
from ..savings.context import physical_context
from ..savings.model import ENERGY_TOLERANCE_KWH, PendingSettlement, parse_frame
from ..solar.learning import INVERTER_CLIPPING_THRESHOLD
from .policy import COMPARISON_SCENARIOS


def frozen_commands(steps, start, end):
    """Join identical commands across billing edges without inventing sub-quarter measurements."""
    cursor, commands = start, None
    for row in steps:
        left, right = (
            max(start, datetime.fromisoformat(row["start"])),
            min(end, datetime.fromisoformat(row["end"])),
        )
        if left >= right:
            continue
        if left != cursor:
            raise ValueError("Frozen command coverage is incomplete")
        if commands is not None and commands != row["devices"]:
            raise ValueError("Changing commands inside a measured quarter needs finer evidence")
        commands = row["devices"]
        cursor = right
        if cursor == end:
            return commands
    raise ValueError("Frozen command coverage is incomplete")


def realized_inputs(problem, bindings, records, start, end):
    """Require contiguous recorded quarters and independently observable available PV.

    Estimated hourly/boundary reconstruction, hybrid AC-only production, enabled
    curtailment or clipped production cannot prove available DC. These remain
    valid settlement inputs elsewhere but cannot promote a forecasting model.
    """
    cursor = start
    inverters = {row.inverter_id: row for row in problem.inverters}
    sources = {row["source_id"]: row for row in bindings["solar"]}
    result = []
    for record in records:
        frame = parse_frame(record["frame"])
        if frame.start != cursor or frame.end > end or frame.quality != "recorded":
            raise PendingSettlement("Recorded quarter evidence is incomplete")
        if record["context"]["bindings"] != bindings:
            raise ValueError("Energy bindings changed during model evaluation")
        load = fsum(frame.channels[row["entity_id"]] * row["sign"] for row in bindings["terms"])
        if load < -ENERGY_TOLERANCE_KWH:
            raise ValueError("Realized household balance is negative")
        duration = (frame.end - frame.start).total_seconds() / SECONDS_PER_HOUR
        totals = {
            identity: fsum(
                frame.channels[sources[source.source_id]["entity_id"]]
                * (
                    inverter.efficiency
                    if sources[source.source_id].get("measurement_boundary", "ac") == "dc"
                    else 1
                )
                for source in problem.pv_sources
                if source.inverter_id == identity
            )
            for identity, inverter in inverters.items()
        }
        pv = []
        for source in problem.pv_sources:
            inverter = inverters[source.inverter_id]
            if source.can_curtail or inverter.hybrid:
                raise ValueError("Available PV needs uncurtailed, independent production evidence")
            amount = frame.channels[sources[source.source_id]["entity_id"]]
            if (
                totals[source.inverter_id]
                >= inverter.ac_limit_w / WATTS_PER_KW * duration * INVERTER_CLIPPING_THRESHOLD
            ):
                raise ValueError("Clipped PV cannot establish available production")
            pv.append(
                (
                    source.source_id,
                    amount
                    if sources[source.source_id].get("measurement_boundary", "ac") == "dc"
                    else amount / inverter.efficiency,
                )
            )
        result.append(
            (
                frame,
                Interval(
                    frame.start,
                    duration,
                    0,
                    0,
                    max(0, load),
                    fsum(amount for _, amount in pv),
                    pv_sources=tuple(pv),
                ),
            )
        )
        cursor = frame.end
    if cursor != end:
        raise PendingSettlement("Recorded quarter evidence is incomplete")
    return tuple(result)


def score(capture, original, records, market_rows, contract, replay, seeds=()):
    """Score both retained command sequences with identical actuals, prices and inventory valuation.

    Billing reuses exact dated formula/bonus settlement. Each scenario maintains
    independent bonus counters and battery stock from the same opening positions.
    The common original terminal curves prevent a challenger rewarding itself
    with its own more optimistic ending-stock valuation.
    """
    problem = physical_context(original)
    start, end = (datetime.fromisoformat(capture[key]) for key in ("start", "end"))
    actual = realized_inputs(problem, original["comparison_bindings"], records, start, end)
    opening = actual[0][0].battery_start
    if set(opening) != {unit.device_id for unit in problem.batteries}:
        raise ValueError("Measured opening battery stock is missing")
    problem = replace(
        problem,
        batteries=tuple(
            replace(unit, battery=replace(unit.battery, initial_kwh=opening[unit.device_id]))
            for unit in problem.batteries
        ),
    )
    location = original["input_snapshot"].get("site_location")
    location = SiteLocation(**location) if location else None
    result = {}
    for scenario in COMPARISON_SCENARIOS:
        commands = capture[scenario]["steps"]
        stored = dict(opening)
        counters = {}
        bill(contract, actual[0][0], 0, 0, counters, seeds, market_rows, location)
        opening_value = inventory(problem, stored)
        purchase = sale = bonus = wear = 0.0
        flows, tariffs = [], []
        for frame, step in actual:
            command = frozen_commands(commands, frame.start, frame.end)
            flow, step_wear = replay(problem, step, stored, command)
            stored = {row.device_id: row.end_kwh for row in flow.batteries}
            amounts = bill(
                contract,
                frame,
                flow.grid_import,
                flow.grid_export,
                counters,
                seeds,
                market_rows,
                location,
            )
            purchase += amounts["purchase"]
            sale += amounts["sale"]
            bonus += amounts["bonus"]
            wear += step_wear
            tariffs.extend(amounts["tariffs"])
            flows.append(flow)
        consumption = evaluate_consumption(
            problem, InstallationPlan(tuple(flows), Costs(0, 0, 0, 0, 0, 0, 0))
        )
        result[scenario] = {
            "purchase": purchase,
            "sale": sale,
            "bonus": bonus,
            "wear": wear,
            "inventory_change": inventory(problem, stored) - opening_value,
            "economic_cost": purchase
            - sale
            - bonus
            + wear
            - (inventory(problem, stored) - opening_value),
            "corrected_import_kwh": consumption.corrected_import_kwh,
            "import_kwh": consumption.grid_import_kwh,
            "end_stored": stored,
            "counters": counters,
            "tariffs": tariffs,
        }
    return {
        "start": capture["start"],
        "end": capture["end"],
        "opening_stored": dict(opening),
        **result,
    }
