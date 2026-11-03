"""Independent measured billing and chronological reference accounting. No optimization."""

from datetime import datetime
from itertools import pairwise
from math import fsum

from ..contracts.compilation import BonusCompiler
from ..contracts.export_meter import seed_reading
from .model import ENERGY_TOLERANCE_KWH, POLICY_VERSION, PendingSettlement
from .physical import advance


def inventory(problem, stored):
    """Use the same bounded valuation curves independently for both scenarios' stored energy."""
    if problem.batteries and not problem.terminal_valuations:
        raise ValueError("Battery valuation evidence is missing")
    return fsum(curve.value(stored[curve.device_id]) for curve in problem.terminal_valuations)


def value_movements(problem, previous, movements):
    """Value physical installation changes separately from either scenario's operational costs."""
    curves = {curve.device_id: curve for curve in problem.terminal_valuations}
    external = {"actual": 0.0, "reference": 0.0}
    for movement in movements:
        identity = movement["device_id"]
        before, after = movement.pop("before"), movement.pop("after")
        if movement["kwh"] > 0:
            amount = curves[identity].value(after) - curves[identity].value(before)
        else:
            curve = previous["curves"][identity]
            amount = (
                min(max(0, after - curve["reserve"]), curve["usable"])
                - min(max(0, before - curve["reserve"]), curve["usable"])
            ) * curve["rate"]
        movement["value"] = amount
        external[movement["scenario"]] += amount
    return movements, external


def bonus_payment(contract, start, end, market, exported, counters, seeds, meter_start, location):
    """Pay dated bonuses over exact rule/window edges, advancing all-export counters even at zero rate."""
    amount = 0.0
    families = BonusCompiler().families(contract)
    for identity, versions in families.items():
        chosen = contract.bonus_at(start)
        if chosen is not None and chosen.rule_id != identity:
            chosen = None
        policy = chosen or versions[0]
        if policy.contract_start is not None and start < policy.contract_start:
            continue
        period = policy.period_start(start).isoformat()
        key = identity + "|" + period
        if key not in counters:
            seed = next(
                (
                    seed_item
                    for seed_item in seeds
                    if seed_item.rule_id == identity
                    and seed_item.period_start.isoformat() == period
                ),
                None,
            )
            if seed is None:
                if policy.period_start(start) != start:
                    raise ValueError("Bonus comparison baseline is missing")
                counters[key] = 0.0
            else:
                if seed.observed_at > start:
                    raise ValueError("Bonus seed cannot establish initial comparison budget")
                counters[key] = seed.used_kwh + seed_reading(seed, meter_start) - seed.meter_kwh
                if counters[key] < -ENERGY_TOLERANCE_KWH:
                    raise ValueError("Export meter reset invalidates bonus seed")
        quota = min(exported, max(0, chosen.cap_kwh - counters[key])) if chosen else 0
        amount += (
            quota * chosen.extra_rate(market, start + (end - start) / 2, location=location)
            if chosen
            else 0
        )
        counters[key] += exported
    return amount


def bill(contract, frame, imported, exported, counters, seeds, market_rows, location):
    """Split at price, contract, annual reset and daylight edges. Preserve every used rule/price version.

    Quarter totals imply a uniform rate inside the recorded interval. Sub-quarter
    splits are labelled estimated by the caller, never claimed as sub-minute measurements.
    """
    edges = {frame.start, frame.end}
    edges.update(BonusCompiler().boundaries(contract, frame.start, frame.end, location))
    for row in market_rows:
        edges.update(
            (
                max(frame.start, datetime.fromisoformat(row["start"])),
                min(frame.end, datetime.fromisoformat(row["end"])),
            )
        )
    for period in contract.periods:
        if frame.start < period.effective_from < frame.end:
            edges.add(period.effective_from)
        if period.effective_until and frame.start < period.effective_until < frame.end:
            edges.add(period.effective_until)
    purchase = sale = bonus = 0.0
    evidence = []
    ordered = sorted(edge for edge in edges if frame.start <= edge <= frame.end)
    for left, right in pairwise(ordered):
        price = max(
            (
                row_item
                for row_item in market_rows
                if datetime.fromisoformat(row_item["start"]) <= left
                and datetime.fromisoformat(row_item["end"]) >= right
            ),
            key=lambda price_record: price_record.get("received_at", ""),
            default=None,
        )
        period = next(
            (
                period_item
                for period_item in contract.periods
                if period_item.effective_from <= left
                and (period_item.effective_until is None or period_item.effective_until >= right)
            ),
            None,
        )
        if price is None:
            raise PendingSettlement("Published price coverage is missing")
        if period is None:
            raise ValueError("Contract coverage is missing")
        fraction = (right - left) / (frame.end - frame.start)
        buy, sell = (
            period.import_formula.evaluate(price["price"]),
            period.export_formula.evaluate(price["price"]),
        )
        purchase += imported * fraction * buy
        sale += exported * fraction * sell
        bonus += bonus_payment(
            contract,
            left,
            right,
            price["price"],
            exported * fraction,
            counters,
            seeds,
            frame.meter_start,
            location,
        )
        evidence.append(
            {
                "start": left.isoformat(),
                "end": right.isoformat(),
                "market_price": price["price"],
                "buy_price": buy,
                "sell_price": sell,
                "source_version": price["version"],
                "contract_version": contract.version,
            }
        )
    return {
        "purchase": purchase,
        "sale": sale,
        "bonus": bonus,
        "grid_cash": purchase - sale - bonus,
        "tariffs": evidence,
    }


