"""Attribute planned battery charging using the recorded DC and AC boundaries."""

from math import isfinite

ENERGY_ROUNDOFF_KWH = 1e-7


def _nonnegative(value):
    """Accept finite physical energy and clamp negligible negative solver roundoff."""
    if type(value) not in (float, int) or not isfinite(value) or value < -ENERGY_ROUNDOFF_KWH:
        raise ValueError("Invalid planned energy")
    return max(0.0, value)


def _efficiency(value):
    """Use only efficiencies with a valid physical conversion factor."""
    if not 0 < _nonnegative(value) <= 1:
        raise ValueError("Invalid conversion efficiency")
    return value


def battery_charge_sources(problem, flows):
    """Return attributed terminal kWh per battery and interval without changing the plan.

    Each converter first assigns its local DC PV to charging and can contribute
    only the remaining PV to its recorded AC export. The shared AC bus assigns
    that PV to the remaining battery import before using recorded grid import.
    Shared sources are allocated proportionally to the corresponding charging
    demand. Unassigned energy can come from another discharging battery.
    Incomplete archived evidence leaves that interval's attribution unavailable.
    """
    if not problem.get("batteries"):
        return []
    try:
        inverter_rows = {row["inverter_id"]: row for row in problem["inverters"]}
        topology = {identity: ([], []) for identity in inverter_rows}
        for battery in problem["batteries"]:
            topology[battery["inverter_id"]][0].append(
                (battery["device_id"], _efficiency(battery["dc_efficiency"]))
            )
        for source in problem["pv_sources"]:
            topology[source["inverter_id"]][1].append(source["source_id"])
    except (KeyError, TypeError, ValueError):
        return []

    result = []
    for flow in flows:
        try:
            result.append(_interval_sources(flow, inverter_rows, topology))
        except (KeyError, TypeError, ValueError):
            result.append({})
    return result


def _interval_sources(flow, inverter_rows, topology):
    """Allocate bounded source budgets once without counting AC PV at two converters."""
    batteries = {row["device_id"]: row["charge_kwh"] for row in flow["batteries"]}
    solar = {row["source_id"]: row["used_kwh"] for row in flow["solar"]}
    inverters = {row["inverter_id"]: row for row in flow["inverters"]}
    pending = []
    solar_ac, charging_ac = 0.0, 0.0
    for identity, (local_batteries, local_solar) in topology.items():
        efficiency = _efficiency(inverter_rows[identity]["efficiency"])
        inverter = inverters[identity]
        charging = [
            (device, _nonnegative(batteries[device]), dc_efficiency)
            for device, dc_efficiency in local_batteries
        ]
        demand_dc = sum(amount / dc_efficiency for _, amount, dc_efficiency in charging)
        solar_dc = sum(_nonnegative(solar[source]) for source in local_solar)
        direct = min(demand_dc, solar_dc)
        solar_ac += min(_nonnegative(inverter["export_kwh"]), (solar_dc - direct) * efficiency)
        if demand_dc:
            imported = min(_nonnegative(inverter["import_kwh"]), (demand_dc - direct) / efficiency)
            charging_ac += imported
            pending.append((charging, direct / demand_dc, imported * efficiency / demand_dc))

    pv_ac_share = min(1.0, solar_ac / charging_ac) if charging_ac else 0.0
    grid_share = (
        min(1.0 - pv_ac_share, _nonnegative(flow["grid_import"]) / charging_ac)
        if charging_ac
        else 0.0
    )
    result = {}
    for charging, direct_share, ac_share in pending:
        for device, amount, _ in charging:
            if not amount:
                continue
            pv_dc = amount * direct_share
            pv_ac = amount * ac_share * pv_ac_share
            grid = amount * ac_share * grid_share
            result[device] = {
                "pv_dc": pv_dc,
                "pv_ac": pv_ac,
                "grid": grid,
                "other": max(0.0, amount - pv_dc - pv_ac - grid),
            }
    return result
