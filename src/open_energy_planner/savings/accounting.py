"""Independent measured billing and chronological reference accounting. No optimization."""

from datetime import datetime
from itertools import pairwise
from math import fsum

from ..contracts.compilation import BonusCompiler
from ..contracts.export_meter import seed_reading
from .model import ENERGY_TOLERANCE_KWH, PendingSettlement


def inventory(problem, stored):
    """Use the same bounded valuation curves independently for both scenarios' stored energy."""
    if problem.batteries and not problem.terminal_valuations:
        raise ValueError("Battery valuation evidence is missing")
    return fsum(curve.value(stored[curve.device_id]) for curve in problem.terminal_valuations)


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
        rate = (
            chosen.extra_rate(market, start + (end - start) / 2, location=location)
            if chosen
            else 0.0
        )
        period = policy.period_start(start)
        counter_key = identity + "|" + period.isoformat()
        try:
            key = opening_counter_key(identity, period, start, counters, seeds, meter_start)
        except ValueError:
            if exported != 0 and rate != 0:
                raise
            # The payment is exact, but missing gross usage must remain unknown.
            counters.pop(counter_key, None)
            continue
        quota = min(exported, max(0, chosen.cap_kwh - counters[key])) if chosen else 0
        amount += quota * rate
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


def bill(
    contract, frame, imported, exported, counters, seeds, market_rows, location, *, uncapped=False
):
    """Split at price, contract, annual reset and daylight edges. Preserve every used rule/price version.

    Quarter totals imply a uniform rate inside the recorded interval. Sub-quarter
    splits are labelled estimated by the caller, never claimed as sub-minute measurements.
    """
    edges = {frame.start, frame.end}
    edges.update(
        BonusCompiler().boundaries(
            contract, frame.start, frame.end, location, include_resets=not uncapped
        )
    )
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
        if uncapped:
            rule = contract.bonus_at(left)
            if rule is not None:
                bonus += (
                    exported
                    * fraction
                    * rule.extra_rate(price["price"], left + (right - left) / 2, location=location)
                )
        else:
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


def measured_cash_flows(frame, bindings):
    """Reconstruct AC grid kWh without storage from the same measured home and solar.

    The frozen household terms already remove battery charging and add discharge.
    Subtracting measured AC solar gives the no-battery net grid exchange. Apply
    its change to recorded gross import/export, retaining both when the battery
    made no net contribution. No SoC, forecast, converter dispatch
    or battery efficiency is applied to these already AC-normalized meters.
    Curtailment and flexible-load timing remain as measured. This is a battery
    cash comparison, not a simulation of all behavior without energy management.
    """
    imported = fsum(frame.channels[entity] for entity in bindings["imports"])
    exported = fsum(frame.channels[entity] for entity in bindings["exports"])
    terms = bindings["terms"]
    if not terms:
        raise ValueError("Measured household balance is unavailable")
    if len({term["entity_id"] for term in terms}) != len(terms):
        raise ValueError("Duplicate measured energy bindings")
    if any(term["sign"] not in (-1, 1) for term in terms):
        raise ValueError("Invalid measured household sign")
    household = fsum(frame.channels[term["entity_id"]] * term["sign"] for term in terms)
    if household < -ENERGY_TOLERANCE_KWH:
        raise ValueError("Measured household balance is negative")
    if any(source.get("measurement_boundary", "ac") != "ac" for source in bindings["solar"]):
        raise PendingSettlement("Without-battery comparison requires measured AC solar energy")
    solar = fsum(frame.channels[source["entity_id"]] for source in bindings["solar"])
    difference = max(0.0, household) - solar - (imported - exported)
    if difference >= 0:
        without_import = imported + max(0.0, difference - exported)
        without_export = max(0.0, exported - difference)
    else:
        without_import = max(0.0, imported + difference)
        without_export = exported + max(0.0, -difference - imported)
    return {"actual": (imported, exported), "without_battery": (without_import, without_export)}
