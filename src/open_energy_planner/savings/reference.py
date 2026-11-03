"""Deterministic self-consumption simulation, independent of prices and optimizer."""

from math import fsum

from open_energy_planner.core.units import WATTS_PER_KW

from .model import ENERGY_TOLERANCE_KWH


def dispatch(problem, step, stored):
    """Follow household demand with stable battery ordering and actual shared AC/DC limits.

    PV remains enabled at negative prices. Absorb local DC clipping first, then
    surplus production, and discharge only against household deficits. This is a
    counterfactual simulation, never a command or a second price optimizer.
    """
    inverters = {inverter.inverter_id: inverter for inverter in problem.inverters}
    dc = {
        key: fsum(
            dict(step.pv_sources)[pv_source.source_id]
            for pv_source in problem.pv_sources
            if pv_source.inverter_id == key
        )
        for key in inverters
    }
    ac = {
        key: min(
            value * inverters[key].efficiency,
            inverters[key].ac_limit_w * step.duration_h / WATTS_PER_KW,
        )
        for key, value in dc.items()
    }
    surplus = fsum(ac.values()) - step.load_kwh
    states, wear = dict(stored), 0.0
    flows = {}
    for unit in sorted(problem.batteries, key=lambda battery_unit: battery_unit.device_id):
        battery, inv = unit.battery, inverters[unit.inverter_id]
        key = unit.inverter_id
        old = states[unit.device_id]
        if not -ENERGY_TOLERANCE_KWH <= old <= battery.capacity_kwh + ENERGY_TOLERANCE_KWH:
            raise ValueError("Reference battery state outside capacity")
        room = max(0, battery.capacity_kwh - old) / battery.charge_efficiency
        charge_limit = battery.charge_limit_w * step.duration_h / WATTS_PER_KW
        # Local clipping never reached the AC bus. Storing it sacrifices no load supply.
        clipped = max(0, dc[key] - max(0, ac[key]) / inv.efficiency) if inv.hybrid else 0
        direct = min(room, charge_limit, clipped * unit.dc_efficiency)
        dc[key] -= direct / unit.dc_efficiency
        room -= direct
        charge_limit -= direct
        charge, discharge = direct, 0.0
        if surplus > 0 and room > 0:
            if inv.hybrid and dc[key] > 0:
                extra = min(
                    room,
                    charge_limit,
                    surplus * unit.dc_efficiency / inv.efficiency,
                    ac[key] * unit.dc_efficiency / inv.efficiency,
                )
                reduction = extra / unit.dc_efficiency * inv.efficiency
                dc[key] -= extra / unit.dc_efficiency
                ac[key] -= reduction
            else:
                conversion = unit.dc_efficiency * inv.efficiency
                headroom = max(0, inv.ac_limit_w * step.duration_h / WATTS_PER_KW - abs(ac[key]))
                extra = min(room, charge_limit, min(surplus, headroom) * conversion)
                reduction = extra / conversion
                ac[key] -= reduction
            charge += extra
            surplus -= reduction
            room -= extra
            charge_limit -= extra
            # Once local PV is absorbed, the same converter may store remaining
            # surplus from a different inverter, within its shared AC rating.
            if surplus > 0 and room > 0 and charge_limit > 0 and ac[key] <= 0:
                conversion = unit.dc_efficiency * inv.efficiency
                headroom = max(0, inv.ac_limit_w * step.duration_h / WATTS_PER_KW - abs(ac[key]))
                extra = min(room, charge_limit, min(surplus, headroom) * conversion)
                charge += extra
                ac[key] -= extra / conversion
                surplus -= extra / conversion
        elif surplus < 0 and not charge:
            conversion = unit.dc_efficiency * inv.efficiency
            headroom = max(0, inv.ac_limit_w * step.duration_h / WATTS_PER_KW - ac[key])
            discharge = min(
                max(0, old - battery.minimum_kwh) * battery.discharge_efficiency,
                battery.discharge_limit_w * step.duration_h / WATTS_PER_KW,
                min(-surplus, headroom) / conversion,
            )
            supplied = discharge * conversion
            ac[key] += supplied
            surplus += supplied
        states[unit.device_id] = (
            old + charge * battery.charge_efficiency - discharge / battery.discharge_efficiency
        )
        wear += discharge / battery.discharge_efficiency * battery.cycle_fee / battery.capacity_kwh
        flows[unit.device_id] = {
            "charge_kwh": charge,
            "discharge_kwh": discharge,
            "end_kwh": states[unit.device_id],
        }
    imported, exported = max(0, -surplus), max(0, surplus)
    if (
        imported
        > problem.grid_import_limit_w * step.duration_h / WATTS_PER_KW + ENERGY_TOLERANCE_KWH
        or exported
        > problem.grid_export_limit_w * step.duration_h / WATTS_PER_KW + ENERGY_TOLERANCE_KWH
    ):
        raise ValueError("Uncontrolled reference exceeds configured grid limit")
    used_solar = {}
    available_solar = dict(step.pv_sources)
    for key, inverter in inverters.items():
        available = fsum(
            available_solar[source.source_id]
            for source in problem.pv_sources
            if source.inverter_id == key
        )
        terminal_use = fsum(
            flows[unit.device_id]["charge_kwh"] / unit.dc_efficiency
            - flows[unit.device_id]["discharge_kwh"] * unit.dc_efficiency
            for unit in problem.batteries
            if unit.inverter_id == key
        )
        used = (
            max(0, ac[key]) / inverter.efficiency
            - max(0, -ac[key]) * inverter.efficiency
            + terminal_use
        )
        # Proportional physical clipping describes the simulation, never measured source provenance.
        fraction = min(1, max(0, used / available)) if available > 0 else 0
        for source in problem.pv_sources:
            if source.inverter_id == key:
                used_solar[source.source_id] = available_solar[source.source_id] * fraction
    return {
        "import_kwh": imported,
        "export_kwh": exported,
        "wear": wear,
        "stored": states,
        "batteries": flows,
        "inverters": [
            {"inverter_id": key, "import_kwh": max(0, -value), "export_kwh": max(0, value)}
            for key, value in ac.items()
        ],
        "solar": [
            {"source_id": source_id, "used_kwh": used} for source_id, used in used_solar.items()
        ],
    }
