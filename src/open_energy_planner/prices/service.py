"""Generic publication schedule and asynchronous source coordination."""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from open_energy_planner.core.units import MINUTES_PER_HOUR, SECONDS_PER_MINUTE
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog

from ..planning_policy import PLANNING_HORIZON_HOURS, PRICE_COVERAGE_HOURS
from .model import (
    MAX_PRICE_SERIES,
    MarketPriceSource,
    PriceCoverage,
    PriceForecastSource,
    PriceKind,
    PriceSeries,
    PricesUnavailable,
    combine,
)
from .policy import (
    FORECAST_FAILURE_RETRY_MINUTES,
    FORECAST_REFRESH_INTERVAL_HOURS,
    LEAD_BUCKET_COUNT,
    LEAD_BUCKET_HOURS,
    MAE_WINDOW_DAYS,
    MIN_ABSOLUTE_MAE_IMPROVEMENT_EUR_PER_KWH,
    MIN_MATCHED_HOURS,
    MIN_RELATIVE_MAE_IMPROVEMENT,
    MIN_SETTLED_DAYS,
    PUBLISHED_RETRY_MINUTES,
    REQUIRED_CONSECUTIVE_ASSESSMENTS,
)

_LOGGER = logging.getLogger(__name__)


def price_failure_reason(error):
    """Explain owned provider codes without copying arbitrary exception bodies into logs."""
    if not isinstance(error, PricesUnavailable):
        return None
    code = str(error)
    if re.fullmatch(r"price_http_[1-5][0-9]{2}", code):
        return f"HTTP {code[-3:]}"
    return {
        "published_prices_not_final": "the provider has not finalized this delivery day",
        "home_assistant_prices_pending": "Home Assistant prices have not arrived yet",
        "published_day_incomplete": "the provider's delivery day is incomplete",
        "price_forecast_expired": "the provider forecast has expired",
        "price_response_unavailable": "the provider request could not be read",
        "price_response_too_large": "the provider response exceeded its size limit",
        "source_identity_mismatch": "the provider area or currency does not match",
        "invalid_price_forecast": "the provider forecast failed validation",
        "invalid_epex_forecast": "the EPEX forecast failed validation",
        "invalid_jeroen_forecast": "the Jeroen forecast failed validation",
    }.get(code, "no usable provider prices")


class PriceStore(Protocol):
    """Persist normalized versions and current references behind an application port."""

    def load(self) -> tuple[PriceSeries, ...]:
        """Load cached normalized responses. Preserve their original issue/receipt times."""
        ...

    def save(self, series: tuple[PriceSeries, ...]) -> None:
        """Preserve immutable versions and atomically replace current references."""
        ...


@dataclass(frozen=True)
class PublicationSchedule:
    """Automatic source policy. The application does not know a provider's clock."""

    timezone: str = "Europe/Amsterdam"
    publication: time = time(13, 5)
    correction: time = time(16, 5)
    retry: timedelta = timedelta(minutes=PUBLISHED_RETRY_MINUTES)

    def bounds(self, day):
        """Return UTC delivery-day bounds, including 23/25-hour daylight-saving days."""
        zone = ZoneInfo(self.timezone)
        return (
            datetime.combine(day, time(), zone).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC),
        )


