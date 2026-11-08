"""Request annual sum references and retain separate physical settlement history."""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from open_energy_planner.contracts.bonus import CounterBook, CounterSeed
from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.contracts.export_meter import export_identity, export_sources
from open_energy_planner.planning_policy import PLANNING_HORIZON_HOURS


@dataclass(frozen=True)
class BaselineRequest:
    """Bind a bounded history read to the exact contract, sources and confirmed counters."""

    contract_version: str
    config_revision: str
    counter_version: str | None
    entity_id: str
    periods: tuple[tuple[str, datetime], ...]
    active_periods: tuple[tuple[str, datetime], ...]
    end: datetime
    entity_ids: tuple[str, ...] = ()
    statistics_sum: bool = False

    @property
    def version(self):
        """Detect delayed reports after configuration changes or a manual counter update."""
        return hashlib.sha256(json.dumps(self.document(), sort_keys=True).encode()).hexdigest()

    def document(self):
        """Serialize only normalized read instructions, without HA metadata or database details."""
        return {
            **({"entity_ids": list(self.entity_ids)} if len(self.entity_ids) > 1 else {}),
            "contract_version": self.contract_version,
            "config_revision": self.config_revision,
            "counter_version": self.counter_version,
            **({"basis": "statistics_sum"} if self.statistics_sum else {}),
            "entity_id": self.entity_id,
            "start": (min(start for _, start in self.periods) - timedelta(hours=1)).isoformat(),
            "end": self.end.isoformat(),
            "periods": [[identity, start.isoformat()] for identity, start in self.periods],
            "active_periods": [
                [identity, start.isoformat()] for identity, start in self.active_periods
            ],
        }


class BonusHistory:
    """Request missing references and refresh annual statistics checkpoints outside HA."""

    def request(self, contract, configuration, book, now):
        """Select the configured export source and allow one hour for HA statistics compilation."""
        if contract is None or configuration is None or not contract.bonuses:
            return None
        specs = export_sources(configuration)
        if not specs or any(not spec.required or spec.quantity != "energy" for spec in specs):
            return None
        entity_id = export_identity(spec.entity_id for spec in specs)
        horizon = now + timedelta(hours=PLANNING_HORIZON_HOURS)
        relevant = {
            rule.rule_id
            for instant in (
                now,
                *(
                    rule.effective_from
                    for rule in contract.bonuses
                    if now < rule.effective_from < horizon
                ),
            )
            if (rule := contract.bonus_at(instant)) is not None
        }
        active_periods = tuple(
            (identity, versions[0].period_start(now))
            for identity, versions in BonusCompiler().families(contract).items()
            if versions[0].effective_from <= now
            and (versions[0].contract_start is None or versions[0].contract_start <= now)
        )
        periods = tuple(
            (identity, start) for identity, start in active_periods if identity in relevant
        )
        if not periods:
            return None
        end = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        if book and all(
            any(
                seed.rule_id == identity
                and seed.period_start == start
                and seed.entity_id == entity_id
                and seed.statistics is not None
                and seed.statistics.latest_at >= end
                and not seed.statistics.issue
                for seed in book.seeds
            )
            for identity, start in periods
        ):
            return None
        if any(start > end or start.minute or start.second for _, start in periods):
            return None
        return BaselineRequest(
            contract.version,
            configuration.revision,
            book.version if book else None,
            entity_id,
            periods,
            active_periods,
            end,
            tuple(spec.entity_id for spec in specs),
            True,
        )

    def derive(self, request, history, book, now):
        """Retain relevant confirmed seeds. Prior periods and removed rules stay in immutable archives."""
        if history.entity_id != request.entity_id:
            raise ValueError("History source changed")
        components = {entity for entity, _value in history.component_meters}
        sources = set(request.entity_ids or (request.entity_id,))
        # The history identity proves a lone source. Aggregates need component evidence too.
        if (components or len(sources) > 1) and components != sources:
            raise ValueError("History components changed")
        active = set(request.active_periods)
        seeds = (
            {
                (seed.rule_id, seed.period_start): seed
                for seed in book.seeds
                if (seed.rule_id, seed.period_start) in active
                and seed.entity_id == request.entity_id
            }
            if book
            else {}
        )
        for identity, start in request.periods:
            used, meter = history.baseline(start, request.end)
            seeds[identity, start] = CounterSeed(
                identity,
                start,
                used,
                meter,
                history.entity_id,
                request.end,
                history.component_meters,
            )
        return CounterBook(now, tuple(seeds.values()))


BONUS_DRAFT_LIFETIME = timedelta(minutes=5)


class BonusBudgetPreview:
    """Own one bounded, expiring draft/history cache. Never publish rules or confirmed counters."""

    def __init__(self):
        """Start without a candidate, temporary baseline or history failure."""
        self.contract = None
        self.book = None
        self.seen_at = None
        self.issue = None

    def update(self, candidate, now):
        """Keep an unchanged draft request stable across UI polling. Discard only its former error on edits."""
        if self.seen_at is not None and now - self.seen_at > BONUS_DRAFT_LIFETIME:
            self.book = None
            self.issue = None
        if (
            self.contract is None
            or replace(candidate, recorded_at=self.contract.recorded_at) != self.contract
        ):
            self.contract = candidate
            self.issue = None
        self.seen_at = now

    def baselines(self, stored, now):
        """Merge only relevant current-period seeds. Persisted confirmations override temporary history."""
        if self.contract is None:
            return stored
        periods = {}
        for identity, rules in BonusCompiler().families(self.contract).items():
            try:
                periods[identity] = rules[0].period_start(now)
            except ValueError:
                # An invalid reset remains a per-rule budget issue. Other rows stay readable.
                continue
        seeds = {}
        books = [book for book in (self.book, stored) if book]
        for book in books:
            for seed in book.seeds:
                if periods.get(seed.rule_id) == seed.period_start:
                    seeds[seed.rule_id, seed.period_start] = seed
        return (
            CounterBook(max(book.recorded_at for book in books), tuple(seeds.values()))
            if seeds
            else None
        )

    def request(self, configuration, stored, now):
        """Reuse the normal all-export history request while the editor is active and evidence is missing."""
        if self.contract is None or now - self.seen_at > BONUS_DRAFT_LIFETIME or self.issue:
            return None
        try:
            return BonusHistory().request(
                self.contract, configuration, self.baselines(stored, now), now
            )
        except ValueError as error:
            self.issue = str(error)
            return None

    def receive(self, request, history, stored, now):
        """Derive a temporary baseline with the same completeness/component checks as saved rules."""
        from open_energy_planner.contracts.bonus_statistics import derive_statistics

        self.book = derive_statistics(request, history, self.baselines(stored, now), now)
        self.issue = None
