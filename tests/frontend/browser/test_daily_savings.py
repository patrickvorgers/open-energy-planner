"""Render daily settlement contracts with independent coverage, responsive details and bounded lists."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, open_primary_navigation


def daily_response():
    """Specify a synthetic month independently. Partial benefit compares matching quarters, not unmatched totals."""
    first = datetime(2026, 10, 1, tzinfo=UTC)
    days = []
    for index in range(30):
        start, end = first + timedelta(days=index), first + timedelta(days=index + 1)
        partial, unavailable = index == 3, index == 5
        multiplier = 1 + (index % 7) / 10
        scenarios = {}
        for kind, base, imported in (
            ("actual", 2, 10),
            ("reference", 3, 15),
            ("without_battery", 4, 20),
        ):
            fraction = (0.25 if kind == "reference" else 0.5) if partial else 1
            absent = unavailable and kind == "reference"
            amount = base * multiplier * fraction
            totals = (
                None
                if absent
                else {
                    "purchase": amount,
                    "sale": 0,
                    "bonus": 0,
                    "grid_cash": amount,
                    "wear": 0,
                    "inventory_change": 0,
                    "economic_cost": amount,
                    "import_kwh": imported * multiplier * fraction,
                    "export_kwh": 0,
                }
            )
            covered = 0 if absent else 86400 * fraction
            scenarios[kind] = {
                "totals": totals,
                "coverage_seconds": covered,
                "estimated_seconds": 7200 if index == 2 else 0,
                "status": "unavailable" if absent else "partial" if partial else "complete",
                "opening": None
                if absent
                else {"at": start.isoformat(), "value": 0, "stored_kwh": {}},
                "closing": None
                if absent
                else {"at": end.isoformat(), "value": 0, "stored_kwh": {}},
                "missing": [
                    {
                        "start": (start + timedelta(seconds=covered)).isoformat(),
                        "end": end.isoformat(),
                        "reason": "Required measurements are missing. Missing energy is not treated as zero.",
                    }
                ]
                if covered < 86400
                else [],
            }
        days.append(
            {
                "day": start.date().isoformat(),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "expected_seconds": 86400,
                "scenarios": scenarios,
                "currency": "EUR",
                "policy": "daily-savings-v1",
                "evaluated_at": end.isoformat(),
                "finalized_at": None if index >= 27 else (end + timedelta(days=3)).isoformat(),
                "finalizes_after": (end + timedelta(days=3)).isoformat(),
                "day_plan_id": None,
                "savings": None if unavailable else multiplier * (0.25 if partial else 1),
                "battery_savings": multiplier * (1 if partial else 2),
            }
        )
    totals = {
        kind: {
            key: sum(
                day["scenarios"][kind]["totals"][key]
                for day in days
                if day["scenarios"][kind]["totals"] is not None
            )
            for key in days[0]["scenarios"][kind]["totals"]
        }
        for kind in scenarios
    }
    return {
        "daily": True,
        "start": first.isoformat(),
        "end": (first + timedelta(days=30)).isoformat(),
        "days": days,
        "day_count": 30,
        "currency": "EUR",
        "details_truncated": False,
        "issue": None,
        "savings": sum(day["savings"] for day in days if day["savings"] is not None),
        "battery_savings": sum(day["battery_savings"] for day in days),
        "totals": totals,
        "coverage": {
            kind: {
                "seconds": sum(day["scenarios"][kind]["coverage_seconds"] for day in days),
                "expected_seconds": 30 * 86400,
            }
            for kind in scenarios
        },
        "chart": [
            {
                "start": day["start"],
                "end": day["end"],
                "savings": day["savings"],
                **{
                    f"{kind}_import_kwh": day["scenarios"][kind]["totals"]["import_kwh"]
                    if day["scenarios"][kind]["totals"]
                    else None
                    for kind in scenarios
                },
            }
            for day in days
        ],
    }


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_daily_results_show_partial_and_unavailable_scenarios_without_false_zero(app_page):
    """Desktop/mobile render three independent costs, 25-row pages and explicit missing ranges. API arithmetic has separate real-backend proof."""
    page, url = app_page
    data = daily_response()
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    open_primary_navigation(page)
    page.get_by_role("link", name="Savings", exact=True).click()
    page.locator("#period-start").fill("2026-10-01")
    page.locator("#period-end").fill("2026-10-30")
    page.get_by_role("button", name="Show period").click()
    expect(page.locator("#daily-savings-details")).to_be_visible()
    expect(page.locator("#daily-savings-rows tr")).to_have_count(25)
    expect(page.locator("#daily-savings-page")).to_have_text("Page 1 of 2")
    expect(page.locator("#daily-savings-rows tr").nth(3)).to_contain_text("50.0%")
    expect(page.locator("#daily-savings-rows tr").nth(3)).to_contain_text("25.0%")
    expect(page.locator("#daily-savings-rows tr").nth(5)).to_contain_text("Unavailable")
    expect(page.locator("#daily-savings-rows tr").nth(3)).to_contain_text("Finalized: Partial")
    detail = page.locator("#daily-savings-rows tr").nth(3).locator("details")
    detail.locator("summary").click()
    expect(detail).to_contain_text("Missing energy is not treated as zero")
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#daily-savings-rows tr")).to_have_count(5)
    expect(page.locator("#daily-savings-page")).to_have_text("Page 2 of 2")
    expect(page.locator("#daily-savings-next")).to_be_disabled()
    expect(page.locator("#daily-savings-rows")).to_contain_text("Provisional: Complete")
    last_details = page.locator("#daily-savings-rows tr").first.locator("details")
    last_details.locator("summary").click()
    data["days"][25]["savings"] = 42
    with page.expect_response("**/v1/savings?**"):
        page.locator("#period").evaluate("form => form.requestSubmit()")
    expect(page.locator("#daily-savings-rows tr").first).to_contain_text("42.00")
    expect(page.locator("#daily-savings-page")).to_have_text("Page 2 of 2")
    expect(last_details).to_have_attribute("open", "")
    expect(last_details.locator("summary")).to_be_focused()
    data["days"][25]["savings"] = 1.4
    with page.expect_response("**/v1/savings?**"):
        page.locator("#period").evaluate("form => form.requestSubmit()")
    expect(page.locator("#daily-savings-rows tr").first).to_contain_text("1.40")
    assert page.evaluate(
        "Chart.getChart(document.getElementById('realization-chart')).data.datasets.map(dataset => dataset.label)"
    ) == ["Actual", "Battery self-consumption", "Without battery"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    folder = Path("results/ci/compact-savings-ui")
    folder.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    last_details.locator("summary").click()
    page.locator("#daily-savings-details .table-scroll").evaluate(
        "element => element.scrollLeft = 0"
    )
    page.locator("#daily-savings-details").screenshot(
        path=str(folder / f"daily-results-{width}.png")
    )
    page.get_by_role("button", name="Previous", exact=True).click()
    page.locator("#savings-heading").evaluate(
        "element => window.scrollTo(0, element.getBoundingClientRect().top + window.scrollY - 90)"
    )
    page.screenshot(path=str(folder / f"daily-savings-{width}.png"))
    page.locator("#savings-chart").locator("xpath=..").screenshot(
        path=str(folder / f"daily-chart-{width}.png")
    )
    labels = page.evaluate(
        "Chart.getChart(document.getElementById('savings-chart')).scales.x.ticks.map(tick => tick.label)"
    )
    assert labels and all("2026" in label for label in labels)
    for identity in ("savings-chart", "realization-chart"):
        expect(page.locator("#" + identity + "-tooltip")).to_have_count(1)
        assert (
            page.locator("#" + identity).evaluate(
                "canvas => canvas.closest('figure').querySelectorAll('.chart-day-headings').length"
            )
            == 0
        )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_timezone_pause_and_recovery_keep_the_selected_daily_results(app_page):
    """Desktop/mobile show the owned recovery action and clear it after a successful refresh without losing costs."""
    page, url = app_page
    data = daily_response()
    data["issue"] = (
        "Historical savings are paused because the Home Assistant timezone changed. Restore the original Home Assistant timezone to resume. Existing daily results are retained."
    )
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=data))
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#coverage")).to_contain_text(
        "Restore the original Home Assistant timezone to resume"
    )
    expect(page.locator("#savings-total")).to_contain_text("36.03")
    expect(page.locator("#daily-savings-rows tr")).to_have_count(25)
    data["issue"] = None
    page.get_by_role("button", name="Show period").click()
    expect(page.locator("#coverage")).not_to_contain_text(
        "Restore the original Home Assistant timezone"
    )
    expect(page.locator("#savings-total")).to_contain_text("36.03")
    expect(page.locator("#daily-savings-rows tr")).to_have_count(25)
