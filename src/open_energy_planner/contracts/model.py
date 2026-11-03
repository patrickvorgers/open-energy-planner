"""Immutable effective-period rules and source-linked tariff calculations."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import cached_property

from open_energy_planner.contracts.calculation.formula import Formula

from ..prices.model import PriceTimeline, aware
from .bonus import BonusRule

MAX_BONUS_FAMILIES = 8
MAX_CONTRACT_ITEMS = 64
MAX_CONTRACT_IDENTIFIER_LENGTH = 128


@dataclass(frozen=True)
class ContractPeriod:
    """Half-open UTC rule period. Import is cost, export is revenue, both per kWh."""

    effective_from: datetime
    effective_until: datetime | None
    import_formula: Formula
    export_formula: Formula
    contract_start: datetime | None = None

    def __post_init__(self):
        """Canonicalize absolute times and reject empty periods or unvalidated formulas."""
        object.__setattr__(self, "effective_from", aware(self.effective_from))
        if self.contract_start is not None:
            object.__setattr__(self, "contract_start", aware(self.contract_start))
            if self.contract_start > self.effective_from:
                raise ValueError("The contract start cannot be later than its price rule")
        if self.effective_until is not None:
            object.__setattr__(self, "effective_until", aware(self.effective_until))
            if self.effective_until <= self.effective_from:
                raise ValueError("Contract period must be positive")
        if not all(
            isinstance(formula, Formula) for formula in (self.import_formula, self.export_formula)
        ):
            raise TypeError("Contract periods require validated formulas")


@dataclass(frozen=True)
class ContractSchedule:
    """One immutable revision, containing dated rules for one area and currency.

    Replacements can correct past periods. Version identity includes the recorded
    instant and original formula text. Old revisions remain independently readable.
    Rules provide continuous coverage from their first start, with an open-ended last rule.
    """

    contract_id: str
    market_area: str
    currency: str
    recorded_at: datetime
    periods: tuple[ContractPeriod, ...]
    bonuses: tuple[BonusRule, ...] = ()

    def __post_init__(self):
        """Require bounded, ordered continuous price rules with no independently configured expiry."""
        object.__setattr__(self, "recorded_at", aware(self.recorded_at))
        if not all(
            isinstance(value, str) and 1 <= len(value.strip()) <= MAX_CONTRACT_IDENTIFIER_LENGTH
            for value in (self.contract_id, self.market_area, self.currency)
        ):
            raise ValueError("Explicit bounded contract identity is required")
        if not isinstance(self.periods, tuple) or not 1 <= len(self.periods) <= MAX_CONTRACT_ITEMS:
            raise ValueError("Contract requires 1-64 immutable periods")
        if not all(isinstance(period, ContractPeriod) for period in self.periods):
            raise TypeError("Expected typed contract periods")
        for left, right in zip(self.periods, self.periods[1:]):
            if (
                left.effective_from >= right.effective_from
                or left.effective_until != right.effective_from
            ):
                raise ValueError(
                    "Price rules require increasing unique starts and continuous coverage"
                )
        if self.periods[-1].effective_until is not None:
            raise ValueError("The last price rule must remain active until replaced")
        if (
            not isinstance(self.bonuses, tuple)
            or len(self.bonuses) > MAX_CONTRACT_ITEMS
            or any(not isinstance(rule, BonusRule) for rule in self.bonuses)
        ):
            raise ValueError("Expected at most 64 immutable bonus versions")
        if len({rule.effective_from for rule in self.bonuses}) != len(self.bonuses):
            raise ValueError(
                "Bonus rules require unique effective dates. Only one rule can be active"
            )
        families = {}
        for rule in self.bonuses:
            families.setdefault(rule.rule_id, []).append(rule)
        if len(families) > MAX_BONUS_FAMILIES:
            raise ValueError("At most eight independent bonus rules are supported")
        for versions in families.values():
            first = versions[0]
            if len({rule.effective_from for rule in versions}) != len(versions) or any(
                (rule.timezone, rule.reset_month, rule.reset_day, rule.contract_start)
                != (first.timezone, first.reset_month, first.reset_day, first.contract_start)
                for rule in versions
            ):
                raise ValueError(
                    "Bonus versions must retain a unique date and the same counter period"
                )

    def bonus_at(self, instant: datetime) -> BonusRule | None:
        """Return the latest effective rule, or None before the first. Historical rules never stack."""
        return max(
            (rule for rule in self.bonuses if rule.effective_from <= aware(instant)),
            key=lambda rule: rule.effective_from,
            default=None,
        )

    @cached_property
    def version(self) -> str:
        """Identify original rules and their recorded/effective instants by content hash."""
        body = asdict(self)
        for period in body["periods"]:
            if period["contract_start"] is None:
                period.pop("contract_start")
        for bonus in body["bonuses"]:
            if not bonus["monthly_schedule"]:
                bonus.pop("monthly_schedule")
            if bonus["contract_start"] is None:
                bonus.pop("contract_start")
        if not self.bonuses:
            body.pop("bonuses")  # Existing immutable no-bonus archives retain their identity.
        return hashlib.sha256(json.dumps(body, default=str, sort_keys=True).encode()).hexdigest()

    def preview(self):
        """Show both rates at -0.1, zero and +0.1 currency/kWh for every rule period."""
        return tuple(
            tuple(
                (
                    market,
                    period.import_formula.evaluate(market),
                    period.export_formula.evaluate(market),
                )
                for market in (-0.1, 0.0, 0.1)
            )
            for period in self.periods
        )


@dataclass(frozen=True)
class TariffEvidence:
    """Retain one billing segment's wholesale origin and exact selected rule period.

    A negative buy price pays for import. A negative sell price charges for export.
    Source version references preserve the original published/forecast distinction.
    Rates are currency/kWh. No supplier-specific data or volume bonus is embedded.
    """

    start: datetime
    end: datetime
    market_price: float
    buy_price: float
    sell_price: float
    price_kind: str
    native_resolution_minutes: int
    source_id: str
    source_version: str
    contract_version: str
    period_index: int


class TariffCalculator:
    """Translate a wholesale timeline through dated formulas without solver dependencies."""

    def calculate(
        self, timeline: PriceTimeline, contract: ContractSchedule
    ) -> tuple[TariffEvidence, ...]:
        """Split at effective boundaries. Reject missing contract coverage or identity mismatch."""
        if (timeline.market_area, timeline.currency) != (contract.market_area, contract.currency):
            raise ValueError("Contract area/currency mismatch")
        result = []
        for price in timeline.entries:
            cursor = price.start
            for index, period in enumerate(contract.periods):
                until = min(price.end, period.effective_until or price.end)
                if period.effective_from <= cursor < until:
                    result.append(
                        TariffEvidence(
                            cursor,
                            until,
                            price.value,
                            period.import_formula.evaluate(price.value),
                            period.export_formula.evaluate(price.value),
                            price.kind.value,
                            price.native_resolution_minutes,
                            price.source_id,
                            price.source_version,
                            contract.version,
                            index,
                        )
                    )
                    cursor = until
                if cursor == price.end:
                    break
            if cursor != price.end:
                raise ValueError("Contract coverage gap")
        return tuple(result)
