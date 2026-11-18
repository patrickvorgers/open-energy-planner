"""The simplified Savings view keeps signed cash, gaps and recovery guidance clear."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN
from tests.frontend.browser.test_measured_savings import measured_response


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("saving", [None, 0, -1.23, 12.34])
def test_matched_cash_availability_keeps_zero_and_negative_values(app_page, saving):
    """Unavailable money stays unavailable, zero remains zero and losses receive an explicit extra-cost label."""
    page, url = app_page
    data = measured_response()
    data["cash"]["savings"] = saving
    if saving is None:
        data["cash"].update(actual_cost=None, without_battery_cost=None)
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-total")).to_have_text(
        "Not available" if saving is None else f"{abs(saving):.2f} EUR"
    )
    expect(page.locator("#savings-outcome")).to_have_text(
        "Extra electricity cost" if saving is not None and saving < 0 else "Your battery saved"
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_signed_daily_chart_keeps_a_missing_day_and_keyboard_inspection(app_page):
    """The thirty-day chart retains negative bars and an unavailable day without converting either to zero."""
    page, url = app_page
    data = measured_response()
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-status")).to_contain_text("Partial period")
    values = page.locator("#savings-chart").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets[0].data.map(point => point.y)"
    )
    assert values == [row["savings"] for row in data["cash"]["chart"]]
    assert None in values and min(value for value in values if value is not None) < 0
    canvas = page.locator("#savings-chart")
    canvas.focus()
    canvas.press("Home")
    for index, value in enumerate(values):
        if index:
            canvas.press("ArrowRight")
        if value is None:
            expect(page.locator("#savings-selected")).to_contain_text("No comparable measurements")
            break


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_pause_guidance_clears_after_refresh_while_preserving_cash(app_page):
    """Owned configuration guidance disappears after recovery without hiding existing matched costs."""
    page, url = app_page
    data = measured_response()
    data["issue"] = (
        "Installation location is required for settlement. Configure the location under Installation"
    )
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-status")).to_contain_text(
        "Configure the location under Installation"
    )
    amount = page.locator("#savings-total").inner_text()
    data["issue"] = None
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#savings-status")).not_to_contain_text(
        "Configure the location under Installation"
    )
    expect(page.locator("#savings-total")).to_have_text(amount)


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_monthly_bins_have_month_ranges_and_signed_money(app_page):
    """Long-period aggregates show monthly ranges rather than claiming each bar is a single day."""
    from tests.frontend.browser.helpers import choose_period

    page, url = app_page
    data = measured_response()
    data["cash"]["chart"] = [
        {"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z", "savings": 2.34},
        {"start": "2026-02-01T00:00:00Z", "end": "2026-03-01T00:00:00Z", "savings": -0.56},
    ]
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    choose_period(page, "2026-01-01", "2026-02-28", host="#savings-period")
    expect(page.locator("#savings-daily-heading")).to_have_text("Savings by month")
    canvas = page.locator("#savings-chart")
    canvas.focus()
    canvas.press("Home")
    expect(page.locator("#savings-selected")).to_have_text("1/1/2026 – 1/31/2026: 2.34 EUR")
    canvas.press("End")
    expect(page.locator("#savings-selected")).to_have_text("2/1/2026 – 2/28/2026: -0.56 EUR")
