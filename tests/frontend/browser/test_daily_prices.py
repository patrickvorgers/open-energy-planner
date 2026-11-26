"""Prove that responsive price charts render backend whole-day selections without ranking prices."""

import re
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from open_energy_planner.prices.daily import DailyPrices
from open_energy_planner.prices.service import PriceService
from open_energy_planner.prices.storage.adapters.store import PriceStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.contract.support.settings import settings
from tests.frontend.browser import price_daily_fixture
from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("day,count", [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)])
def test_visual_price_fixture_respects_actual_dst_days(tmp_path, monkeypatch, day, count):
    """Synthetic published days remain final with 92 spring or 100 autumn quarters and adjacent forecasts."""
    now = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=12)
    monkeypatch.setattr(price_daily_fixture, "datetime", SimpleNamespace(now=lambda _zone: now))
    with closing(SqlPlanRepository(tmp_path)) as repository:
        documents = SqlDocuments(repository.engine, "prices")
        service = PriceService("NL", None, None, PriceStore(documents))
        price_daily_fixture.seed(service, repository.engine)
        zone = ZoneInfo("Europe/Amsterdam")
        assert len(service.series[0].points) == count
        for index, source in enumerate(service.series):
            expected_day = day + timedelta(days=index)
            assert all(
                point.start.astimezone(zone).date() == expected_day for point in source.points
            )
            assert source.points[-1].end.astimezone(zone).date() == expected_day + timedelta(days=1)
        assert service.series[0].points[-1].end == service.series[1].points[0].start
        final = DailyPrices(documents).read(
            "NL",
            "Europe/Amsterdam",
            service.series[0].points[0].start,
            service.series[0].points[-1].end,
        )[0]
        assert final["status"] == "final" and len(final["quarters"]) == count


@pytest.mark.parametrize("app_site", ["prices_daily"], indirect=True)
def test_price_daily_marks_and_accessible_description_follow_backend(app_page):
    """Complete-day selections stay identical when the visible window shrinks, with desktop/mobile evidence."""
    page, url = app_page
    discover(page, url)
    response = page.request.put(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}, data=settings()
    )
    assert response.ok
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-daily-status")).to_contain_text("including elapsed quarters")
    expect(page.locator("#price-daily-status")).to_contain_text("Final selections are retained")
    actual = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).dailyPriceQuarters"
    )
    daily = page.request.get(
        url + "/v1/prices/daily", headers={"Authorization": "Bearer " + TOKEN}
    ).json()["days"]
    expected = {
        (datetime.fromisoformat(start).timestamp() * 1000, extreme)
        for day in daily
        if day["status"] != "incomplete"
        for extreme in ("cheapest", "most_expensive")
        for start in day[extreme]
    }
    assert {(row["start"], row["extreme"]) for row in actual} == expected
    expect(page.get_by_label("Whole-day spot-price selections", exact=True)).to_have_count(0)
    expect(page.locator("#price-daily-detail")).to_have_count(0)
    description = page.get_by_role("img", name="Price inputs", exact=True)
    expect(description).to_have_accessible_description(re.compile("12 most expensive quarters"))
    page.get_by_role("button", name="Full period", exact=True).click()
    directory = Path("results/ci")
    directory.mkdir(parents=True, exist_ok=True)
    page.locator("figure").filter(has=page.locator("#price-chart")).screenshot(
        path=str(directory / f"daily-prices-{page.viewport_size['width']}.png")
    )
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    assert (
        page.evaluate("Chart.getChart(document.getElementById('price-chart')).dailyPriceQuarters")
        == actual
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_original_plan_keeps_frozen_prices_and_ranks_contract_rates(app_page):
    """Original contract rates determine the plan bands without rewriting frozen chart prices."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    plan = page.request.get(
        url + f"/v1/plans/{plan_id}/comparison?view=plan", headers=headers
    ).json()["plan"]
    frozen_tariffs = plan["input_snapshot"]["tariff_evidence"]

    page.goto(url + "/overview?plan=" + plan_id)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-low-label")).to_have_text("Lowest import rates")
    expect(page.locator("#price-high-label")).to_have_text("Highest export rates")
    marks = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).dailyPriceQuarters"
    )
    assert {mark["extreme"] for mark in marks} == {"cheapest", "most_expensive"}
    assert {mark["start"] for mark in marks} <= {
        datetime.fromisoformat(interval["start"]).timestamp() * 1000
        for interval in plan["input_snapshot"]["problem"]["intervals"]
    }
    canvas = page.locator("#price-chart")
    canvas.scroll_into_view_if_needed()
    pointer = page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('price-chart'));
        const quarter = chart.dailyPriceQuarters[0];
        return {x: chart.scales.x.getPixelForValue(quarter.start + (quarter.end - quarter.start) * 0.8),
                y: chart.chartArea.top + chart.chartArea.height / 2};
    }""")
    canvas.hover(position=pointer)
    expect(page.locator("#price-chart-tooltip")).to_contain_text(
        "Among the day's lowest import rates in this plan"
    )
    rates = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).data.datasets.map(dataset => dataset.data.map(point => point.y))"
    )
    assert rates == [
        [tariff[field] for tariff in frozen_tariffs for _ in range(2)]
        for field in ("market_price", "buy_price", "sell_price")
    ]
