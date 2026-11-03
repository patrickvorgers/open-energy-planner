"""Immutable market-price contracts. No HTTP, provider payloads or retail formulas."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import cached_property
from itertools import islice, pairwise
from math import isfinite
from typing import Protocol

from open_energy_planner.core.units import SECONDS_PER_HOUR

from ..planning_policy import FINE_INTERVAL_MINUTES, PLANNING_HORIZON_HOURS
from .policy import LEAD_BUCKET_HOURS

MAX_PRICE_POINTS = 512
MAX_PRICE_SERIES = 8
MIN_PRICE_INTERVAL = timedelta(minutes=15)


def aware(value: datetime) -> datetime:
    """Require an aware timestamp and canonicalize equal instants to UTC for stable archives."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Price timestamps require a timezone")
    return value.astimezone(UTC)


class PriceKind(StrEnum):
    """Distinguish final published wholesale prices from uncertain model forecasts."""

    PUBLISHED = "published"
    FORECAST = "forecast"


class PriceCoverage(StrEnum):
    """Classify the contiguous usable price prefix without changing API wire values."""

    READY = "ready"
    SHORTENED_HORIZON = "shortened_horizon"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PricePoint:
    """A half-open price interval in currency/kWh, preserving original resolution.

    Negative prices are valid. Missing/non-finite values are not zero. Retail
    taxes, fees, compensation and bonuses belong to the contract calculation.
    """

    start: datetime
    end: datetime
    value: float
    kind: PriceKind
    native_resolution_minutes: int

    def __post_init__(self):
        """Reject malformed time, value, classification and resolution metadata."""
        object.__setattr__(self, "start", aware(self.start))
        object.__setattr__(self, "end", aware(self.end))
        if (
            self.end - self.start < MIN_PRICE_INTERVAL
            or type(self.value) not in (int, float)
            or not isfinite(self.value)
            or not isinstance(self.kind, PriceKind)
            or type(self.native_resolution_minutes) is not int
            or self.native_resolution_minutes <= 0
        ):
            raise ValueError("Invalid price interval")
        object.__setattr__(self, "value", float(self.value))


