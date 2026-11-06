"""Keep the grid comparison aligned with the other full-width planning charts."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_grid_exchange_uses_full_planning_width(app_page):
    """Render a real 48-hour plan at desktop/mobile sizes without horizontal overflow."""
    page, url = app_page
    page.clock.install(time=START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    page.get_by_role("button", name="Full horizon", exact=True).click()
    forecast = page.locator("figure:has(#forecast-chart)").bounding_box()
    comparison = page.locator("figure:has(#energy-chart)")
    bounds = comparison.bounding_box()
    assert bounds["x"] == pytest.approx(forecast["x"], abs=1)
    assert bounds["width"] == pytest.approx(forecast["width"], abs=1)
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    assert page.locator("#energy-chart").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.some(series => "
        "series.data.some(point => point.y > 0) && series.data.some(point => point.y < 0))"
    )
    page.evaluate("document.fonts.ready")
    comparison.screenshot(path=f"results/ci/grid-exchange-{page.viewport_size['width']}.png")
