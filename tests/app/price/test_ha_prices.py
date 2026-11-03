"""Automatic HA transport preserves published-price validation, retries and corrections."""

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from open_energy_planner.prices.adapters.home_assistant import AutomaticPublishedPrices
from open_energy_planner.prices.model import PricesUnavailable
from open_energy_planner.prices.service import PriceService, PublicationSchedule
from tests.app.price.test_prices import published


class MemoryStore:
    """Retain accepted immutable series without involving financial database fixtures."""

    def __init__(self):
        """Start with no published evidence."""
        self.series = ()

    def load(self):
        """Return exactly the last successfully saved series."""
        return self.series

    def save(self, series):
        """Record the proposed immutable cache."""
        self.series = series


def report(source, now, *, available=True, reason="ready"):
    """Build a peer discovery response with no synthetic price observations."""
    return {
        **{key: value for key, value in source.request(now).items() if key != "days"},
        "available": available,
        "reason": reason,
        "responses": [],
    }


def response(source, day, now, *, value=-50):
    """Specify full market-day hourly intervals and negative EUR/MWh prices independently."""
    requested = next(row for row in source.request(now)["days"] if row["date"] == day.isoformat())
    left, right = PublicationSchedule().bounds(day)
    return {
        **requested,
        "data": {
            "NL": [
                {
                    "start": (left + timedelta(hours=index)).isoformat(),
                    "end": (left + timedelta(hours=index + 1)).isoformat(),
                    "price": value,
                }
                for index in range(int((right - left).total_seconds() / 3600))
            ]
        },
    }


@pytest.mark.parametrize("day, hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_ha_day_uses_existing_admission_and_keeps_dst_negative_prices(day, hours):
    """HA values convert once to EUR/kWh and preserve the full 23/25-hour delivery day."""

    async def scenario():
        """Refresh through the real price service before and after a peer response."""
        now = datetime.combine(day, datetime.min.time(), UTC)
        direct = AsyncMock()
        source = AutomaticPublishedPrices("NL", direct)
        store = MemoryStore()
        service = PriceService("NL", source, None, store)
        await service.refresh(now)
        body = report(source, now)
        body["responses"] = [response(source, day, now)]
        source.receive(body, now)
        service.attempts.clear()
        await service.refresh(now)
        assert service.complete(day)
        assert len(store.series[0].points) == hours
        assert {point.value for point in store.series[0].points} == {-0.05}
        direct.fetch_day.assert_not_awaited()

    asyncio.run(scenario())


def test_discovery_outage_does_not_duplicate_provider_calls_and_stale_falls_back():
    """Selected HA failures wait while absent, stale or never-connected HA permits direct access."""

    async def scenario():
        """Exercise eligibility transitions without accepting a fabricated market day."""
        now = datetime(2026, 11, 1, 8, tzinfo=UTC)
        direct = AsyncMock()
        source = AutomaticPublishedPrices("NL", direct)
        for reason in ("ready", "waiting_for_publication", "request_failed"):
            source.receive(report(source, now, reason=reason), now)
            with pytest.raises(PricesUnavailable):
                await source.fetch_day(now.date(), now)
        direct.fetch_day.assert_not_awaited()
        await source.fetch_day(now.date(), now + timedelta(minutes=3))
        assert direct.fetch_day.await_count == 1
        source.receive(report(source, now, available=False, reason="not_configured"), now)
        await source.fetch_day(now.date(), now)
        assert direct.fetch_day.await_count == 2
        unconnected = AutomaticPublishedPrices("NL", direct)
        with pytest.raises(PricesUnavailable):
            await unconnected.fetch_day(now.date(), now)
        await unconnected.fetch_day(now.date(), now + timedelta(minutes=3))
        assert direct.fetch_day.await_count == 3
        assert unconnected.request(now)["days"] == []
        assert unconnected.document(now + timedelta(minutes=3))["selected"] == "nord_pool_direct"

    asyncio.run(scenario())


def test_invalid_report_is_atomic_and_old_ack_cannot_satisfy_correction():
    """Incomplete or mismatched reports leave discovery intact and old replies cannot mask corrections."""

    async def scenario():
        """Replay an acknowledgement against a newly requested correction for the same day."""
        now = datetime(2026, 11, 1, 8, tzinfo=UTC)
        source = AutomaticPublishedPrices("NL", AsyncMock())
        with pytest.raises(PricesUnavailable):
            await source.fetch_day(now.date(), now)
        body = report(source, now)
        body["responses"] = [response(source, now.date(), now)]
        incomplete = deepcopy(body)
        incomplete["responses"][0]["data"]["NL"].pop()
        for invalid in (incomplete, {**body, "currency": "SEK"}, {**body, "market_area": "BE"}):
            with pytest.raises(ValueError):
                source.receive(invalid, now)
            assert source.reported_at is None
            assert source.responses == {}
        source.receive(body, now)
        assert (await source.fetch_day(now.date(), now)).points[0].value == -0.05
        source.receive(body, now)
        assert source.responses == {}
        with pytest.raises(PricesUnavailable):
            await source.fetch_day(now.date(), now)
        source.receive(body, now)
        assert source.responses == {}
        assert source.request(now)["days"][0]["request_id"] != body["responses"][0]["request_id"]

    asyncio.run(scenario())


def test_late_ha_response_during_direct_fetch_cannot_satisfy_later_correction():
    """A successful direct fallback retires both pending work and an HA reply received mid-fetch."""

    async def scenario():
        """Pause direct retrieval, deliver the old HA request, then request a fresh correction."""
        now = datetime(2026, 11, 1, 8, tzinfo=UTC)
        started, release = asyncio.Event(), asyncio.Event()
        direct_prices = published(now.date(), now, value=0.2)

        async def fetch_day(_day, _now):
            """Hold direct prices while the authenticated HA bridge catches up."""
            started.set()
            await release.wait()
            return direct_prices

        source = AutomaticPublishedPrices("NL", AsyncMock(fetch_day=fetch_day))
        with pytest.raises(PricesUnavailable):
            await source.fetch_day(now.date(), now)
        body = report(source, now)
        body["responses"] = [response(source, now.date(), now)]
        later = now + timedelta(minutes=3)
        retrieval = asyncio.create_task(source.fetch_day(now.date(), later))
        await started.wait()
        source.receive(body, later)
        release.set()
        assert await retrieval is direct_prices
        with pytest.raises(PricesUnavailable):
            await source.fetch_day(now.date(), later)
        assert source.request(later)["days"][0]["request_id"] != body["responses"][0]["request_id"]

    asyncio.run(scenario())
