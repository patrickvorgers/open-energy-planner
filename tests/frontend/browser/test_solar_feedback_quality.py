"""Show retained partial solar evidence on responsive charts without granting a complete-day score."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize(
    "app_site", ["solar_feedback_quality", "solar_feedback_quality_hybrid"], indirect=True
)
def test_partial_solar_plan_accuracy_and_daily_chart_remain_distinct(app_page, request):
    """Pure hybrid PV scores a complete day. Actual feedback excludes only affected standalone hours."""
    page, url = app_page
    hybrid = request.node.callspec.params["app_site"] == "solar_feedback_quality_hybrid"
    discover(page, url, timezone="UTC")
    response = page.request.get(
        url + "/v1/forecast-quality/synthetic-feedback-plan",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.ok
    plan = response.json()["series"][0]
    assert plan["coverage"]["paired_hours"] == (24 if hybrid else 17)
    assert plan["metrics"]["mae_kwh"] == pytest.approx(1.93 / 24 if hybrid else 0.07)
    assert plan["alternatives"][0]["metrics"]["mae_kwh"] == pytest.approx(
        0.965 / 24 if hybrid else 0.035
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.evaluate("window.__oepProductReadsIdle()")
    page.get_by_label("Day", exact=True).select_option("2026-06-15")
    page.evaluate("window.__oepProductReadsIdle()")
    page.get_by_role("button", name="Solar", exact=True).click()
    source = page.locator("#quality-charts article").filter(has_text="Synthetic south roof")
    if hybrid:
        expect(source).not_to_contain_text("not independently comparable")
        expect(source).not_to_contain_text("Not scored yet")
        expect(source).to_contain_text("Comparable pairs: 24")
    else:
        expect(source).to_contain_text(
            "Curtailment feedback does not establish unrestricted production for this hour"
        )
    expect(source.get_by_role("checkbox", name="Machine learning", exact=True)).to_be_checked()
    lengths = source.locator("canvas").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.map(row => row.data.length)"
    )
    assert lengths == [24, 24, 24, 24, 24]
    measured = source.locator("canvas").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.find(row => row.label === 'Measured').data.map(point => point.y)"
    )
    assert measured == pytest.approx(
        [
            0,
            0,
            0,
            0,
            0,
            0,
            0.1,
            0.25,
            0.5,
            0.8,
            1.1,
            1.4,
            1.5,
            1.4,
            1.1,
            0.8,
            0.5,
            0.2,
            0,
            0,
            0,
            0,
            0,
            0,
        ]
    )
    if not hybrid:
        expect(source.get_by_text("Not scored yet", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    source.get_by_text("Hourly evidence and provenance", exact=True).click()
    if not hybrid:
        expect(source.locator("details table")).to_contain_text("Curtailment feedback")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/solar-feedback-partial-{request.node.callspec.params['app_site']}-{page.viewport_size['width']}.png",
        full_page=True,
    )
