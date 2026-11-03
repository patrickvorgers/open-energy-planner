"""Select published HA prices before direct retrieval without coupling the planner to HA."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..model import MAX_PRICE_POINTS, PriceKind, PricePoint, PriceSeries, PricesUnavailable
from ._parsing import stamp

DISCOVERY_MAX_AGE = timedelta(minutes=2)
MAX_RESPONSE_DAYS = 2


class AutomaticPublishedPrices:
    """Own transient HA discovery and pending day requests, retaining durable prices elsewhere.

    The authenticated HA bridge supplies discovery and action responses. Missing
    or incompatible installations select the direct adapter. A selected HA source
    retains its retry boundary during publication delays and service failures.
    """

    def __init__(self, market_area, direct):
        """Bind one contract area and its existing direct published-price adapter."""
        self.market_area = market_area
        self.direct = direct
        self.reported_at = None
        self.available = None
        self.reason = "discovering"
        self.pending = {}
        self.responses = {}
        self.started_at = None

    def selected(self, now):
        """Return HA eligibility only while its authenticated discovery report is fresh."""
        return (
            self.available
            if self.reported_at and timedelta() <= now - self.reported_at <= DISCOVERY_MAX_AGE
            else None
        )

    async def fetch_day(self, day, now):
        """Delegate absent HA configurations, otherwise request a day through the HA bridge."""
        selected = self.selected(now)
        if day in self.responses:
            return self.responses.pop(day)
        if self.started_at is None:
            self.started_at = now
        if (
            selected is False
            or self.reported_at
            and selected is None
            or self.reported_at is None
            and now - self.started_at >= DISCOVERY_MAX_AGE
        ):
            candidate = await self.direct.fetch_day(day, now)
            self.pending.pop(day, None)
            self.responses.pop(day, None)
            return candidate
        self.pending.setdefault(day, uuid4().hex)
        raise PricesUnavailable("home_assistant_prices_pending")

    def request(self, now):
        """Return wholesale requests, pruning pending and queued days to market today/tomorrow.

        Snapshot reads call this on the event loop without yielding. Expired
        transient work is removed from both dictionaries, never durable prices.
        """
        today = now.astimezone(ZoneInfo("Europe/Oslo")).date()
        self.pending = {
            day: token for day, token in self.pending.items() if 0 <= (day - today).days <= 1
        }
        self.responses = {
            day: response
            for day, response in self.responses.items()
            if 0 <= (day - today).days <= 1
        }
        return {
            "market_area": self.market_area,
            "currency": "EUR",
            "days": [
                {"date": day.isoformat(), "request_id": self.pending[day]}
                for day in sorted(self.pending)
            ],
        }

    def receive(self, body, now):
        """Validate the full report before updating discovery and queuing matching responses.

        Returns:
            A tuple of eligibility change and validated matching day/series pairs.
            Obsolete request identities are acknowledged without queuing prices.

        Raises:
            ValueError: If identities, prices or full-day coverage are invalid.
            TypeError: If a payload value has an unsupported type.
            KeyError: If required interval fields are missing.
            OverflowError: If a numeric or date value exceeds supported bounds.
        """
        if not isinstance(body, dict) or set(body) != {
            "market_area",
            "currency",
            "available",
            "reason",
            "responses",
        }:
            raise ValueError("Invalid HA price report")
        if body["market_area"] != self.market_area or body["currency"] != "EUR":
            raise ValueError("HA price area or currency changed")
        if type(body["available"]) is not bool or body["reason"] not in {
            "ready",
            "not_configured",
            "not_loaded",
            "incompatible",
            "ambiguous",
            "waiting_for_publication",
            "request_failed",
        }:
            raise ValueError("Invalid HA price discovery")
        if body["available"] != (
            body["reason"] in {"ready", "waiting_for_publication", "request_failed"}
        ):
            raise ValueError("Inconsistent HA price discovery")
        rows = body["responses"]
        if not isinstance(rows, list) or len(rows) > MAX_RESPONSE_DAYS:
            raise ValueError("Invalid HA price responses")
        candidates = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"date", "request_id", "data"}:
                raise ValueError("Invalid HA price response")
            if not isinstance(row["request_id"], str) or len(row["request_id"]) != 32:
                raise ValueError("Invalid HA price request identity")
            day = date.fromisoformat(row["date"])
            if not body["available"] or day in seen:
                raise ValueError("Unrequested HA price day")
            seen.add(day)
            payload = row["data"]
            if not isinstance(payload, dict) or set(payload) != {self.market_area}:
                raise ValueError("Unexpected HA price area")
            points = payload[self.market_area]
            if not isinstance(points, list) or not points or len(points) > MAX_PRICE_POINTS:
                raise ValueError("Invalid HA price intervals")
            normalized = []
            for point in points:
                if type(point["price"]) not in (int, float):
                    raise ValueError("Invalid HA market price")
                start, end = stamp(point["start"]), stamp(point["end"])
                normalized.append(
                    PricePoint(
                        start,
                        end,
                        point["price"] / 1000,
                        PriceKind.PUBLISHED,
                        int((end - start).total_seconds() / 60),
                    )
                )
            candidate = PriceSeries(
                "nord_pool", self.market_area, "EUR", now, None, None, tuple(normalized)
            )
            zone = ZoneInfo("Europe/Oslo")
            left = datetime.combine(day, time(), zone).astimezone(UTC)
            right = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
            if (
                candidate.points[0].start != left
                or candidate.points[-1].end != right
                or any(
                    first.end != second.start
                    for first, second in zip(candidate.points, candidate.points[1:])
                )
            ):
                raise ValueError("Incomplete HA price day")
            if day in self.pending and row["request_id"] == self.pending[day]:
                candidates.append((day, candidate))
        changed = self.available != body["available"]
        self.available, self.reason, self.reported_at = body["available"], body["reason"], now
        for day, candidate in candidates:
            self.responses[day] = candidate
            self.pending.pop(day, None)
        return changed, candidates

    def document(self, now):
        """Expose the selected transport separately from price coverage and forecast preferences."""
        selected = self.selected(now)
        discovery_expired = (
            self.started_at is not None and now - self.started_at >= DISCOVERY_MAX_AGE
        )
        return {
            "mode": "automatic",
            "selected": None
            if self.reported_at is None and not discovery_expired
            else "home_assistant_nord_pool"
            if selected
            else "nord_pool_direct",
            "reason": self.reason
            if selected is not None
            else "discovering"
            if self.reported_at is None and not discovery_expired
            else "connection_stale",
        }


def bridge(prices):
    """Return only an active HA source, excluding retained resources of invalid configuration."""
    if getattr(prices, "configuration_issue", None):
        return None
    service = getattr(prices, "service", prices)
    source = getattr(service, "published", None)
    return source if isinstance(source, AutomaticPublishedPrices) else None
