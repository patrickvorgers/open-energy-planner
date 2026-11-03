"""Compose cached market data and contract rules into the existing planning-basis port."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Protocol

from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.contracts.model import ContractSchedule, TariffCalculator
from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.home_assistant.observations import (
    BasisSource,
    InputsUnavailable,
    PlanningBasis,
    SourceConfiguration,
)
from open_energy_planner.installation.valuation import derive
from open_energy_planner.planning_policy import PLANNING_HORIZON_HOURS, TERMINAL_VALUATION_HOURS
from open_energy_planner.prices.model import PriceSeries, PriceTimeline

MAX_BILLING_SEGMENTS = 1024


class CachedPrices(Protocol):
    """Expose only typed accepted prices and their stable semantic version. Never HTTP payloads."""

    series: tuple[PriceSeries, ...]

    def timeline(
        self, now: datetime, *, hours: float, market_area: str, currency: str
    ) -> PriceTimeline:
        """Return composed prices. Source preference and fallback belong to the price adapter."""
        ...

    @property
    def version(self) -> str:
        """Identify values/coverage/provenance without rolling-clock or receipt-only churn."""
        ...


class ContractBasisSource:
    """Replace future-source tariff placeholders with effective rates and exact evidence.

    Forecast quantities and installation limits remain owned by the upstream
    basis source. Billing edges refine forecast average rates while original
    control groups retain fixed terminal battery power across their segments.
    No source, rule, counter baseline or location is fabricated.
    """

    def __init__(
        self,
        future: BasisSource | None,
        prices: Callable[[], CachedPrices | None],
        contract: Callable[[], ContractSchedule | None],
        counters=lambda: None,
    ):
        """Borrow nonblocking current-state views without owning adapters or persistence."""
        self.future, self.prices, self.contract = future, prices, contract
        self.counters = counters

    def current(
        self, configuration: SourceConfiguration, now: datetime, *, report=None
    ) -> PlanningBasis:
        """Apply available dated rules, shorten missing-price tails and bound source validity."""
        contract, prices = self.contract(), self.prices()
        if contract is None:
            raise InputsUnavailable("contract_not_configured")
        if prices is None or not prices.series:
            raise InputsUnavailable("market_prices_unavailable")
        if contract.recorded_at > now:
            raise InputsUnavailable("contract_revision_from_future")
        future = self.future.current(configuration, now, report=report) if self.future else None
        if future is None:
            raise InputsUnavailable("forecasts_not_configured")
        if future.config_revision != configuration.revision or future.valid_until <= now:
            raise InputsUnavailable("forecast_or_prices_invalid")
        problem = future.problem
        if problem.export_bonuses:
            raise InputsUnavailable("unsupported_contract_bonus")
        end = problem.intervals[-1].start + timedelta(hours=problem.intervals[-1].duration_h)
        if not now < end <= now + timedelta(hours=PLANNING_HORIZON_HOURS):
            raise InputsUnavailable("unsupported_forecast_horizon")
        try:
            timeline = prices.timeline(
                now,
                market_area=contract.market_area,
                currency=contract.currency,
                hours=(end - now).total_seconds() / SECONDS_PER_HOUR,
            )
            evidence = TariffCalculator().calculate(timeline, contract)
        except (ValueError, TypeError, OverflowError) as error:
            raise InputsUnavailable("contract_or_market_prices_invalid") from error
        location = report.location if report else None
        compiler = BonusCompiler()
        try:
            edges = (
                compiler.boundaries(contract, now, timeline.covered_until, location)
                if evidence
                else set()
            )
        except (ValueError, TypeError, KeyError) as error:
            raise InputsUnavailable("bonus_window_unavailable") from error
        edges.update(item.start for item in evidence)
        edges.update(item.end for item in evidence)
        intervals, used = [], []
        for step in problem.intervals:
            left = max(now, step.start)
            right = min(timeline.covered_until, step.start + timedelta(hours=step.duration_h))
            if left >= right:
                continue
            boundaries = sorted({left, right, *(edge for edge in edges if left < edge < right)})
            group = step.control_group or f"{step.start.isoformat()}:{step.duration_h}"
            for begin, until in pairwise(boundaries):
                price = next(
                    (item for item in evidence if item.start <= begin and item.end >= until), None
                )
                if price is None:
                    raise InputsUnavailable("tariff_coverage_gap")
                duration = (until - begin).total_seconds() / SECONDS_PER_HOUR
                intervals.append(
                    replace(
                        step,
                        start=begin,
                        duration_h=duration,
                        buy_price=price.buy_price,
                        sell_price=price.sell_price,
                        load_kwh=step.load_kwh * duration / step.duration_h,
                        pv_kwh=step.pv_kwh * duration / step.duration_h,
                        pv_sources=tuple(
                            (key, value * duration / step.duration_h)
                            for key, value in step.pv_sources
                        ),
                        control_group=group,
                    )
                )
                used.append(replace(price, start=begin, end=until))
                if len(intervals) > MAX_BILLING_SEGMENTS:
                    raise InputsUnavailable("too_many_billing_segments")
        if not intervals or intervals[0].start != now:
            raise InputsUnavailable("tariff_coverage_gap")
        book = self.counters()
        try:
            bonus_rules, bonus_evidence = compiler.compile(
                contract, intervals, used, now, book, configuration, report
            )
        except (ValueError, TypeError, KeyError) as error:
            reason = str(error)
            raise InputsUnavailable(
                reason if reason.startswith("bonus_") else "bonus_rules_invalid"
            ) from error
        priced = replace(problem, intervals=tuple(intervals), export_bonuses=bonus_rules)
        terminal_tariffs = []
        if priced.batteries:
            planning_end = intervals[-1].start + timedelta(hours=intervals[-1].duration_h)
            extended = prices.timeline(
                now,
                market_area=contract.market_area,
                currency=contract.currency,
                hours=(planning_end - now).total_seconds() / SECONDS_PER_HOUR
                + TERMINAL_VALUATION_HOURS,
            )
            for entry in extended.entries:
                if entry.end <= planning_end:
                    continue
                entry = replace(entry, start=max(entry.start, planning_end))
                try:
                    additional = TariffCalculator().calculate(
                        PriceTimeline(
                            entry.start,
                            entry.end,
                            contract.market_area,
                            contract.currency,
                            (entry,),
                        ),
                        contract,
                    )
                except (ValueError, TypeError, OverflowError):
                    # Optional future valuation must not make covered commands
                    # unusable when later contract periods are not configured.
                    break
                terminal_tariffs.extend(additional)
            priced = replace(priced, terminal_valuations=derive(priced, tuple(terminal_tariffs)))
        used_versions = {item.source_version for item in (*used, *terminal_tariffs)}
        # Coverage and provider expiry bound commands. The next planning tick is not data expiry.
        deadlines = [future.valid_until]
        deadlines.extend(
            source.expires_at
            for source in prices.series
            if source.version in used_versions and source.expires_at is not None
        )
        # Issue-time precedence can change selected rates without changing the
        # source cache's semantic version. Retain that selection, but exclude
        # receipt-only hashes and moving boundaries between equivalent tariffs.
        # Lead buckets and control steps may split the same rate as time advances.
        selected = []
        for item in (*used, *terminal_tariffs):
            identity = (
                item.market_price,
                item.buy_price,
                item.sell_price,
                item.price_kind,
                item.native_resolution_minutes,
                item.source_id,
                item.period_index,
            )
            segment = (item.end.isoformat(), *identity)
            if selected and selected[-1][1:] == identity:
                selected[-1] = segment
            else:
                selected.append(segment)
        version = hashlib.sha256(
            json.dumps(
                (
                    prices.version,
                    contract.version,
                    book.version if bonus_rules else None,
                    str(location),
                    selected,
                ),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return replace(
            future,
            price_version=version,
            valid_until=min(deadlines),
            problem=priced,
            contract_version=contract.version,
            tariff_evidence=tuple(used),
            bonus_evidence=bonus_evidence,
        )
