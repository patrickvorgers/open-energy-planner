"""Provider-neutral matched forecast evidence and stable daily source preference policy."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from open_energy_planner.core.units import SECONDS_PER_HOUR

from .model import PriceKind
from .policy import (
    LEAD_BUCKET_COUNT,
    LEAD_BUCKET_HOURS,
    MAE_THRESHOLD_TOLERANCE,
    MAE_WINDOW_DAYS,
    MAX_COMPARISON_INTERVAL_HOURS,
    MIN_ABSOLUTE_MAE_IMPROVEMENT_EUR_PER_KWH,
    MIN_MATCHED_HOURS,
    MIN_RELATIVE_MAE_IMPROVEMENT,
    MIN_SETTLED_DAYS,
    REQUIRED_CONSECUTIVE_ASSESSMENTS,
)


@dataclass(frozen=True)
class ForecastPair:
    """Two original EUR/kWh predictions for one delivery hour and identical receipt cohort."""

    bucket: int
    cohort: datetime
    target: datetime
    primary_price: float
    alternative_price: float
    primary_version: str
    alternative_version: str


@dataclass(frozen=True)
class PublishedHour:
    """One complete published UTC hour, its mean EUR/kWh price and original source reference."""

    target: datetime
    price: float
    version: str


@dataclass(frozen=True)
class ForecastScore:
    """Equal-hour-weighted matched absolute errors, without native database rows."""

    primary_mae: float | None
    alternative_mae: float | None
    matched_hours: int
    settled_days: int


class SelectionStore(Protocol):
    """Atomically preserve typed price evidence and assessment results behind a storage port."""

    def load(self, area: str) -> dict:
        """Return latest decisions. Unavailable storage raises a sanitized OSError."""
        ...

    def update(
        self,
        area: str,
        pairs: tuple[ForecastPair, ...],
        actuals: tuple[PublishedHour, ...],
        day: datetime,
        window_start: datetime,
        decide: Callable,
    ) -> dict:
        """Commit evidence and daily decisions together. Invoke policy only with plain scores/state."""
        ...


def hourly(series):
    """Return complete UTC-hour averages. Gaps and partial hours are not observations."""
    result = {}
    for source in series:
        totals = {}
        for point in source.points:
            # Comparison never expands an arbitrarily long upstream interval.
            # Supported APIs provide quarter-hour or hourly observations.
            if point.end - point.start > timedelta(hours=MAX_COMPARISON_INTERVAL_HOURS):
                continue
            left = point.start
            while left < point.end:
                hour = left.replace(minute=0, second=0, microsecond=0)
                right = min(point.end, hour + timedelta(hours=1))
                duration = (right - left).total_seconds()
                value, seconds = totals.get(hour, (0, 0))
                totals[hour] = (value + point.value * duration, seconds + duration)
                left = right
        for hour, (total, seconds) in totals.items():
            if seconds == SECONDS_PER_HOUR:
                result[hour] = total / SECONDS_PER_HOUR
    return result


class ForecastSelection:
    """Compare matched as-received forecasts without transport or database dependencies.

    The price service serializes updates in an owned background thread and installs
    the detached returned state only after persistence succeeds. Evidence is never
    retrospectively reconstructed. Source IDs are supplied by composition.
    """

    def __init__(self, store: SelectionStore, market_area: str, primary: str, alternative: str):
        """Restore preferences through the storage port. Retain explicit unavailability on failure."""
        self.store, self.area, self.primary, self.alternative = (
            store,
            market_area,
            primary,
            alternative,
        )
        self.states = {}
        self.issue = None
        try:
            self.states = store.load(market_area)
        except OSError:
            self.issue = "forecast_selection_storage_unavailable"

    @property
    def preferred(self):
        """Return source preference for each successive forecast lead bucket."""
        return tuple(
            self.state(bucket).get("preferred_source", self.primary)
            for bucket in range(LEAD_BUCKET_COUNT)
        )

    def state(self, bucket):
        """Ignore a preference for a retired provider without rewriting its archived assessment."""
        state = self.states.get(bucket, {})
        return (
            state
            if state.get("preferred_source", self.primary) in (self.primary, self.alternative)
            else {}
        )

    def update(self, series, now, paired=()):
        """Freeze complete same-round pairs and published truth, then atomically assess.

        Exclude any already known quarter of a target hour. The storage port also
        excludes targets known from earlier responses no longer in the live cache.
        Repeated receipt cohorts cannot overwrite an original pair.
        """
        published = tuple(
            series_item
            for series_item in series
            if series_item.market_area == self.area
            and series_item.currency == "EUR"
            and all(point_item.kind == PriceKind.PUBLISHED for point_item in series_item.points)
        )
        actuals = tuple(
            PublishedHour(target, price, source.version)
            for source in published
            for target, price in hourly((source,)).items()
        )
        candidates = {
            source_item.source_id: source_item
            for source_item in paired
            if source_item.market_area == self.area
            and source_item.currency == "EUR"
            and source_item.expires_at is not None
            and source_item.expires_at > now
            and source_item.received_at == now
            and all(point_item.kind == PriceKind.FORECAST for point_item in source_item.points)
        }
        pairs = []
        if self.primary in candidates and self.alternative in candidates:
            first, second = candidates[self.primary], candidates[self.alternative]
            primary_hours, alternative_hours = hourly((first,)), hourly((second,))
            cohort = now.replace(minute=0, second=0, microsecond=0)
            for target in primary_hours.keys() & alternative_hours.keys():
                bucket = int(
                    (target - now).total_seconds() // (LEAD_BUCKET_HOURS * SECONDS_PER_HOUR)
                )
                if not 0 <= bucket < LEAD_BUCKET_COUNT or any(
                    point_item.start < target + timedelta(hours=1) and point_item.end > target
                    for source in published
                    for point_item in source.points
                ):
                    continue
                pairs.append(
                    ForecastPair(
                        bucket,
                        cohort,
                        target,
                        primary_hours[target],
                        alternative_hours[target],
                        first.version,
                        second.version,
                    )
                )
        day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.store.update(
            self.area,
            tuple(pairs),
            actuals,
            day,
            day - timedelta(days=MAE_WINDOW_DAYS),
            self.decide,
        )

    def decide(self, old: dict, score: ForecastScore, day: datetime) -> dict:
        """Apply both improvement thresholds for consecutive daily assessments. Ties retain preference."""
        if old.get("preferred_source", self.primary) not in (self.primary, self.alternative):
            old = {}
        preferred = old.get("preferred_source", self.primary)
        challenger, streak, reason = None, 0, "insufficient_evidence"
        if score.settled_days >= MIN_SETTLED_DAYS and score.matched_hours >= MIN_MATCHED_HOURS:
            current, other = (
                (score.primary_mae, score.alternative_mae)
                if preferred == self.primary
                else (score.alternative_mae, score.primary_mae)
            )
            reason = "retained_within_threshold"
            if (
                current - other
                >= max(
                    current * MIN_RELATIVE_MAE_IMPROVEMENT, MIN_ABSOLUTE_MAE_IMPROVEMENT_EUR_PER_KWH
                )
                - MAE_THRESHOLD_TOLERANCE
            ):
                challenger = self.alternative if preferred == self.primary else self.primary
                consecutive = old.get("assessed_on") == (day - timedelta(days=1)).date().isoformat()
                streak = (
                    old.get("streak", 0) + 1
                    if consecutive and old.get("challenger") == challenger
                    else 1
                )
                reason = "challenger_pending"
                if streak >= REQUIRED_CONSECUTIVE_ASSESSMENTS:
                    preferred, challenger, streak, reason = (
                        challenger,
                        None,
                        0,
                        "switched_after_three_days",
                    )
        return {
            "assessed_on": day.date().isoformat(),
            "preferred_source": preferred,
            "challenger": challenger,
            "streak": streak,
            "primary_mae": score.primary_mae,
            "alternative_mae": score.alternative_mae,
            "matched_hours": score.matched_hours,
            "settled_days": score.settled_days,
            "reason": reason,
        }
