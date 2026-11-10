"""Independent measured billing and chronological reference accounting. No optimization."""

from dataclasses import replace
from datetime import datetime
from itertools import pairwise
from math import fsum

from ..contracts.compilation import BonusCompiler
from ..contracts.export_meter import seed_reading
from .model import ENERGY_TOLERANCE_KWH, POLICY_VERSION, PendingSettlement
from .physical import advance, measured_step, transition_scenario
from .reference import dispatch


def inventory(problem, stored):
    """Use the same bounded valuation curves independently for both scenarios' stored energy."""
    if problem.batteries and not problem.terminal_valuations:
        raise ValueError("Battery valuation evidence is missing")
    return fsum(curve.value(stored[curve.device_id]) for curve in problem.terminal_valuations)


def inventory_curves(problem):
    """Freeze reserve kWh, useful kWh and currency/kWh rates needed for a later hardware removal."""
    return {
        curve.device_id: {
            "reserve": curve.reserve_kwh,
            "usable": curve.usable_kwh,
            "rate": curve.rate,
        }
        for curve in problem.terminal_valuations
    }


def value_movements(problem, previous, movements):
    """Return valued copies of physical installation changes, separate from operational costs."""
    movements = [dict(movement) for movement in movements]
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
        policy = versions[0]
        if start < policy.effective_from:
            continue
        if policy.contract_start is not None and start < policy.contract_start:
            continue
        key = opening_counter_key(
            identity, policy.period_start(start), start, counters, seeds, meter_start
        )
        quota = min(exported, max(0, chosen.cap_kwh - counters[key])) if chosen else 0
        amount += (
            quota * chosen.extra_rate(market, start + (end - start) / 2, location=location)
            if chosen
            else 0
        )
        counters[key] += exported
    return amount


def opening_counter_key(identity, period, start, counters, seeds, meter_start):
    """Initialize established usage once. Unknown mid-period usage is never silently zero."""
    key = identity + "|" + period.isoformat()
    if key not in counters:
        seed = next(
            (item for item in seeds if item.rule_id == identity and item.period_start == period),
            None,
        )
        if seed is None:
            if period != start:
                raise ValueError("Bonus comparison baseline is missing")
            counters[key] = 0.0
        else:
            if seed.observed_at > start:
                raise ValueError("Bonus seed cannot establish initial comparison budget")
            counters[key] = seed.used_kwh + seed_reading(seed, meter_start) - seed.meter_kwh
            if counters[key] < -ENERGY_TOLERANCE_KWH:
                raise ValueError("Export meter reset invalidates bonus seed")
    return key


def advance_counters(contract, frame, exported, previous, seeds, location):
    """Advance gross-export usage without prices or payment formulas when metering is known.

    The caller must first establish simulated opening usage. Exact annual and
    activation boundaries use the same uniform within-quarter split as billing.
    This advances a detached copy, so a failed attempt cannot partly consume quota.
    Exported energy and every counter value are gross kWh, independent of rates.
    """
    counters = dict(previous) if previous is not None else {}
    edges = {
        frame.start,
        frame.end,
        *BonusCompiler().boundaries(contract, frame.start, frame.end, location),
    }
    families = BonusCompiler().families(contract)
    for left, right in pairwise(sorted(edge for edge in edges if frame.start <= edge <= frame.end)):
        fraction = (right - left) / (frame.end - frame.start)
        for identity, versions in families.items():
            policy = versions[0]
            if left < policy.effective_from or (
                policy.contract_start is not None and left < policy.contract_start
            ):
                continue
            key = opening_counter_key(
                identity, policy.period_start(left), left, counters, seeds, frame.meter_start
            )
            counters[key] += exported * fraction
    return counters


def without_battery_flows(problem, frame, bindings):
    """Use the shared measured-load/available-PV boundary with the original converters and no batteries."""
    step, estimated = measured_step(problem, frame, bindings)
    return dispatch(replace(problem, batteries=(), terminal_valuations=()), step, {}), estimated


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


def evaluate(
    problem, frame, bindings, previous, contract, seeds, market_rows, location, *, physical=None
):
    """Advance both scenarios exactly once from stored continuation. Date filters never reach this function."""
    physical = advance(problem, frame, bindings, previous) if physical is None else physical
    channels = frame.channels
    load = physical["load_kwh"]
    estimated_pv = physical["estimated_pv"]
    reference_stored = physical["reference_start"]
    reference = physical["reference"]
    movements, external_value = value_movements(problem, previous, physical["movements"])
    actual_import, actual_export, actual_wear = actual_flows(problem, frame, bindings)
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
    state["curves"] = inventory_curves(problem)
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


