"""Exercise zero-based price charts using complete synthetic tariff days and original plan inputs."""

from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.app.contract.support.settings import settings
from tests.frontend.browser.helpers import TOKEN, discover


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
    legend = page.locator("#price-chart-legend")
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

    page.route("**/v1/plans/*/comparison", original_prices)
    page.goto(url + "/overview?plan=browser-original-plan")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#current-price-status")).to_contain_text("Selected plan")
    bounds = page.evaluate(
        "() => {const axis=Chart.getChart(document.getElementById('price-chart')).scales.y; return [axis.min,axis.max];}"
    )
    assert bounds == pytest.approx([-0.27, 0.23] if negative else [0, 0.50 / 0.9])
