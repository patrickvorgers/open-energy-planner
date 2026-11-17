"""Daily ranking persistence and calendar behavior on every supported database backend."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from open_energy_planner.prices.daily import DailyPrices
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.service import PriceService, PublicationSchedule
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA


def source(area, day, *, kind=PriceKind.PUBLISHED, flat=False):
    """Build independent ascending or tied quarter prices for a complete Amsterdam day."""
    start, end = PublicationSchedule().bounds(day)
    count = int((end - start).total_seconds() / 900)
    return PriceSeries(
        "synthetic",
        area,
        "EUR",
        start,
        start,
        end + timedelta(hours=6) if kind == PriceKind.FORECAST else None,
        tuple(
            PricePoint(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                0.1 if flat else (index - 40) / 100,
                kind,
                15,
            )
            for index in range(count)
        ),
    )


def service_for(adapter, area, sources):
    """Use production snapshot selection and a real app-owned price document namespace."""
    SCHEMA.create_all(adapter.engine)
    documents = SqlDocuments(adapter.engine, "prices")
    service = PriceService(area, None, None, PriceStore(documents))
    service.series = sources
    return service, DailyPrices(documents)


@pytest.mark.parametrize(
    "day,count", [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100), (date(2026, 11, 17), 96)]
)
def test_daily_extremes_include_elapsed_quarters_and_dst(selection_store, day, count):
    """At local noon, the twelve early negative-price quarters still win across the entire day."""
    adapter, area = selection_store
    evidence = source(area, day)
    service, daily = service_for(adapter, area, (evidence,))
    now = datetime.combine(day, datetime.min.time(), ZoneInfo("Europe/Amsterdam")) + timedelta(
        hours=12
    )
    daily.update(service.daily_timeline(now), area, "Europe/Amsterdam", now)
    result = daily.read(
        area, "Europe/Amsterdam", evidence.points[0].start, evidence.points[-1].end
    )[0]
    assert result["status"] == "final"
    assert len(result["quarters"]) == count
    assert result["cheapest"] == [point.start.isoformat() for point in evidence.points[:12]]
    assert set(result["most_expensive"]) == {
        point.start.isoformat() for point in evidence.points[-12:]
    }
    assert all(datetime.fromisoformat(value) < now for value in result["cheapest"])


def test_equal_prices_have_stable_disjoint_twelve_quarter_sets(selection_store):
    """Chronological ties choose the first twelve low quarters and the next twelve high quarters."""
    adapter, area = selection_store
    evidence = source(area, date(2026, 11, 17), flat=True)
    service, daily = service_for(adapter, area, (evidence,))
    now = evidence.received_at
    daily.update(service.daily_timeline(now), area, "Europe/Amsterdam", now)
    result = daily.read(area, "Europe/Amsterdam", now, now + timedelta(days=1))[0]
    assert result["cheapest"] == [point.start.isoformat() for point in evidence.points[:12]]
    assert result["most_expensive"] == [point.start.isoformat() for point in evidence.points[12:24]]
    assert not set(result["cheapest"]) & set(result["most_expensive"])


def test_forecasts_change_until_final_day_then_survive_restart_and_correction(selection_store):
    """A forecast reversal changes rankings, published prices finalize them, and later corrections cannot move them."""
    adapter, area = selection_store
    evidence = source(area, date(2026, 11, 17), kind=PriceKind.FORECAST)
    service, daily = service_for(adapter, area, (evidence,))
    now = evidence.received_at
    end = evidence.points[-1].end
    daily.update(service.daily_timeline(now), area, "Europe/Amsterdam", now)
    original = daily.read(area, "Europe/Amsterdam", now, end)[0]
    assert original["status"] == "provisional"
    reversed_source = replace(
        evidence, points=tuple(replace(point, value=-point.value) for point in evidence.points)
    )
    service.series = (reversed_source,)
    daily.update(service.daily_timeline(now), area, "Europe/Amsterdam", now)
    revised = daily.read(area, "Europe/Amsterdam", now, end)[0]
    assert set(revised["cheapest"]) == {point.start.isoformat() for point in evidence.points[-12:]}
    assert revised["cheapest"] != original["cheapest"]
    service.series = (source(area, date(2026, 11, 17)), reversed_source)
    daily.update(service.daily_timeline(now), area, "Europe/Amsterdam", now)
    final = daily.read(area, "Europe/Amsterdam", now, end)[0]
    assert final["status"] == "final" and final["cheapest"] == original["cheapest"]
    restarted, reloaded = service_for(
        adapter,
        area,
        (
            replace(
                reversed_source,
                expires_at=None,
                points=tuple(
                    replace(point, kind=PriceKind.PUBLISHED) for point in reversed_source.points
                ),
            ),
        ),
    )
    reloaded.update(restarted.daily_timeline(now), area, "Europe/Amsterdam", now)
    assert reloaded.read(area, "Europe/Amsterdam", now, end)[0] == final
    assert reloaded.read(area + "other", "Europe/Amsterdam", now, end)[0]["status"] == "incomplete"


def test_rolling_forecasts_retain_elapsed_prices_but_never_rank_a_partial_day(selection_store):
    """Rolling noon coverage retains saved morning quarters. A new installation cannot guess missing morning prices."""
    adapter, area = selection_store
    evidence = source(area, date(2026, 11, 17), kind=PriceKind.FORECAST)
    service, daily = service_for(adapter, area, (evidence,))
    start, end = evidence.points[0].start, evidence.points[-1].end
    daily.update(service.daily_timeline(start), area, "Europe/Amsterdam", start)
    noon = start + timedelta(hours=12)
    service.series = (replace(evidence, points=evidence.points[48:]),)
    daily.update(service.daily_timeline(noon), area, "Europe/Amsterdam", noon)
    result = daily.read(area, "Europe/Amsterdam", start, end)[0]
    assert result["status"] == "provisional" and len(result["quarters"]) == 96
    assert result["cheapest"] == [point.start.isoformat() for point in evidence.points[:12]]
    other_service, other_daily = service_for(
        adapter, area + "new", (replace(service.series[0], market_area=area + "new"),)
    )
    other_daily.update(other_service.daily_timeline(noon), area + "new", "Europe/Amsterdam", noon)
    partial = other_daily.read(area + "new", "Europe/Amsterdam", start, end)[0]
    assert partial["status"] == "incomplete"
    assert partial["cheapest"] == partial["most_expensive"] == []
    assert len(partial["quarters"]) == 48


def test_expired_forecast_is_not_resurrected_by_midnight_projection(selection_store):
    """A six-hour forecast already expired at noon cannot provide future quarters through a midnight clock."""
    adapter, area = selection_store
    evidence = source(area, date(2026, 11, 17), kind=PriceKind.FORECAST)
    start = evidence.points[0].start
    evidence = replace(evidence, expires_at=start + timedelta(hours=6))
    service, daily = service_for(adapter, area, (evidence,))
    noon = start + timedelta(hours=12)
    daily.update(service.daily_timeline(noon), area, "Europe/Amsterdam", noon)
    result = daily.read(area, "Europe/Amsterdam", start, evidence.points[-1].end)[0]
    assert result["status"] == "incomplete" and result["quarters"] == []


def test_daily_reads_bound_calendar_range_and_do_not_write(selection_store):
    """Reading an absent day stays empty and periods beyond four days are refused."""
    adapter, area = selection_store
    _, daily = service_for(adapter, area, ())
    now = datetime(2026, 11, 17, tzinfo=UTC)
    assert daily.read(area, "UTC", now, now + timedelta(days=1))[0]["status"] == "incomplete"
    assert list(daily.documents.iter_prefix("daily-")) == []
    with pytest.raises(ValueError, match="four calendar days"):
        daily.read(area, "UTC", now, now + timedelta(days=5))