def actual_flows(problem, frame, bindings):
    """Read actual grid kWh and measured battery wear without requiring a simulated PV or SoC history."""
    identities = {unit.device_id for unit in problem.batteries}
    if set(frame.battery_start) != identities or set(frame.battery_end) != identities:
        raise ValueError("Battery endpoint coverage is missing")
    inverters = {inverter.inverter_id: inverter for inverter in problem.inverters}
    imported = fsum(frame.channels[entity] for entity in bindings["imports"])
    exported = fsum(frame.channels[entity] for entity in bindings["exports"])
    wear = 0.0
    for unit in problem.batteries:
        if (
            max(frame.battery_start[unit.device_id], frame.battery_end[unit.device_id])
            > unit.battery.capacity_kwh + ENERGY_TOLERANCE_KWH
        ):
            raise ValueError("Measured battery energy exceeds capacity")
        binding = next(
            (item for item in bindings["batteries"] if item["device_id"] == unit.device_id),
            None,
        )
        if binding is None:
            raise ValueError("Settlement battery binding is missing for a planned battery")
        discharged = frame.channels[binding["energy_entity"]]
        wear += (
            discharged
            / (
                unit.battery.discharge_efficiency
                * unit.dc_efficiency
                * inverters[unit.inverter_id].efficiency
            )
            * unit.battery.cycle_fee
            / unit.battery.capacity_kwh
        )
    return imported, exported, wear


def independent_account(
    problem,
    frame,
    bindings,
    kind,
    previous,
    contract,
    seeds,
    prices,
    location,
    *,
    common_start=False,
    inventory_previous=None,
):
    """Bill one independently recoverable scenario after a gap without inventing reference stock.

    Actual inventory endpoints come from measurements. Without-battery flows
    use the same measured demand and available-PV policy as self-consumption.
    Unknown simulated bonus counters cannot use an actual export seed.
    An actual ``inventory_previous`` checkpoint contains end, actual_stored,
    capacities, curves and scalar inventory value. Contiguous hardware movements
    are excluded from operating cost even when reference stock is unavailable.
    """
    movements = []
    if kind == "actual":
        imported, exported, wear = actual_flows(problem, frame, bindings)
        opening = inventory(problem, frame.battery_start)
        closing = inventory(problem, frame.battery_end)
        stored = frame.battery_end
        estimated = False
        if inventory_previous and inventory_previous["end"] == frame.start.isoformat():
            _stored, changes = transition_scenario(problem, frame, inventory_previous, "actual")
            movements, external = value_movements(problem, inventory_previous, changes)
            opening = inventory_previous["inventory"] + external["actual"]
    elif kind == "without_battery":
        flows, estimated = without_battery_flows(problem, frame, bindings)
        imported, exported, wear = flows["import_kwh"], flows["export_kwh"], 0.0
        opening = closing = 0.0
        stored = {}
    else:
        raise ValueError("Unsupported independent savings scenario")
    counters = dict(previous) if previous is not None else {}
    if kind != "actual" and not simulated_counters_ready(
        contract, frame.start, previous, common_start=common_start
    ):
        raise PendingSettlement("Simulated bonus counters are unknown after missing measurements")
    result = bill(contract, frame, imported, exported, counters, seeds, prices, location)
    result.update(
        wear=wear,
        inventory_start=opening,
        inventory_end=closing,
        inventory_change=closing - opening,
        economic_cost=result["grid_cash"] + wear - (closing - opening),
        import_kwh=imported,
        export_kwh=exported,
        stored=stored,
        available_pv_estimated=estimated,
        inventory_movements=movements,
    )
    return result, counters


def simulated_counters_ready(contract, start, counters, *, common_start=False):
    """Admit known usage, a true reset or the original common start.

    Counter keys combine rule ID and original period start. Values are gross kWh.
    ``common_start`` is valid only before the scenarios first diverge. It never
    means the beginning of a selected date range or the provisional recovery floor.
    """
    for versions in BonusCompiler().families(contract).values():
        policy = versions[0]
        if start < policy.effective_from or (
            policy.contract_start is not None and start < policy.contract_start
        ):
            continue
        period = policy.period_start(start)
        if not (
            common_start
            or period == start
            or counters is not None
            and policy.rule_id + "|" + period.isoformat() in counters
        ):
            return False
    return True
