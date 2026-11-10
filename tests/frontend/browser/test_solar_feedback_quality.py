"""Show retained partial solar evidence on responsive charts without granting a complete-day score."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("app_site", ["solar_feedback_quality"], indirect=True)
def test_partial_solar_plan_accuracy_and_daily_chart_remain_distinct(app_page):
    """Real HTTP preserves 17 paired hours while desktop/mobile show the full-day curves and explicit daily exclusion."""
    page, url = app_page
    discover(page, url, timezone="UTC")
    response = page.request.get(
        url + "/v1/forecast-quality/synthetic-feedback-plan",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.ok
    plan = response.json()["series"][0]
    assert plan["coverage"]["paired_hours"] == 17
    assert plan["metrics"]["mae_kwh"] == pytest.approx(0.07)
    assert plan["alternatives"][0]["metrics"]["mae_kwh"] == pytest.approx(0.035)
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.evaluate("window.__oepProductReadsIdle()")
    page.get_by_label("Day", exact=True).select_option("2026-06-15")
    page.evaluate("window.__oepProductReadsIdle()")
    page.get_by_role("button", name="Solar", exact=True).click()
    source = page.locator("#quality-charts article").filter(has_text="Synthetic south roof")
    expect(source).to_contain_text(
        "Curtailment feedback does not establish unrestricted production for this hour"
    )
    expect(source.get_by_role("checkbox", name="Machine learning", exact=True)).to_be_checked()
    lengths = source.locator("canvas").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.map(row => row.data.length)"
    )
    assert lengths == [24, 24, 24, 24, 24]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    source.get_by_text("Hourly evidence and provenance", exact=True).click()
    expect(source.locator("details table")).to_contain_text("Curtailment feedback")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/solar-feedback-partial-{page.viewport_size['width']}.png", full_page=True
    )
