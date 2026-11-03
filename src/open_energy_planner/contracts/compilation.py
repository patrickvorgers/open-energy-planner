"""Project dated bonuses and confirmed measured baselines onto plain billing segments."""

from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise

from ..optimization.export_bonus import ExportBonus
from .export_meter import export_identity, export_sources
from .model import TariffEvidence


@dataclass(frozen=True)
class PriceInput:
    """One current tariff segment and potential extra export rate before any volume cap.

    Missing window/location evidence leaves the bonus unknown without hiding the
    known base tariff. This is a read-only rate preview, never a planning input.
    """

    tariff: TariffEvidence
    bonus_rate: float | None
    issue: str | None


@dataclass(frozen=True)
class BonusEvidence:
    """Retain starting volume and the sum or physical endpoint basis used by this candidate."""

    rule_id: str
    counter_version: str
    period_start: datetime
    initial_kwh: float
    seed_used_kwh: float
    seed_meter_kwh: float
    meter_kwh: float
    meter_observed_at: datetime
    entity_id: str


class BonusCompiler:
    """Compile only arithmetic/time/counter semantics. No provider, transport or native solver."""

    def families(self, contract):
        """Group dated revisions by stable identity in deterministic order."""
        result = {}
        for rule in contract.bonuses:
            result.setdefault(rule.rule_id, []).append(rule)
        return {
            identity: tuple(sorted(rules, key=lambda item: item.effective_from))
            for identity, rules in sorted(result.items())
        }

    def boundaries(self, contract, start, end, location):
        """Collect rule/window/year edges before projecting the physical energy forecasts."""
        return {
            edge
            for rule in contract.bonuses
            if rule.effective_from < end
            for edge in rule.boundaries(start, end, location=location)
        }

    def preview(self, contract, prices, *, location=None):
        """Project saved bonus formulas/eligibility onto tariffs without plans or counters.

        Split exactly at rule and paid-window edges. Unknown bonus eligibility is
        represented by None. Never substitute zero or consume an annual budget.
        """
        if not prices:
            return ()
        edges, unknown_windows = set(), set()
        for rule in contract.bonuses:
            if rule.effective_from >= prices[-1].end:
                continue
            edges.add(rule.effective_from)
            try:
                edges.update(rule.boundaries(prices[0].start, prices[-1].end, location=location))
            except (ValueError, TypeError, KeyError):
                unknown_windows.add(rule.effective_from)
        result = []
        for price in prices:
            boundaries = sorted(
                {
                    price.start,
                    price.end,
                    *(edge for edge in edges if price.start < edge < price.end),
                }
            )
            for start, end in pairwise(boundaries):
                middle = start + (end - start) / 2
                chosen = contract.bonus_at(middle)
                rate, issue = 0.0, None
                if chosen:
                    try:
                        rate = chosen.extra_rate(price.market_price, middle, location=location)
                    except (ValueError, TypeError, KeyError, OverflowError):
                        rate = None
                        issue = (
                            "bonus_window_unavailable"
                            if chosen.effective_from in unknown_windows
                            else "bonus_rate_unavailable"
                        )
                result.append(PriceInput(replace(price, start=start, end=end), rate, issue))
        return tuple(result)

    def current_evidence(self, contract, now, book, configuration, report, *, identities=None):
        """Validate actual counter continuity once for planning and the read-only budget view."""
        families = {
            identity: versions
            for identity, versions in self.families(contract).items()
            if (identities is None or identity in identities)
            and (versions[0].contract_start is None or versions[0].contract_start <= now)
        }
        if not families:
            return ()
        if book is None or book.recorded_at > now:
            raise ValueError("bonus_counter_baseline_required")
        specs = export_sources(configuration)
        if not specs or any(not spec.required or spec.quantity != "energy" for spec in specs):
            raise ValueError("bonus_export_measurement_required")
        source_identity = export_identity(spec.entity_id for spec in specs)
        seeds = {(seed.rule_id, seed.period_start): seed for seed in book.seeds}
        evidence = []
        for identity, versions in families.items():
            policy = versions[0]
            period_start = policy.period_start(now)
            seed = seeds.get((identity, period_start))
            if seed is None or seed.entity_id != source_identity or seed.statistics is None:
                raise ValueError("bonus_counter_baseline_required")
            basis = seed.statistics
            evidence.append(
                BonusEvidence(
                    identity,
                    book.version,
                    period_start,
                    basis.used(now),
                    basis.prefix_kwh or 0,
                    sum(item.reference_sum for item in basis.series),
                    sum(item.latest_sum for item in basis.series),
                    basis.latest_at,
                    seed.entity_id,
                )
            )
        return tuple(evidence)

    def compile(self, contract, intervals, prices, now, book, configuration, report):
        """Use all-grid-export annual counters and per-segment extra rates without mutating actual state."""
        if not contract.bonuses:
            return (), ()
        identities = {
            chosen.rule_id
            for price in prices
            if (chosen := contract.bonus_at(price.start + (price.end - price.start) / 2))
            is not None
        }
        evidence = self.current_evidence(
            contract, now, book, configuration, report, identities=identities
        )
        current = {item.rule_id: item for item in evidence}
        compiled = []
        for identity, versions in self.families(contract).items():
            if identity not in identities:
                continue
            policy = versions[0]
            # A future supplier contract has not accumulated exports. Existing periods still require evidence.
            initial = current[identity].initial_kwh if identity in current else 0.0
            rates, caps, periods = [], [], []
            for step, price in zip(intervals, prices, strict=True):
                middle = price.start + (price.end - price.start) / 2
                chosen = contract.bonus_at(middle)
                if chosen is not None and chosen.rule_id != identity:
                    chosen = None
                rates.append(
                    chosen.extra_rate(
                        price.market_price, middle, location=report.location if report else None
                    )
                    if chosen
                    else 0
                )
                caps.append(chosen.cap_kwh if chosen else 0)
                periods.append(policy.period_start(middle).isoformat())
            compiled.append(
                ExportBonus(identity, tuple(rates), tuple(caps), tuple(periods), initial)
            )
        return tuple(compiled), tuple(evidence)
