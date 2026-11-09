"""Dated bonus rules, local windows and measured aggregate-export counter seeds."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cached_property
from math import isfinite
from zoneinfo import ZoneInfo

from open_energy_planner.contracts.bonus_statistics import StatisticsBasis
from open_energy_planner.contracts.calculation.condition import ComparisonCondition
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.history.model import COMPONENT_METER_TOLERANCE_KWH

from ..prices.model import aware
from .windows import BonusWindow, ClockWindow

MAX_BONUS_NUMERIC_VALUE = 1000000000.0
MAX_BONUS_ID_LENGTH = 64
MAX_COUNTER_SEEDS = 64


def nonnegative(value):
    """Reject booleans, missing values and non-finite/negative confirmed quantities."""
    if (
        type(value) not in (int, float)
        or not 0 <= value <= MAX_BONUS_NUMERIC_VALUE
        or not isfinite(value)
    ):
        raise ValueError("Expected a bounded nonnegative number")
    return float(value)


@dataclass(frozen=True)
class BonusRule:
    """One dated version of an export bonus with optional battery-export protection.

    Versions of the same rule share their period definition. Price/time gates
    control payment, not counter advancement. A zero extra rate can disable a
    bonus without resetting its yearly volume. Only the contract's latest effective rule can pay. Identities retain counter provenance.
    """

    rule_id: str
    effective_from: datetime
    rate_formula: Formula
    cap_kwh: float
    timezone: str = "Europe/Amsterdam"
    reset_month: int = 1
    reset_day: int = 1
    price_condition: ComparisonCondition | None = None
    window: str = "always"
    start_time: str | None = None
    end_time: str | None = None
    contract_start: datetime | None = None
    monthly_schedule: tuple[ClockWindow | None, ...] = ()
    prevent_battery_export: bool = False

    def __post_init__(self):
        """Validate bounded rule identity, annual boundary, formulas and exactly relevant window fields."""
        object.__setattr__(self, "effective_from", aware(self.effective_from))
        if self.contract_start is not None:
            object.__setattr__(self, "contract_start", aware(self.contract_start))
            if self.contract_start > self.effective_from:
                raise ValueError("Bonus cannot precede its contract")
        if type(self.prevent_battery_export) is not bool:
            raise ValueError("Battery export protection must be boolean")
        if not isinstance(self.rule_id, str) or not 1 <= len(self.rule_id) <= MAX_BONUS_ID_LENGTH:
            raise ValueError("Invalid bonus rule identity")
        if not isinstance(self.rate_formula, Formula) or (
            self.price_condition is not None
            and not isinstance(self.price_condition, ComparisonCondition)
        ):
            raise TypeError("Bonus rules require validated formulas")
        object.__setattr__(self, "cap_kwh", nonnegative(self.cap_kwh))
        ZoneInfo(self.timezone)
        if type(self.reset_month) is not int or type(self.reset_day) is not int:
            raise ValueError("Invalid annual boundary")
        date(2001, self.reset_month, self.reset_day)
        _ = self.payment_window
        for market in (-0.1, 0, 0.1):
            self.extra_rate(market, self.effective_from, check_window=False)

    def period_start(self, instant):
        """Return the annual reset clamped to activation. Counter owners use the first family version."""
        zone = ZoneInfo(self.timezone)
        local = aware(instant).astimezone(zone)
        candidate = datetime(local.year, self.reset_month, self.reset_day, tzinfo=zone)
        if local < candidate:
            candidate = candidate.replace(year=candidate.year - 1)
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != candidate.replace(
            tzinfo=None
        ):
            raise ValueError("Annual reset falls in a nonexistent local time")
        boundary = candidate.astimezone(UTC)
        return max(
            [
                boundary,
                *(
                    start
                    for start in (self.contract_start, self.effective_from)
                    if start is not None and aware(instant) >= start
                ),
            ]
        )

    @cached_property
    def payment_window(self):
        """Resolve every strategy through the same rate-independent window contract."""
        return BonusWindow(self.window, self.start_time, self.end_time, self.monthly_schedule)

    def boundaries(self, start, end, *, location=None):
        """Combine payment edges with dated activation and annual counter boundaries."""
        result = set(
            self.payment_window.boundaries(start, end, timezone=self.timezone, location=location)
        )
        result.add(self.effective_from)
        if self.contract_start is not None:
            result.add(self.contract_start)
        zone = ZoneInfo(self.timezone)
        day = start.astimezone(zone).date() - timedelta(days=1)
        last = end.astimezone(zone).date() + timedelta(days=1)
        while day <= last:
            result.add(self.period_start(datetime.combine(day, time(12), zone)))
            day += timedelta(days=1)
        return tuple(sorted(instant for instant in result if start < instant < end))

    def extra_rate(self, market, instant, *, check_window=True, location=None):
        """Return only the nonnegative additional rate when price/time conditions hold."""
        if self.price_condition is not None and not self.price_condition.matches(market):
            return 0.0
        if check_window and not self.payment_window.contains(
            instant, timezone=self.timezone, location=location
        ):
            return 0.0
        return nonnegative(self.rate_formula.evaluate(market))


@dataclass(frozen=True)
class CounterSeed:
    """Annual volume and optional physical endpoint at one instant in a bonus period.

    Annual planning uses the validated statistics basis when present. Physical
    endpoints remain independent evidence for settlement and archived plans.
    Unknown annual volume and unavailable physical endpoints remain null.
    """

    rule_id: str
    period_start: datetime
    used_kwh: float | None
    meter_kwh: float | None
    entity_id: str
    observed_at: datetime
    component_meters: tuple[tuple[str, float], ...] = ()
    statistics: StatisticsBasis | None = None

    def __post_init__(self):
        """Reject ambiguous counter provenance and malformed numeric/time values."""
        object.__setattr__(self, "period_start", aware(self.period_start))
        object.__setattr__(self, "observed_at", aware(self.observed_at))
        if not isinstance(self.rule_id, str) or not 1 <= len(self.rule_id) <= MAX_BONUS_ID_LENGTH:
            raise ValueError("Invalid counter rule identity")
        if not isinstance(self.entity_id, str) or not self.entity_id.startswith("sensor."):
            raise ValueError("Counter seed requires its cumulative export entity")
        if self.statistics is not None and not isinstance(self.statistics, StatisticsBasis):
            raise ValueError("Invalid counter statistics")
        if self.statistics is None or self.statistics.prefix_kwh is not None:
            nonnegative(self.used_kwh)
        elif self.used_kwh is not None:
            raise ValueError("Unknown annual volume must be null")
        if self.statistics is None or self.statistics.physical_available:
            nonnegative(self.meter_kwh)
        elif self.meter_kwh is not None or self.component_meters:
            raise ValueError("Unavailable physical endpoint must be null")
        if (
            not isinstance(self.component_meters, (tuple, list))
            or len(self.component_meters) > MAX_COUNTER_SEEDS
        ):
            raise ValueError("Invalid component meters")
        components = []
        for item in self.component_meters:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0].startswith("sensor.")
            ):
                raise ValueError("Invalid component meter")
            nonnegative(item[1])
            components.append(tuple(item))
        if len({entity for entity, _value in components}) != len(components) or (
            components
            and abs(sum(value for _entity, value in components) - self.meter_kwh)
            > COMPONENT_METER_TOLERANCE_KWH
        ):
            raise ValueError("Component meters do not match aggregate")
        object.__setattr__(self, "component_meters", tuple(components))
        if self.observed_at < self.period_start:
            raise ValueError("Counter seed predates its period")
        if self.statistics is not None:
            from .export_meter import export_identity

            if (
                self.statistics.reference_at < self.period_start
                or self.statistics.latest_at != self.observed_at
                or export_identity(item.statistic_id for item in self.statistics.series)
                != self.entity_id
            ):
                raise ValueError("Counter statistics period mismatch")
            expected = (
                self.statistics.prefix_kwh
                + sum(item.exported_kwh for item in self.statistics.series)
                if self.statistics.prefix_kwh is not None
                else None
            )
            if (
                expected is not None
                and abs(self.used_kwh - expected) > COMPONENT_METER_TOLERANCE_KWH
            ):
                raise ValueError("Counter volume differs from statistics")
            if self.statistics.physical_available and {entity for entity, _value in components} != {
                item.statistic_id for item in self.statistics.series
            }:
                raise ValueError("Physical statistics members do not match")


@dataclass(frozen=True)
class CounterBook:
    """Immutable references and measured checkpoints. Candidate scenarios never mutate actual counters."""

    recorded_at: datetime
    seeds: tuple[CounterSeed, ...]

    def __post_init__(self):
        """Require unique bounded rule/period seeds and explicit recorded-time provenance."""
        object.__setattr__(self, "recorded_at", aware(self.recorded_at))
        if (
            not isinstance(self.seeds, tuple)
            or not 1 <= len(self.seeds) <= MAX_COUNTER_SEEDS
            or any(not isinstance(seed, CounterSeed) for seed in self.seeds)
        ):
            raise ValueError("Expected 1-64 immutable counter seeds")
        if len({(seed.rule_id, seed.period_start) for seed in self.seeds}) != len(self.seeds):
            raise ValueError("Duplicate counter seed")
        if any(seed.observed_at > self.recorded_at for seed in self.seeds):
            raise ValueError("Counter seed is from the future")

    @property
    def version(self):
        """Identify exactly which confirmed counter baselines a candidate used."""
        data = asdict(self)
        for seed in data["seeds"]:
            if seed["statistics"] is None:
                seed.pop("statistics")
            if not seed["component_meters"]:
                seed.pop("component_meters")
        return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()
