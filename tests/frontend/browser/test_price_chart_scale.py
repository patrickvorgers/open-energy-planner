"""Exercise zero-based price charts using complete synthetic tariff days and original plan inputs."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.app.contract.support.settings import settings
from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import (
    TOKEN,
    discover,
    open_chart_series,
    wait_for_product_reads,
)


@pytest.mark.parametrize("app_site", ["prices"], indirect=True)
@pytest.mark.parametrize("negative", [False, True])
def test_current_price_scale_follows_zero_negative_prices_and_legend(app_page, negative):
    """Two tariff days keep their zero reference through signed prices, hiding series and range changes."""
    page, url = app_page
    discover(page, url)
    body = settings(buy="market + 0.10", sell="market - 0.02")
    body["bonuses"] = [
        {
            "rule_id": "synthetic",
            "effective_from": body["periods"][0]["effective_from"],
            "rate_formula": "0.03",
            "cap_kwh": 5000,
            "timezone": "UTC",
        }
    ]
    response = page.request.put(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}, data=body
    )
    assert response.ok, f"Synthetic contract setup failed (HTTP {response.status})"

    def tariff_days(route):
        """Retain real coverage/kind metadata and replace only synthetic tariff values."""
        document = route.fetch().json()
        intervals = document["intervals"]
        start = datetime.fromisoformat(intervals[0]["start"])
        profile = (
            0.12,
            0.10,
            0.08,
            0.08,
            0.10,
            0.14,
            0.22,
            0.32,
            0.40,
            0.30,
            0.24,
            0.20,
            0.16,
            0.12,
            0.10,
            0.12,
            0.18,
            0.28,
            0.36,
            0.32,
            0.24,
            0.20,
            0.16,
            0.14,
        )
        assert (datetime.fromisoformat(intervals[-1]["end"]) - start).total_seconds() >= 86400
        for interval in intervals:
            hour = int((datetime.fromisoformat(interval["start"]) - start).total_seconds() // 3600)
            market = profile[hour % 24] - (0.10 if negative else 0)
            interval.update(
                market_price=market,
                buy_price=market + 0.10,
                sell_price=market - 0.02,
                total_export_price=market + 0.01,
            )
        route.fulfill(json=document)

    page.route("**/v1/prices/inputs", tariff_days)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#current-price-status")).to_contain_text("Current tariff preview")
    page.get_by_role("button", name="Full period", exact=True).click()
    bounds = page.evaluate(
        "() => {const axis=Chart.getChart(document.getElementById('price-chart')).scales.y; return [axis.min,axis.max];}"
    )
    assert bounds == pytest.approx([-0.095, 0.455] if negative else [0, 0.50 / 0.9])
    directory = Path("results/ci")
    directory.mkdir(parents=True, exist_ok=True)
    page.locator("figure").filter(has=page.locator("#price-chart")).screenshot(
        path=str(
            directory
            / f"price-scale-{'negative' if negative else 'positive'}-{page.viewport_size['width']}.png"
        )
    )
    legend = open_chart_series(page, "price-chart")
    for name in ("Spot market rate", "Export rate", "Export rate incl. bonus"):
        legend.get_by_role("checkbox", name=name, exact=True).uncheck()
    assert page.evaluate("Chart.getChart(document.getElementById('price-chart')).scales.y.min") == 0
    for name in ("Spot market rate", "Export rate", "Export rate incl. bonus"):
        legend.get_by_role("checkbox", name=name, exact=True).check()
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    minimum = page.evaluate("Chart.getChart(document.getElementById('price-chart')).scales.y.min")
    assert minimum < -0.04 if negative else minimum == 0
    page.get_by_role("button", name="Full period", exact=True).click()
    assert page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).scales.y.min"
    ) == pytest.approx(bounds[0])


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("negative", [False, True])
def test_original_plan_price_scale_uses_the_same_zero_reference(app_page, negative):
    """Linked original plans use frozen tariff inputs and the same signed scale as current previews."""
    page, url = app_page

    def original_prices(route):
        """Keep original energy evidence and vary only two synthetic original tariff intervals."""
        document = route.fetch().json()
        plan = document["plan"]
        for index, (interval, tariff) in enumerate(
            zip(
                plan["input_snapshot"]["problem"]["intervals"],
                plan["input_snapshot"]["tariff_evidence"],
                strict=True,
            )
        ):
            market = (0.12, 0.40)[index] - (0.32 if negative else 0)
            interval.update(buy_price=market + 0.10, sell_price=market - 0.02)
            tariff.update(market_price=market, buy_price=market + 0.10, sell_price=market - 0.02)
        route.fulfill(json=document)

    page.route("**/v1/plans/*/comparison?view=plan", original_prices)
    page.goto(url + "/overview?plan=browser-original-plan")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#plan-version")).to_have_value("browser-original-plan")
    expect(page.locator("#current-price-status")).to_be_empty()
    expect(page.locator("#current-price-status")).to_be_hidden()
    bounds = page.evaluate(
        "() => {const axis=Chart.getChart(document.getElementById('price-chart')).scales.y; return [axis.min,axis.max];}"
    )
    assert bounds == pytest.approx([-0.27, 0.23] if negative else [0, 0.50 / 0.9])


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_price_tooltip_uses_the_visible_bonus_rate_for_export_revenue(app_page):
    """The linked readout uses the selected quarter's rate with normal and missing bonus fallbacks."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    first_quarter = START + timedelta(hours=10)

    def original_plan_with_bonus(route):
        """Supply frozen synthetic bonus rates without changing the plan's physical flows."""
        document = route.fetch().json()
        problem = document["plan"]["input_snapshot"]["problem"]
        rates = [0.0] * len(problem["intervals"])
        rates[40:43] = [0.17, 0.0, None]
        problem["export_bonuses"] = [{"rates": rates}]
        route.fulfill(json=document)

    page.route("**/v1/plans/*/comparison?view=plan", original_plan_with_bonus)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-chart-legend")).to_contain_text("Export rate incl. bonus")
    wait_for_product_reads(page)
    page.get_by_role("button", name="Full period", exact=True).click()

    price_chart = page.locator("#price-chart")
    price_chart.scroll_into_view_if_needed()
    tooltip = page.locator("#price-chart-tooltip")
    export = tooltip.locator("tbody").filter(has_text="Grid export")
    for offset, (rate, revenue_label, amount) in enumerate(
        [
            ("0.25", "Revenue incl. bonus", "0.17"),
            ("0.08", "Revenue before bonus", "0.06"),
            ("0.08", "Revenue before bonus", "0.06"),
        ]
    ):
        midpoint = first_quarter + timedelta(minutes=15 * offset + 7)
        point = price_chart.evaluate(
            """(canvas, time) => {
                const chart = Chart.getChart(canvas);
                return {x: chart.scales.x.getPixelForValue(time), y: chart.chartArea.top + 15};
            }""",
            midpoint.timestamp() * 1000,
        )
        price_chart.click(position=point)
        expect(tooltip).to_be_visible()
        expect(export.get_by_role("row").filter(has_text="Rate").locator("td").first).to_have_text(
            rate
        )
        expect(
            export.get_by_role("row").filter(has_text=revenue_label).locator("td").first
        ).to_have_text(amount)
        expect(page.locator("#linked-chart-reading")).to_contain_text(revenue_label)
        if offset == 0:
            directory = Path("results/ci/price-tooltip-bonus")
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f"export-bonus-{page.viewport_size['width']}.png"))
