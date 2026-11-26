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
from tests.frontend.browser.documentation_fixture import START
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
    expect(page.locator("#price-low-label")).to_have_text("12 cheapest quarters")
    expect(page.locator("#price-high-label")).to_have_text("12 most expensive quarters")
    expect(page.locator("#price-daily-status")).to_contain_text(
        "The cheapest quarters use import rates."
    )
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


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_linked_plan_ranks_exact_import_and_bonus_inclusive_export_intervals(app_page):
    """The cap keeps 12 quarters. Bonus moves low base-export rates 0, 2 and 4 to the top."""
    page, url = app_page
    start = datetime(2026, 11, 4, 9, tzinfo=UTC)
    boundaries = [start + timedelta(minutes=15 * index) for index in range(16)]
    import_order = [12, 9, 6, 3, 1, 5, 7, 8, 10, 11, 13, 14, 0, 2, 4]
    export_order = [0, 2, 4, 14, 13, 12, 11, 10, 9, 8, 7, 6]
    bonuses = {0: 0.9, 2: 0.8, 4: 0.7}
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    page.clock.set_fixed_time(start)

    def ranked_quarters(route):
        """Provide a nonchronological import ladder and an increasing base-export ladder."""
        document = route.fetch().json()
        plan = document["plan"]
        snapshot = plan["input_snapshot"]
        interval = snapshot["problem"]["intervals"][0]
        tariff = snapshot["tariff_evidence"][0]
        command = plan["steps"][0]
        flows = plan["expected_flows"][0]
        plan.update(generated_at=start.isoformat(), valid_until=boundaries[-1].isoformat())
        plan["steps"], plan["expected_flows"] = [], []
        snapshot["problem"]["intervals"], snapshot["tariff_evidence"] = [], []
        for index in range(15):
            begin, end = boundaries[index].isoformat(), boundaries[index + 1].isoformat()
            buy = 0.05 + import_order.index(index) * 0.02
            sell = 0.10 + index * 0.01
            snapshot["problem"]["intervals"].append(
                {**interval, "start": begin, "buy_price": buy, "sell_price": sell}
            )
            snapshot["tariff_evidence"].append(
                {**tariff, "start": begin, "end": end, "buy_price": buy, "sell_price": sell}
            )
            plan["steps"].append({**command, "start": begin, "end": end})
            plan["expected_flows"].append(flows)
        snapshot["problem"]["export_bonuses"] = [
            {"rates": [bonuses.get(index, 0) for index in range(15)]}
        ]
        route.fulfill(json=document)

    page.route("**/comparison?view=plan", ranked_quarters)
    page.goto(url + "/overview?plan=" + plan_id)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-daily-status")).to_contain_text(
        "The cheapest quarters use import rates."
    )
    price = page.locator("#price-chart")
    marks = price.evaluate("canvas => Chart.getChart(canvas).dailyPriceQuarters")
    assert [(mark["extreme"], mark["start"]) for mark in marks] == [
        (kind, boundaries[index].timestamp() * 1000)
        for kind, indexes in (("cheapest", import_order[:12]), ("most_expensive", export_order))
        for index in indexes
    ]
    price.press("Home")
    reading = page.locator("#linked-chart-reading")
    expect(reading).to_contain_text("Among the day's highest export rates")
    expect(reading).not_to_contain_text("Among the day's lowest import rates")
    expect(page.locator("#price-chart-tooltip")).to_contain_text(
        "Among the day's highest export rates"
    )
    price.press("ArrowRight")
    expect(reading).to_contain_text("Among the day's lowest import rates")
    expect(reading).not_to_contain_text("Among the day's highest export rates")


@pytest.mark.parametrize("app_site", ["savings_corrected"], indirect=True)
def test_linked_price_readout_uses_original_cash_rates_and_keeps_missing_values_unknown(app_page):
    """A frozen 0.30 tariff gives 0.38 for 1.25 kWh, and missing energy cannot become zero."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    plan = page.request.get(
        url + f"/v1/plans/{plan_id}/comparison?view=plan", headers=headers
    ).json()["plan"]
    page.clock.set_fixed_time(
        datetime.fromisoformat(plan["input_snapshot"]["problem"]["intervals"][0]["start"])
    )

    def adjust_grid_evidence(route):
        """Supply one known amount followed by a gap to the original-plan view."""
        document = route.fetch().json()
        opening, missing = document["plan"]["expected_flows"]
        opening["grid_import"] = 1.25
        missing.pop("grid_import")
        route.fulfill(json=document)

    page.route("**/comparison?view=plan", adjust_grid_evidence)
    page.goto(url + "/overview?plan=" + plan_id)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    price = page.locator("#price-chart")
    price.press("Home")
    reading = page.locator("#linked-chart-reading")
    expect(reading).to_contain_text("Grid import: 1.25 kWh. Cost: 0.38 EUR at 0.30 EUR/kWh.")
    price.press("End")
    expect(reading).to_contain_text("Grid import: Unavailable. Cost: Unavailable at 0.30 EUR/kWh.")


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_hovering_the_peer_chart_dismisses_a_different_pinned_interval(app_page):
    """Pin a price quarter, then hover a different quarter on the Optimizer chart."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    price = page.locator("#price-chart")
    optimizer = page.locator("#plan-chart")
    page.set_viewport_size({**page.viewport_size, "height": 1600})
    optimizer.evaluate("canvas => canvas.scrollIntoView({block: 'center', behavior: 'instant'})")
    scroll_before = page.evaluate("scrollY")
    positions = """(canvas, position) => {
        const {left, right, top, bottom} = Chart.getChart(canvas).chartArea;
        return {x: left + (right - left) * position, y: (top + bottom) / 2};
    }"""
    price.click(position=price.evaluate(positions, 0.25))
    pinned = page.locator("#price-chart-tooltip.chart-tooltip-linked")
    expect(pinned).to_be_visible()
    original_quarter = pinned.locator(".chart-tooltip-title").inner_text()
    reading = page.locator("#linked-chart-reading")
    expect(reading).to_contain_text(original_quarter)

    optimizer.hover(position=optimizer.evaluate(positions, 0.75))
    assert page.evaluate("scrollY") == scroll_before
    expect(pinned).to_be_hidden()
    expect(reading).to_be_empty()
    current_quarter = page.locator("#plan-chart-tooltip .chart-tooltip-title")
    expect(current_quarter).to_be_visible()
    assert current_quarter.inner_text() != original_quarter
