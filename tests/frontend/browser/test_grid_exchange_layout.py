"""Keep the grid comparison aligned with the other full-width planning charts."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
@pytest.mark.parametrize("renamed", [False, True])
def test_grid_exchange_uses_full_planning_width(app_page, renamed):
    """A 48-hour desktop/mobile comparison matches scenario order with solid, individually toggled lines."""
    page, url = app_page
    page.clock.install(time=START)
    page.goto(url + "/overview")
    headings = ["Optimizer", "Battery self-consumption", "Without battery: Baseline"]
    if renamed:
        headings = ["Smart strategy", "Own solar storage", "Household without storage"]
        for identity, name in zip(
            ["optimizer-heading", "self-consumption-heading", "reference-heading"],
            headings,
            strict=True,
        ):
            page.locator(f"#{identity}").evaluate(
                "(heading, name) => heading.textContent = name", name
            )
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    page.locator('[data-hours="0"]').click()
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
    if not renamed:
        comparison.screenshot(path=f"results/ci/grid-exchange-{page.viewport_size['width']}.png")
    assert page.locator(".scenario-card h3").all_text_contents() == headings
    legend = page.locator("#energy-chart-legend")
    expect(legend.locator("label")).to_have_text(headings)
    rendering = page.locator("#energy-chart").evaluate("""canvas => {
        const chart = Chart.getChart(canvas);
        return chart.data.datasets.map((series, index) => ({
            label: series.label,
            dash: Array.from(chart.getDatasetMeta(index).dataset.options.borderDash || []),
            color: series.borderColor,
        }));
    }""")
    assert [series["label"] for series in rendering] == headings
    assert [series["dash"] for series in rendering] == [[], [], []]
    assert [series["color"] for series in rendering] == ["#078b80", "#5d70bc", "#e49b38"]
    for index, heading in enumerate(headings):
        toggle = legend.get_by_role("checkbox", name=heading, exact=True)
        toggle.uncheck()
        assert page.locator("#energy-chart").evaluate(
            "(canvas, index) => !Chart.getChart(canvas).isDatasetVisible(index)", index
        )
        toggle.check()
        assert page.locator("#energy-chart").evaluate(
            "(canvas, index) => Chart.getChart(canvas).isDatasetVisible(index)", index
        )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_loading_grid_canvas_stays_inside_its_chart_space(app_page):
    """An uninitialized chart cannot cover the evidence control while plan loading waits."""
    page, url = app_page
    held = []
    page.route("**/v1/plans/*/comparison?view=plan", lambda route: held.append(route))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    with page.expect_request("**/v1/plans/*/comparison?view=plan"):
        page.get_by_role("button", name="Connect", exact=True).click()
    canvas = page.locator("#energy-chart")
    assert canvas.evaluate("canvas => !Chart.getChart(canvas)")
    bounds = canvas.bounding_box()
    wrapper = canvas.locator("..").bounding_box()
    assert bounds["y"] >= wrapper["y"]
    assert bounds["y"] + bounds["height"] <= wrapper["y"] + wrapper["height"] + 1
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence-panel")).to_have_attribute("open", "")
    for route in held:
        route.fulfill(status=503, body="Temporary archive read failure")