@dataclass(frozen=True)
class PriceSeries:
    """Freeze one normalized response with area, currency and issue/receipt provenance.

    Gaps are permitted and remain explicit during composition. Overlaps within
    a response are rejected. Provider issue time may be unknown, never fabricated.
    Forecast expiry is independent of horizon and repeated receipt time.
    """

    source_id: str
    market_area: str
    currency: str
    received_at: datetime
    issued_at: datetime | None
    expires_at: datetime | None
    points: tuple[PricePoint, ...]

    def __post_init__(self):
        """Validate immutable ordered content and source/temporal identity."""
        object.__setattr__(self, "received_at", aware(self.received_at))
        for name in ("issued_at", "expires_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, aware(value))
        if self.issued_at is not None and self.issued_at > self.received_at:
            raise ValueError("Price issue time is in the future")
        if not all(
            isinstance(value_item, str) and value_item.strip()
            for value_item in (self.source_id, self.market_area, self.currency)
        ):
            raise ValueError("Price identity is required")
        if not isinstance(self.points, tuple) or not 1 <= len(self.points) <= MAX_PRICE_POINTS:
            raise ValueError("A price series requires between one and 512 immutable points")
        if not all(isinstance(point, PricePoint) for point in self.points):
            raise TypeError("Expected typed price points")
        if any(
            point_item.end > next_point.start
            for point_item, next_point in zip(self.points, self.points[1:])
        ):
            raise ValueError("Price intervals overlap or are unordered")

    @cached_property
    def version(self) -> str:
        """Identify exact source content/issue metadata without receipt-time churn."""
        body = asdict(self)
        body.pop("received_at")
        return hashlib.sha256(json.dumps(body, default=str, sort_keys=True).encode()).hexdigest()


class PricesUnavailable(ValueError):
    """A bounded source/readiness failure. No replacement price is invented."""


class MarketPriceSource(Protocol):
    """Fetch normalized final prices for a delivery date without exposing provider JSON."""

    async def fetch_day(self, day: date, now: datetime) -> PriceSeries:
        """Return available final delivery intervals or raise PricesUnavailable."""
        ...


class PriceForecastSource(Protocol):
    """Fetch an independent forecast covering the still-unpublished horizon."""

    HORIZON_HOURS: int

    async def fetch(self, now: datetime) -> PriceSeries:
        """Return normalized forecast intervals with bounded freshness."""
        ...


@dataclass(frozen=True)
class PriceSlice:
    """A composed interval retaining source version, price kind and native granularity."""

    start: datetime
    end: datetime
    value: float
    kind: PriceKind
    native_resolution_minutes: int
    source_id: str
    source_version: str


@dataclass(frozen=True)
class PriceTimeline:
    """A contiguous usable prefix. Missing coverage is an explicit shortened horizon."""

    start: datetime
    requested_end: datetime
    market_area: str
    currency: str
    entries: tuple[PriceSlice, ...]

    @property
    def covered_until(self) -> datetime:
        """Return the first unavailable instant, including an immediate start gap."""
        return self.entries[-1].end if self.entries else self.start

    @property
    def status(self) -> PriceCoverage:
        """Expose full, shortened or unavailable coverage without substituting zero."""
        if not self.entries:
            return PriceCoverage.UNAVAILABLE
        return (
            PriceCoverage.READY
            if self.covered_until == self.requested_end
            else PriceCoverage.SHORTENED_HORIZON
        )


def combine(
    series: Iterable[PriceSeries],
    start: datetime,
    *,
    market_area: str,
    currency: str = "EUR",
    hours: float = PLANNING_HORIZON_HOURS,
    preferred: tuple[str | None, ...] = (),
) -> PriceTimeline:
    """Prefer published prices per subinterval and stop at the first uncovered gap.

    Project selected rates onto UTC quarters without changing their native
    resolution metadata. Repeat hourly prices without interpolation. Partial
    horizon edges and exact source boundaries remain explicit. Never average
    away published quarter-hour evidence. Control resolution belongs to planning.
    Expired forecasts are ignored. Cross-area/currency mixing is rejected.
    At most eight ordered series of 512 points are admitted. A boundary sweep
    scans at most one active point per source, costing O(P log P + P*S) for
    P points and S sources. Source intervals must be at least 15 minutes.

    Args:
        series: Normalized source responses for the same area and currency.
        start: Aware planning instant. Source expiry is checked against this instant,
            independently of the future delivery horizon.
        market_area: Internal bidding-zone identity.
        currency: Required wholesale currency. Values are currency/kWh.
        hours: Positive floating-point horizon length in hours, including DST days.
        preferred: Optional forecast source IDs per successive 24-hour lead bucket.
            Missing/expired preferred coverage falls back to another usable source.

    Published prices win, followed by newer issue time (receipt when unknown),
    then source ID for deterministic ties. ValueError rejects non-positive
    horizons, naive timestamps and mismatched area/currency.
    """
    start = aware(start)
    end = start + timedelta(hours=hours)
    if hours <= 0:
        raise ValueError("Positive horizon required")
    sources = tuple(islice(series, MAX_PRICE_SERIES + 1))
    if len(sources) > MAX_PRICE_SERIES:
        raise ValueError("Too many price sources")
    starts = {}
    boundaries = {start, end}
    quarter = start.replace(
        minute=start.minute // FINE_INTERVAL_MINUTES * FINE_INTERVAL_MINUTES,
        second=0,
        microsecond=0,
    ) + timedelta(minutes=FINE_INTERVAL_MINUTES)
    while quarter < end:
        boundaries.add(quarter)
        quarter += timedelta(minutes=FINE_INTERVAL_MINUTES)
    # Evaluate rolling source preferences at quarter boundaries, not extra off-grid instants.
    for source_index, source in enumerate(sources):
        if source.market_area != market_area or source.currency != currency:
            raise ValueError("Price area/currency mismatch")
        if source.expires_at is not None and source.expires_at <= start:
            continue
        for point in source.points:
            # Expiry bounds when a forecast may be USED, not its delivery horizon.
            until = point.end
            if point.start < end and until > start:
                left, right = max(start, point.start), min(end, until)
                boundaries.update((left, right))
                starts.setdefault(left, []).append((source_index, point, source))
    entries = []
    active = {}
    for left, right in pairwise(sorted(boundaries)):
        # Ordered non-overlapping series have at most one active point each.
        # Scan only the bounded source set, not every point at every boundary.
        active = {key: value for key, value in active.items() if value[0].end > left}
        for source_index, point, source in starts.get(left, ()):
            active[source_index] = (point, source)
        if not active:
            break
        point, source = max(
            active.values(),
            key=lambda pair: (
                pair[0].kind == PriceKind.PUBLISHED,
                pair[1].source_id
                == (
                    preferred[
                        min(
                            int(
                                (left - start).total_seconds()
                                // (LEAD_BUCKET_HOURS * SECONDS_PER_HOUR)
                            ),
                            len(preferred) - 1,
                        )
                    ]
                    if preferred
                    else None
                ),
                pair[1].issued_at or pair[1].received_at,
                pair[1].source_id,
            ),
        )
        entries.append(
            PriceSlice(
                left,
                right,
                point.value,
                point.kind,
                point.native_resolution_minutes,
                source.source_id,
                source.version,
            )
        )
    return PriceTimeline(start, end, market_area, currency, tuple(entries))