def evaluate(problem, frame, bindings, previous, contract, seeds, market_rows, location):
    """Advance both scenarios exactly once from stored continuation. Date filters never reach this function."""
    physical = advance(problem, frame, bindings, previous)
    channels = frame.channels
    load = physical["load_kwh"]
    estimated_pv = physical["estimated_pv"]
    reference_stored = physical["reference_start"]
    reference = physical["reference"]
    movements, external_value = value_movements(problem, previous, physical["movements"])
    inverters = {inverter.inverter_id: inverter for inverter in problem.inverters}
    actual_import = fsum(channels[entity] for entity in bindings["imports"])
    actual_export = fsum(channels[entity] for entity in bindings["exports"])
    actual_wear = 0.0
    for unit in problem.batteries:
        binding = next(
            (
                battery_binding
                for battery_binding in bindings["batteries"]
                if battery_binding["device_id"] == unit.device_id
            ),
            None,
        )
        if binding is None:
            raise ValueError("Settlement battery binding is missing for a planned battery")
        discharged = channels[binding["energy_entity"]]
        inv = inverters[unit.inverter_id]
        actual_wear += (
            discharged
            / (unit.battery.discharge_efficiency * unit.dc_efficiency * inv.efficiency)
            * unit.battery.cycle_fee
            / unit.battery.capacity_kwh
        )
    counters = {
        kind: dict(previous["counters"][kind]) if previous else {}
        for kind in ("actual", "reference")
    }
    actual = bill(
        contract,
        frame,
        actual_import,
        actual_export,
        counters["actual"],
        seeds,
        market_rows,
        location,
    )
    simulated = bill(
        contract,
        frame,
        reference["import_kwh"],
        reference["export_kwh"],
        counters["reference"],
        seeds,
        market_rows,
        location,
    )
    state = {
        "end": frame.end.isoformat(),
        "reference_stored": reference["stored"],
        "counters": counters,
        "actual_stored": frame.battery_end,
    }
    for kind, result, stored_start, stored_end, wear in (
        ("actual", actual, frame.battery_start, frame.battery_end, actual_wear),
        ("reference", simulated, reference_stored, reference["stored"], reference["wear"]),
    ):
        start_value = previous["inventory"][kind] if previous else inventory(problem, stored_start)
        start_value += external_value[kind]
        end_value = inventory(problem, stored_end)
        result.update(
            wear=wear,
            inventory_start=start_value,
            inventory_end=end_value,
            inventory_change=end_value - start_value,
            economic_cost=result["grid_cash"] + wear - (end_value - start_value),
        )
        state.setdefault("inventory", {})[kind] = end_value
    state["capacities"] = {
        battery.device_id: battery.battery.capacity_kwh for battery in problem.batteries
    }
    state["curves"] = {
        terminal_valuation.device_id: {
            "reserve": terminal_valuation.reserve_kwh,
            "usable": terminal_valuation.usable_kwh,
            "rate": terminal_valuation.rate,
        }
        for terminal_valuation in problem.terminal_valuations
    }
    actual.update(import_kwh=actual_import, export_kwh=actual_export, stored=frame.battery_end)
    simulated.update(
        import_kwh=reference["import_kwh"],
        export_kwh=reference["export_kwh"],
        stored=reference["stored"],
    )
    return {
        "start": frame.start.isoformat(),
        "end": frame.end.isoformat(),
        "policy": POLICY_VERSION,
        "actual": actual,
        "reference": simulated,
        "savings": simulated["economic_cost"] - actual["economic_cost"],
        "avoided_import_kwh": simulated["import_kwh"] - actual_import,
        "quality": frame.quality,
        "available_pv_estimated": estimated_pv,
        "subinterval_estimated": len(actual["tariffs"]) > 1,
        "inventory_movements": movements,
        "load_kwh": max(0, load),
        "measured_pv_kwh": sum(
            channels[solar_flow["entity_id"]] for solar_flow in bindings["solar"]
        ),
    }, state
