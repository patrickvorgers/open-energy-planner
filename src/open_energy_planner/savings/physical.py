"""Continuous measured self-consumption physics, independent of contracts and bonus accounting."""

from datetime import timedelta
from math import fsum

from open_energy_planner.core.units import SECONDS_PER_HOUR

from ..installation.model import slice_interval
from ..optimization.domain import Interval
from .model import ENERGY_TOLERANCE_KWH
from .reference import dispatch


def transition(problem, frame, previous):
    """Preserve independent stocks across measured hardware changes without pricing energy.

    Args:
        problem: Frozen installation physics. Battery capacities are in kWh.
        frame: Validated measured battery start/end energy in kWh, keyed by device ID.
        previous: None for the common measured start, otherwise a checkpoint with
            end, reference_stored, actual_stored and capacities. Stock/capacity
            mappings contain kWh. Reference stock never follows actual SoC.

    Returns:
        A (reference_stored, movements) tuple. Movement records identify device_id
        and scenario, signed kwh change, before/after stock, acquisition_cost of
        zero and installation_change reason. Monetary valuation belongs to billing.
        This function does not mutate the frame, checkpoint or archive.

    Raises:
        ValueError: Measured battery energy exceeds its configured capacity.
    """
    reference, movements = {}, []
    for scenario in ("actual", "reference"):
        stored, changed = transition_scenario(problem, frame, previous, scenario)
        movements.extend(changed)
        if scenario == "reference":
            reference = stored
    return reference, movements


def transition_scenario(problem, frame, previous, scenario):
    """Track one scenario's hardware changes without requiring the other scenario's stock.

    All stocks, capacities and signed movement quantities are kWh. ``previous``
    contains actual_stored and capacities, plus reference_stored only when the
    reference is requested. None initializes the explicitly common measured start.
    The result is (remaining stock, external movements). Added/replaced capacity
    receives only the measured introduced energy. Inputs remain unchanged.
    """
    capacities = {unit.device_id: unit.battery.capacity_kwh for unit in problem.batteries}
    for key, capacity in capacities.items():
        if max(frame.battery_start[key], frame.battery_end[key]) > capacity + ENERGY_TOLERANCE_KWH:
            raise ValueError("Measured battery energy exceeds capacity")
    if previous is None:
        return dict(frame.battery_start), []
    old = previous["actual_stored"] if scenario == "actual" else previous["reference_stored"]
    stored, movements = {}, []
    for key in old.keys() | capacities.keys():
        before = old.get(key, 0)
        if key not in capacities:
            after = 0
        elif key not in old:
            after = frame.battery_start[key]
        elif capacities[key] != previous["capacities"][key]:
            introduced = frame.battery_start[key] - min(
                previous["actual_stored"][key], capacities[key]
            )
            after = min(capacities[key], max(0, min(before, capacities[key]) + introduced))
        else:
            after = before
        if key in capacities:
            stored[key] = after
        if after != before:
            movements.append(
                {
                    "device_id": key,
                    "scenario": scenario,
                    "kwh": after - before,
                    "before": before,
                    "after": after,
                    "acquisition_cost": 0,
                    "reason": "installation_change",
                }
            )
    return stored, movements


