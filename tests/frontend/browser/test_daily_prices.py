"""Prove that responsive price charts render backend whole-day selections without ranking prices."""

from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.app.contract.support.settings import settings
from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("app_site", ["prices_daily"], indirect=True)
def test_price_daily_marks_and_keyboard_details_follow_backend(app_page):
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
    selector = page.get_by_label("Whole-day spot-price selections", exact=True)
    expect(selector).to_be_enabled()
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
    selector.focus()
    page.keyboard.press("End")
    expect(page.locator("#price-daily-detail")).to_contain_text("12 most expensive quarters")
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
def test_original_plan_keeps_frozen_prices_and_uses_only_returned_daily_labels(app_page):
    """A deliberately contrary backend label is rendered verbatim without recalculating frozen plan prices."""
    page, url = app_page
    captured = {}

    def daily_response(route):
        """Return a backend-selected expensive quarter independently of the plan's numerical rates."""
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(route.request.url).query)
        start = datetime.fromisoformat(query["start"][0])
        from datetime import timedelta

        end = start + timedelta(minutes=15)
        captured["start"] = start.timestamp() * 1000
        route.fulfill(
            json={
                "days": [
                    {
                        "day": start.date().isoformat(),
                        "status": "final",
                        "currency": "EUR",
                        "cheapest": [],
                        "most_expensive": [start.isoformat()],
                        "quarters": [
                            {
                                "start": start.isoformat(),
                                "end": end.isoformat(),
                                "market_price": -100.0,
                                "price_kind": "published",
                            }
                        ],
                    }
                ]
            }
        )

    page.route("**/v1/prices/daily?*", daily_response)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_label("Whole-day spot-price selections", exact=True)).to_be_enabled()
    marks = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).dailyPriceQuarters"
    )
    assert marks[0]["start"] == captured["start"] and marks[0]["extreme"] == "most_expensive"
    expect(page.locator("#price-daily-detail")).to_contain_text("12 most expensive quarters")
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('price-chart')).data.datasets[0].data[0].y"
        )
        != -100.0
    )
