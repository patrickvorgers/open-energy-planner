"""Savings A shows one measured cash comparison and a shared responsive calendar."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


def measured_response():
    """Thirty independently specified signed daily cash results include a missing and partial day."""
    first = datetime(2026, 10, 1, tzinfo=UTC)
    chart = []
    for index in range(30):
        start, end = first + timedelta(days=index), first + timedelta(days=index + 1)
        saving = (
            None if index == 5 else -0.35 if index == 13 else round(0.8 + (index % 7) * 0.21, 2)
        )
        actual = None if saving is None else 2.0 + (index % 4) * 0.3
        chart.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "savings": saving,
                "actual_cost": actual,
                "without_battery_cost": None if actual is None else actual + saving,
                "coverage_seconds": 0 if saving is None else 43200 if index == 3 else 86400,
                "estimated_seconds": 900 if index == 2 else 0,
            }
        )
    return {
        "daily": True,
        "currency": "EUR",
        "days": [],
        "cash": {
            "savings": sum(row["savings"] or 0 for row in chart),
            "actual_cost": sum(row["actual_cost"] or 0 for row in chart),
            "without_battery_cost": sum(row["without_battery_cost"] or 0 for row in chart),
            "coverage_seconds": sum(row["coverage_seconds"] for row in chart),
            "estimated_seconds": 900,
            "expected_seconds": 30 * 86400,
            "chart": chart,
        },
    }


@pytest.mark.parametrize("app_site", ["savings", "activity_ingress"], indirect=True)
def test_savings_a_uses_matched_cash_totals_and_a_custom_calendar(app_page):
    """Desktop/mobile show the same signed results, custom dates and keyboard chart access without simulation controls."""
    page, url = app_page
    data = measured_response()
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    if "hassio_ingress" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-total")).to_contain_text(f"{data['cash']['savings']:.2f}")
    expect(page.locator("#savings-status")).to_contain_text("Partial period")
    expect(page.get_by_text("Evaluation version", exact=True)).to_have_count(0)
    page.locator("#savings-period").get_by_role("button", name="Choose period").click()
    dialog = page.get_by_role("dialog", name="Choose period")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="This month", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Custom period", exact=True).click()
    dialog.get_by_label("From", exact=True).fill("2026-10-01")
    dialog.get_by_label("To", exact=True).fill("2026-10-30")
    with page.expect_request(
        lambda request: "/v1/savings?" in request.url and "2026-10-01" in request.url
    ):
        dialog.get_by_role("button", name="Apply period", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator("#savings-chart")).to_have_attribute("tabindex", "0")
    page.locator("#savings-chart").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#savings-selected")).not_to_be_empty()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("heading", name="Savings", exact=True).click()
    page.evaluate("window.scrollTo(0, 0)")
    directory = Path("results/ci/measured-savings")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(
            directory
            / f"savings-{page.viewport_size['width']}-{'ingress' if 'hassio_ingress' in url else 'standalone'}.png"
        ),
        full_page=True,
    )
    page.locator("#savings-period").get_by_role("button", name="Choose period").click()
    page.screenshot(
        path=str(
            directory
            / f"calendar-{page.viewport_size['width']}-{'ingress' if 'hassio_ingress' in url else 'standalone'}.png"
        ),
        full_page=False,
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_negative_savings_and_failed_refresh_never_show_stale_money(app_page):
    """A negative result is an extra cost, and a failed newer request clears all previous amounts."""
    page, url = app_page
    data = measured_response()
    data["cash"].update(savings=-4.56, actual_cost=14.56, without_battery_cost=10)
    failed = False
    page.route(
        "**/v1/savings?**",
        lambda route: (
            route.fulfill(status=503, body="Unavailable") if failed else route.fulfill(json=data)
        ),
    )
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-outcome")).to_have_text("Extra electricity cost")
    expect(page.locator("#savings-total")).to_contain_text("4.56")
    failed = True
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#savings-total")).to_have_text("Not available")
    expect(page.locator("#savings-actual")).to_have_text("Unavailable")
    expect(page.locator("#savings-status")).to_contain_text("could not be loaded")
    expect(page.locator("#savings-chart")).to_have_attribute("tabindex", "-1")
    assert page.locator("#savings-chart").evaluate("canvas => canvas.onkeydown") is None


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_no_completed_savings_day_disables_selection_until_a_day_completes(app_page):
    """First-day installations show no active date selection, then recover on a later refresh."""
    page, url = app_page
    data = measured_response()
    data["calendar"] = {"has_completed_days": False, "min_day": None, "max_day": None}
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#savings-period .period-trigger")).to_be_disabled()
    expect(page.locator("#savings-period-label")).to_have_text("No completed days")
    data["calendar"] = {
        "has_completed_days": True,
        "min_day": "2026-10-01",
        "max_day": "2026-10-30",
    }
    data["cash"]["aggregation"] = "month"
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#savings-period .period-trigger")).to_be_enabled()
    expect(page.locator("#savings-daily-heading")).to_have_text("Savings by month")