class PriceService:
    """Own cache, due fetching and semantic change detection, independent of providers.

    Refresh calls are serialized. Synchronous timeline/document reads use the
    immutable tuple replaced atomically on the event loop, without locking.
    Each source failure preserves already accepted data.
    Forecasts remain usable only until their original expiry. A changed receipt
    or issue timestamp alone does not request new optimization. The independent
    persistence_lock covers durable acceptance only. The app composition root
    assigns its shared financial-publication barrier before refresh starts.
    """

    def __init__(
        self,
        market_area: str,
        published: MarketPriceSource | None,
        forecast: PriceForecastSource | None,
        store: PriceStore,
        *,
        schedule=None,
        alternative: PriceForecastSource | None = None,
        selection=None,
        extra_forecasts=(),
    ):
        """Restore typed prices and initialize source-specific retry/periodic state."""
        self.market_area, self.published, self.forecast, self.store = (
            market_area,
            published,
            forecast,
            store,
        )
        self.schedule = schedule or PublicationSchedule()
        self.alternative, self.selection = alternative, selection
        self.extra_forecasts = tuple(extra_forecasts)
        self.selection_input_version = None
        self.issues = {}
        self.refresh_logs = {}
        try:
            loaded = store.load()
        except (ValueError, OSError, TypeError, KeyError):
            loaded = ()
            self.issues["cache"] = "cache_unreadable"
        self.series = tuple(
            source_item
            for source_item in loaded
            if source_item.market_area == market_area
            and source_item.currency == "EUR"
            and (
                selection is None
                or source_item.source_id in selection.sources
                or all(point.kind == PriceKind.PUBLISHED for point in source_item.points)
            )
        )
        if any(
            source_item.market_area != market_area or source_item.currency != "EUR"
            for source_item in loaded
        ):
            self.issues["cache"] = "cache_identity_mismatch"
        if "cache" in self.issues:
            _LOGGER.warning(
                "Price cache could not be fully restored (%s). Only accepted current-area prices will be used while sources refresh.",
                self.issues["cache"],
            )
        self.attempts = {}
        self.corrections = set()
        self.forecast_attempts = {}
        self.last_coverage = None
        self.lock = asyncio.Lock()
        # The composition root replaces this before refresh starts to coordinate durable consumers.
        self.persistence_lock = asyncio.Lock()
        self.events = None
        self.activity_requests = {}

    async def fetch_activity(self, source, now, day=None):
        """Record actual requests and keep an asynchronous HA delivery request under one ID."""
        from ..activities.events import Health, Outcome

        if self.events is None:
            candidate = await source.fetch_day(day, now) if day else await source.fetch(now)
            await self._accept(candidate, day)
            return candidate
        owner = self.activity_owner(source)
        title = "Fetch published prices" if day else "Fetch price forecast"
        request_key = owner, day
        identity = self.activity_requests.get(request_key)
        if identity is None:
            identity = await self.events.start(owner, "prices", title)
            self.activity_requests[request_key] = identity
        try:
            candidate = await source.fetch_day(day, now) if day else await source.fetch(now)
            await self._accept(candidate, day)
        except asyncio.CancelledError:
            await self.events.finish(
                identity, Outcome.INTERRUPTED, "Stopped before the price request finished."
            )
            self.activity_requests.pop(request_key, None)
            raise
        except Exception as error:
            if (
                isinstance(error, PricesUnavailable)
                and str(error) == "home_assistant_prices_pending"
            ):
                self.events.availability(
                    owner,
                    Health.UNKNOWN,
                    "Waiting for the requested Home Assistant price delivery. "
                    + self.activity_coverage(now),
                )
                raise
            detail = (
                f"Adapter: {getattr(source, 'SOURCE_ID', 'unknown')}. Market: {self.market_area}. "
                + self.activity_coverage(now)
            )
            await self.events.finish(identity, Outcome.FAILED, detail)
            self.activity_requests.pop(request_key, None)
            self.events.availability(
                owner, Health.DEGRADED if self.timeline(now).entries else Health.UNAVAILABLE, detail
            )
            raise
        transport = getattr(source, "last_transport", None)
        detail = f"Adapter: {candidate.source_id}. Market: {self.market_area}. "
        if transport:
            detail += f"Transport: {transport}. "
        if day:
            detail += f"Delivery day: {day.isoformat()}. "
        detail += self.activity_coverage(now)
        await self.events.finish(identity, Outcome.COMPLETED, detail, usable=True)
        self.activity_requests.pop(request_key, None)
        self.events.availability(owner, Health.AVAILABLE, detail)
        return candidate

    def activity_coverage(self, now):
        """Describe retained usable coverage rather than guessing validity from a fetch exception."""
        timeline = self.timeline(now)
        if timeline.entries:
            return f"Usable prices cover through {timeline.covered_until.astimezone(UTC):%Y-%m-%d %H:%M} UTC. Coverage: {timeline.status.value.replace('_', ' ')}. Original source expiry still applies."
        return "No currently usable prices remain. Planning requires usable price coverage."

    def activity_owner(self, source):
        """Keep failure/recovery episodes independent when the bidding area changes."""
        return f"prices:{getattr(source, 'SOURCE_ID', 'home_assistant')}:{self.market_area}"

    def publish_schedule(self, now):
        """Project existing publication, correction and retry clocks without running providers."""
        if not self.events or self.published is None:
            return
        local = now.astimezone(ZoneInfo(self.schedule.timezone))
        today = local.date()
        tomorrow = today + timedelta(days=1)
        due = [datetime.combine(tomorrow, self.schedule.publication, local.tzinfo)]
        for day in (today, tomorrow):
            publication = datetime.combine(today, self.schedule.publication, local.tzinfo)
            correction = datetime.combine(today, self.schedule.correction, local.tzinfo)
            if day == tomorrow and not self.complete(day) and local < publication:
                due.append(publication)
            elif not self.complete(day):
                due.append(self.attempts.get(day, now) + self.schedule.retry)
            elif day == tomorrow and day not in self.corrections and local < correction:
                due.append(correction)
        self.events.scheduled(
            self.activity_owner(self.published), "prices", "Fetch published prices", min(due)
        )

    @property
    def version(self):
        """Hash price values, coverage, kind and original resolution without receipt churn."""
        evidence = sorted(
            {
                (
                    series_item.source_id,
                    series_item.market_area,
                    series_item.currency,
                    point.start.isoformat(),
                    point.end.isoformat(),
                    point.value,
                    point.kind.value,
                    point.native_resolution_minutes,
                )
                for series_item in self.series
                for point in series_item.points
            }
        )
        if self.selection is not None:
            evidence.append(self.selection.preferred)
        return hashlib.sha256(json.dumps(evidence).encode()).hexdigest()

    def complete(self, day):
        """Check final published coverage of the complete delivery day, not a fixed slot count."""
        start, end = self.schedule.bounds(day)
        sources = tuple(
            series_item
            for series_item in self.series
            if all(point.kind == PriceKind.PUBLISHED for point in series_item.points)
        )
        timeline = combine(
            sources, start, market_area=self.market_area, hours=(end - start).total_seconds() / 3600
        )
        return timeline.status == PriceCoverage.READY

    def timeline(self, now, *, hours=PLANNING_HORIZON_HOURS, market_area=None, currency="EUR"):
        """Return selected normalized prices. Published values always outrank preferences."""
        if market_area is not None and market_area != self.market_area or currency != "EUR":
            raise ValueError("Price area/currency mismatch")
        return combine(
            self.series,
            now,
            market_area=self.market_area,
            hours=hours,
            preferred=self.selection.preferred if self.selection else (),
        )

    async def _accept(self, candidate, day=None):
        """Persist and publish one accepted cache under the app's persistence barrier.

        Provider fetching and candidate validation happen before acquiring this
        lock. The app shares it with financial publication, preventing a retained
        correction from overtaking an evaluation's final price-cursor update.

        Cancellation drains the owned writer before returning. A completed disk
        write may be loaded on restart even when cancellation prevents publishing
        it in memory. No background writer outlives this acceptance operation.
        """
        if candidate.market_area != self.market_area or candidate.currency != "EUR":
            raise PricesUnavailable("source_identity_mismatch")
        if day is not None:
            left, right = self.schedule.bounds(day)
            if any(
                point.kind != PriceKind.PUBLISHED or point.start < left or point.end > right
                for point in candidate.points
            ):
                raise PricesUnavailable("invalid_published_day")
            coverage = combine(
                (candidate,),
                left,
                market_area=self.market_area,
                hours=(right - left).total_seconds() / 3600,
            )
            if coverage.status != PriceCoverage.READY:
                raise PricesUnavailable("published_day_incomplete")
            retained = tuple(
                series_item
                for series_item in self.series
                if not (
                    series_item.source_id == candidate.source_id
                    and any(left <= point.start < right for point in series_item.points)
                )
            )
        else:
            if any(point.kind != PriceKind.FORECAST for point in candidate.points):
                raise PricesUnavailable("invalid_forecast_kind")
            retained = tuple(
                series_item
                for series_item in self.series
                if series_item.source_id != candidate.source_id
            )
        # Old delivery data stays in immutable archive files, not the live working set.
        proposed = (
            *(
                source_item
                for source_item in retained
                if source_item.points[-1].end > candidate.received_at
            ),
            candidate,
        )
        if len(proposed) > MAX_PRICE_SERIES:
            raise PricesUnavailable("price_source_limit")
        async with self.persistence_lock:
            writer = asyncio.create_task(asyncio.to_thread(self.store.save, proposed))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                # Shutdown must drain the actual thread before releasing service ownership.
                await asyncio.gather(writer, return_exceptions=True)
                raise
            self.series = proposed
            self.issues.pop("cache", None)

    async def refresh(self, now):
        """Fetch missing expected days at 30-minute retry intervals and one correction check.

        Tomorrow is expected after the publication clock. Today's missing prices
        are requested immediately on startup. Forecast request intervals are
        measured from the start of the previous attempt: 60 minutes when it
        succeeded or 30 minutes when it failed. Usable cached data retains its
        original expiry. Exceptions are source readiness, not synthetic fallback.

        Returns:
            True for changed semantic prices, readiness or usable coverage, including
            expiry between calls. Full rolling-horizon end movement alone is not a
            source change. Expected fetch/storage failures are recorded in issues.
            Unexpected programming errors propagate to the runtime logger.
        """
        async with self.lock:
            before = self.version
            previous_timeline = self.timeline(now)
            previous_coverage = self.last_coverage or self._coverage(previous_timeline)
            local = now.astimezone(ZoneInfo(self.schedule.timezone))
            today = local.date()
            self.issues = {
                key: value
                for key, value in self.issues.items()
                if not key.startswith("published:") or key.split(":", 1)[1] >= today.isoformat()
            }
            self.refresh_logs = {
                key: status
                for key, status in self.refresh_logs.items()
                if not key.startswith("published:") or key.split(":", 1)[1] >= today.isoformat()
            }
            days = [today]
            if local.time() >= self.schedule.publication:
                days.append(today + timedelta(days=1))
            for day in days if self.published is not None else ():
                issue_key = f"published:{day.isoformat()}"
                correction = (
                    day > today
                    and local.time() >= self.schedule.correction
                    and day not in self.corrections
                )
                if self.complete(day) and not correction:
                    self.issues.pop(issue_key, None)
                    continue
                previous = self.attempts.get(day)
                # A scheduled correction is allowed even if a missing-day retry was recent.
                if (
                    previous is not None
                    and now - previous < self.schedule.retry
                    and not (
                        correction
                        and previous.astimezone(ZoneInfo(self.schedule.timezone)).time()
                        < self.schedule.correction
                    )
                ):
                    continue
                self.attempts[day] = now
                status = self.refresh_logs.setdefault(
                    issue_key,
                    BackgroundLog(_LOGGER, f"Published market prices for {day.isoformat()}"),
                )
                try:
                    candidate = await self.fetch_activity(self.published, now, day)
                    if correction and self.complete(day):
                        self.corrections.add(day)
                    self.issues.pop(issue_key, None)
                    status.success(
                        "The complete delivery day is available. Scheduled correction checks remain enabled."
                    )
                except (PricesUnavailable, OSError) as error:
                    self.issues[issue_key] = str(error)
                    status.exception(
                        "Previously accepted prices keep their original expiry. Planning requires usable price coverage.",
                        retry_seconds=self.schedule.retry.total_seconds(),
                        expected=True,
                        reason=price_failure_reason(error),
                        waiting=str(error)
                        in {
                            "published_prices_not_final",
                            "home_assistant_prices_pending",
                            "published_day_incomplete",
                        },
                    )
            paired = []
            for key, source in (
                ("forecast", self.forecast),
                ("alternative_forecast", self.alternative),
                *((source.SOURCE_ID, source) for source in self.extra_forecasts),
            ):
                if source is None:
                    continue
                previous = self.forecast_attempts.get(key)
                interval = (
                    timedelta(minutes=FORECAST_FAILURE_RETRY_MINUTES)
                    if key in self.issues
                    else timedelta(hours=FORECAST_REFRESH_INTERVAL_HOURS)
                )
                if previous is not None and now - previous < interval:
                    continue
                self.forecast_attempts[key] = now
                status = self.refresh_logs.setdefault(
                    key,
                    BackgroundLog(_LOGGER, f"Price forecast {getattr(source, 'SOURCE_ID', key)}"),
                )
                try:
                    candidate = await self.fetch_activity(source, now)
                    paired.append(candidate)
                    self.issues.pop(key, None)
                    status.success(
                        f"Received {len(candidate.points)} price intervals. Next refresh in {FORECAST_REFRESH_INTERVAL_HOURS * MINUTES_PER_HOUR:g} minutes."
                    )
                except (PricesUnavailable, OSError) as error:
                    self.issues[key] = str(error)
                    status.exception(
                        "Previously accepted prices keep their original expiry. Other available sources remain usable.",
                        retry_seconds=FORECAST_FAILURE_RETRY_MINUTES * SECONDS_PER_MINUTE,
                        expected=True,
                        reason=price_failure_reason(error),
                    )
                finally:
                    if self.events:
                        minutes = (
                            FORECAST_FAILURE_RETRY_MINUTES
                            if key in self.issues
                            else FORECAST_REFRESH_INTERVAL_HOURS * MINUTES_PER_HOUR
                        )
                        self.events.scheduled(
                            self.activity_owner(source),
                            "prices",
                            "Fetch price forecast",
                            now + timedelta(minutes=minutes),
                        )
            await self.assess(now, tuple(paired))
            current_timeline = self.timeline(now)
            current_coverage = self._coverage(current_timeline)
            if self.last_coverage is None or previous_coverage != current_coverage:
                _LOGGER.info(
                    "Market price coverage: %s. Usable intervals: %d. Only current, unexpired source data is admitted.",
                    current_timeline.status.value.replace("_", " "),
                    len(current_timeline.entries),
                )
            self.last_coverage = current_coverage
            self.publish_schedule(now)
            return before != self.version or previous_coverage != current_coverage

    async def assess(self, now, paired=()):
        """Own the comparison writer through completion. Failed storage retains the previous preference."""
        if self.selection is not None:
            day = now.astimezone(ZoneInfo("UTC")).date().isoformat()
            if (
                not paired
                and self.selection_input_version == self.version
                and all(
                    self.selection.states.get(bucket, {}).get("assessed_on") == day
                    for bucket in range(LEAD_BUCKET_COUNT)
                )
            ):
                return
            writer = asyncio.create_task(
                asyncio.to_thread(self.selection.update, self.series, now, tuple(paired))
            )
            try:
                self.selection.states = await asyncio.shield(writer)
                self.selection.issue = None
                self.selection_input_version = self.version
                self.issues.pop("selection", None)
                if "selection" in self.refresh_logs:
                    self.refresh_logs["selection"].success(
                        "Price source selection is stored again."
                    )
            except asyncio.CancelledError:
                await asyncio.gather(writer, return_exceptions=True)
                raise
            except OSError as error:
                self.selection.issue = str(error)
                self.issues["selection"] = str(error)
                self.refresh_logs.setdefault(
                    "selection", BackgroundLog(_LOGGER, "Price source selection storage")
                ).exception(
                    "The previous source preference remains active. Check storage availability. A later price refresh will retry.",
                )

    def _coverage(self, timeline):
        """Compare usable prefixes without treating a full rolling horizon's moving end as a new input."""
        return (
            timeline.status,
            None
            if timeline.status in (PriceCoverage.READY, PriceCoverage.UNAVAILABLE)
            else timeline.covered_until,
        )

    def document(self, now):
        """Expose source-neutral readiness, provenance and coverage to the local API."""
        timeline = self.timeline(now)
        document = {
            "version": self.version,
            "status": timeline.status,
            "market_area": self.market_area,
            "currency": "EUR",
            "requested_until": timeline.requested_end.isoformat(),
            "covered_until": timeline.covered_until.isoformat(),
            "issues": dict(self.issues),
            "entries": [asdict(price_entry) for price_entry in timeline.entries],
            "forecast_selection": self.selection_document(now),
            "source_status": [
                {
                    "source_id": source.source_id,
                    "issued_at": source.issued_at,
                    "received_at": source.received_at,
                    "covered_until": source.points[-1].end,
                    "available": (source.expires_at is None or source.expires_at > now)
                    and source.points[-1].end > now,
                }
                for source in self.series
                if any(point.kind == PriceKind.FORECAST for point in source.points)
            ],
            "source_preferences": {
                "available_sources": list(getattr(self.selection, "sources", ())),
                "source_horizons_hours": dict(getattr(self.selection, "source_horizons", {})),
                "preferences": list(getattr(self.selection, "active_manual", ())),
                "stored_preferences": list(getattr(self.selection, "manual", ())),
                "configuration_version": getattr(self.selection, "configuration_version", None),
            },
            "selection_policy": {
                "planning_horizon_hours": PLANNING_HORIZON_HOURS,
                "price_coverage_hours": PRICE_COVERAGE_HOURS,
                "relative_improvement": MIN_RELATIVE_MAE_IMPROVEMENT,
                "absolute_improvement_eur_per_kwh": MIN_ABSOLUTE_MAE_IMPROVEMENT_EUR_PER_KWH,
                "consecutive_days": REQUIRED_CONSECUTIVE_ASSESSMENTS,
                "window_days": MAE_WINDOW_DAYS,
                "minimum_days": MIN_SETTLED_DAYS,
                "minimum_hours": MIN_MATCHED_HOURS,
            },
        }
        return json.loads(json.dumps(document, default=lambda value: value.isoformat()))

    def supports_identity(self, market_area, currency):
        """A fixed-area injected service accepts only its original normalized identity."""
        return (market_area, currency) == (self.market_area, "EUR")

    def selection_document(self, now):
        """Explain preference versus actual use per lead bucket, including safe fallback."""
        extended = self.timeline(now, hours=PRICE_COVERAGE_HOURS)
        rows = []
        for bucket in range(LEAD_BUCKET_COUNT):
            left, right = (
                now + timedelta(hours=LEAD_BUCKET_HOURS * bucket),
                now + timedelta(hours=LEAD_BUCKET_HOURS * (bucket + 1)),
            )
            used = sorted(
                {
                    price_entry.source_id
                    for price_entry in extended.entries
                    if price_entry.start < right
                    and price_entry.end > left
                    and price_entry.kind == PriceKind.FORECAST
                }
            )
            published = any(
                price_entry.start < right
                and price_entry.end > left
                and price_entry.kind == PriceKind.PUBLISHED
                for price_entry in extended.entries
            )
            state = dict(self.selection.state(bucket)) if self.selection else {}
            preferred = self.selection.preferred[bucket] if self.selection else None
            reason = (
                "available_source_fallback"
                if used and preferred and any(source != preferred for source in used)
                else "published_prices"
                if published and not used
                else "manual_preference"
                if state.get("selection_mode") == "manual" and used
                else state.get("reason", "available_forecast" if used else "no_forecast_coverage")
            )
            rows.append(
                {
                    **state,
                    "lead_start_hours": bucket * LEAD_BUCKET_HOURS,
                    "lead_end_hours": (bucket + 1) * LEAD_BUCKET_HOURS,
                    "preferred_source": preferred,
                    "primary_source": state.get("primary_source", self.selection.primary)
                    if self.selection
                    else None,
                    "alternative_source": state.get(
                        "alternative_source", self.selection.alternative
                    )
                    if self.selection
                    else None,
                    "active_sources": used,
                    "published_prices_used": published,
                    "usage_reason": reason,
                    "selection_issue": self.selection.issue if self.selection else None,
                }
            )
        return rows
