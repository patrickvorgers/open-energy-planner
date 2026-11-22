"""Attribute planned battery flows using the recorded DC and AC boundaries."""

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


def battery_flow_attribution(problem, flows):
    """Return attributed terminal kWh per battery and interval without changing the plan.

    Each converter first assigns its local DC PV to charging and can contribute
    only the remaining PV to its recorded AC export. On the AC bus, solar and
    then battery output supply site demand. Surplus AC solar charges storage
    before grid import. Surplus solar then claims grid export before battery
    output. Battery transfers and other unattributed flows retain a remainder.
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
            result.append(_interval_attribution(flow, inverter_rows, topology))
        except (KeyError, TypeError, ValueError):
            result.append({})
    return result


def _interval_attribution(flow, inverter_rows, topology):
    """Allocate each shared AC and local DC budget once at its physical boundary."""
    batteries = {row["device_id"]: row for row in flow["batteries"]}
    solar = {row["source_id"]: row["used_kwh"] for row in flow["solar"]}
    inverters = {row["inverter_id"]: row for row in flow["inverters"]}
    charging = {}
    discharging = {}
    solar_ac, imported_ac, exported_ac = 0.0, 0.0, 0.0
    for identity, (local_batteries, local_solar) in topology.items():
        efficiency = _efficiency(inverter_rows[identity]["efficiency"])
        inverter = inverters[identity]
        converter_import = _nonnegative(inverter["import_kwh"])
        converter_export = _nonnegative(inverter["export_kwh"])
        imported_ac += converter_import
        exported_ac += converter_export
        local_charging = [
            (device, _nonnegative(batteries[device]["charge_kwh"]), dc_efficiency)
            for device, dc_efficiency in local_batteries
        ]
        demand_dc = sum(amount / dc_efficiency for _, amount, dc_efficiency in local_charging)
        solar_dc = sum(_nonnegative(solar[source]) for source in local_solar)
        direct = min(demand_dc, solar_dc)
        solar_export = min(converter_export, (solar_dc - direct) * efficiency)
        solar_ac += solar_export
        if demand_dc:
            charge_ac = min(converter_import, (demand_dc - direct) / efficiency)
            for device, amount, dc_efficiency in local_charging:
                if amount:
                    charging[device] = (
                        amount,
                        amount * direct / demand_dc,
                        charge_ac * (amount / dc_efficiency) / demand_dc,
                        dc_efficiency * efficiency,
                    )
        local_discharging = [
            (device, _nonnegative(batteries[device]["discharge_kwh"]), dc_efficiency)
            for device, dc_efficiency in local_batteries
        ]
        available_ac = sum(
            amount * dc_efficiency * efficiency for _, amount, dc_efficiency in local_discharging
        )
        if available_ac:
            output_share = min(1.0, (converter_export - solar_export) / available_ac)
            for device, amount, dc_efficiency in local_discharging:
                if amount:
                    discharging[device] = (
                        amount,
                        amount * dc_efficiency * efficiency * output_share,
                        output_share,
                    )

    grid_import = _nonnegative(flow["grid_import"])
    grid_export = _nonnegative(flow["grid_export"])
    # The AC balance already includes appliances, EV and thermal work in aggregate.
    own_load = max(0.0, grid_import + exported_ac - grid_export - imported_ac)
    charging_ac = sum(amount_ac for _, _, amount_ac, _ in charging.values())
    discharge_ac = sum(amount_ac for _, amount_ac, _ in discharging.values())
    solar_surplus = max(0.0, solar_ac - own_load)
    solar_charge = min(charging_ac, solar_surplus)
    load_after_solar = max(0.0, own_load - solar_ac)
    battery_load = min(discharge_ac, load_after_solar)
    grid_charge = min(
        charging_ac - solar_charge, max(0.0, grid_import - (load_after_solar - battery_load))
    )
    battery_export = min(
        discharge_ac - battery_load, max(0.0, grid_export - (solar_surplus - solar_charge))
    )

    result = {}
    for device, (amount, direct, amount_ac, conversion) in charging.items():
        share = amount_ac / charging_ac if charging_ac else 0.0
        result[device] = {
            "charge": _with_remainder(
                amount,
                pv_dc=direct,
                pv_ac=solar_charge * share * conversion,
                grid_import=grid_charge * share * conversion,
            )
        }
    for device, (amount, _, output_share) in discharging.items():
        share = amount * output_share / discharge_ac if discharge_ac else 0.0
        result.setdefault(device, {})["discharge"] = _with_remainder(
            amount, own_use=battery_load * share, grid_export=battery_export * share
        )
    return result


def _with_remainder(amount, **routes):
    """Keep every attributed series in terminal kWh and preserve unassigned energy."""
    return {**routes, "other": max(0.0, amount - sum(routes.values()))}