def advance(problem, frame, bindings, previous):
    """Calculate one contiguous measured frame without contracts, counters or persistence.

    Args:
        problem: Frozen physical inputs, limits and original available-PV forecasts.
        frame: Validated interval with kWh meter deltas and battery endpoints.
        bindings: Disjoint signed household terms, import/export channels, solar
            and battery associations, plus any recorded possible curtailment.
        previous: None at first initialization, otherwise a checkpoint containing
            end, reference_stored, actual_stored and capacities, with energy in kWh.

    Returns:
        A detached calculation containing reference dispatch flows, reference_start
        stock, installation movements, load_kwh, estimated_pv and the next state
        checkpoint. Meter deltas are nonnegative. Household term signs reconstruct
        demand. Grid import/export remain separate nonnegative quantities.
        The calculation is pure and never writes a checkpoint.

    Raises:
        ValueError: A required meter, battery endpoint or solar association is
            missing, household balance is negative, continuity has a gap, capacity
            is exceeded, available PV cannot be established during curtailment,
            or dispatch breaches a physical grid limit. Callers retain the last
            valid checkpoint when handling these failures.
    """
    step, estimated_pv = measured_step(problem, frame, bindings)
    if previous and previous["end"] != frame.start.isoformat():
        raise ValueError("Reference continuity requires the missing interval first")
    reference_stored, movements = transition(problem, frame, previous)
    reference = dispatch(problem, step, reference_stored)
    return {
        "reference": reference,
        "reference_start": reference_stored,
        "movements": movements,
        "load_kwh": step.load_kwh,
        "estimated_pv": estimated_pv,
        "state": {
            "end": frame.end.isoformat(),
            "reference_stored": reference["stored"],
            "actual_stored": frame.battery_end,
            "capacities": {unit.device_id: unit.battery.capacity_kwh for unit in problem.batteries},
        },
    }


def measured_step(problem, frame, bindings):
    """Resolve measured demand and available PV independently of a reference battery's state.

    Meter channels are AC kWh. Source energy in the returned interval is DC kWh.
    Original forecasts may only supply explicitly labelled unavailable/curtailed
    PV potential, never replace measured household load or grid imports.
    """
    channels = frame.channels
    required = (
        {energy_term["entity_id"] for energy_term in bindings["terms"]}
        | set(bindings["imports"])
        | set(bindings["exports"])
        | {solar_flow["entity_id"] for solar_flow in bindings["solar"]}
        | {battery["energy_entity"] for battery in bindings["batteries"]}
    )
    if not required <= channels.keys():
        raise ValueError("Required settlement meter is missing")
    load = fsum(
        channels[energy_term["entity_id"]] * energy_term["sign"]
        for energy_term in bindings["terms"]
    )
    if load < -ENERGY_TOLERANCE_KWH:
        raise ValueError("Measured household balance is negative")
    ids = {battery.device_id for battery in problem.batteries}
    if set(frame.battery_start) != ids or set(frame.battery_end) != ids:
        raise ValueError("Battery endpoint coverage is missing")
    pv = []
    estimated_pv = False
    inverters = {inverter.inverter_id: inverter for inverter in problem.inverters}
    for source in problem.pv_sources:
        binding = next(
            (
                solar_flow
                for solar_flow in bindings["solar"]
                if solar_flow["source_id"] == source.source_id
            ),
            None,
        )
        if binding is None:
            raise ValueError("Settlement solar binding is missing for a planned source")
        measured_dc = channels[binding["entity_id"]] / inverters[source.inverter_id].efficiency
        projected = covered = 0.0
        for row in problem.intervals:
            left, right = (
                max(frame.start, row.start),
                min(frame.end, row.start + timedelta(hours=row.duration_h)),
            )
            if left < right:
                projected += dict(slice_interval(row, left, right).pv_sources)[source.source_id]
                covered += (right - left).total_seconds()
        hybrid = inverters[source.inverter_id].hybrid and any(
            battery.inverter_id == source.inverter_id for battery in problem.batteries
        )
        if (source.can_curtail and bindings.get("curtailed", {}).get(source.source_id)) or (
            hybrid and (projected > 0 or measured_dc > 0)
        ):
            if covered < (frame.end - frame.start).total_seconds():
                raise ValueError("Available PV estimate is missing during curtailment")
            amount = max(projected, measured_dc)
            estimated_pv = True
        else:
            amount = measured_dc
        pv.append((source.source_id, amount))
    step = Interval(
        frame.start,
        (frame.end - frame.start).total_seconds() / SECONDS_PER_HOUR,
        0,
        0,
        max(0, load),
        fsum(value for _, value in pv),
        pv_sources=tuple(pv),
    )
    return step, estimated_pv
