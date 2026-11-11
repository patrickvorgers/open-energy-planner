"""Keep Planning legends limited to appliances contributing within the selected window."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["device_planning_legend"], indirect=True)
def test_appliance_legends_follow_window_without_losing_visibility_choices(app_page):
    """Zero use is omitted, future expected use follows the window, and hidden series remain recoverable."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    wait_for_product_reads(page)
    identities = ("plan-chart", "self-consumption-chart", "reference-chart")
    original = page.evaluate(
        """ids => ids.map(id => Chart.getChart(document.getElementById(id)).data.datasets.map(dataset => dataset.data))""",
        identities,
    )
    for identity in identities:
        legend = page.locator(f"#{identity}-legend")
        expect(legend.locator("label").filter(has_text="Unused appliance")).not_to_be_visible()
        expect(legend.get_by_role("checkbox", name="Charge point", exact=True)).to_be_visible()
        later = legend.get_by_role("checkbox", name="Later appliance", exact=True)
        expect(later).to_be_visible()
        later.focus()
        later.press("Space")
        expect(later).not_to_be_checked()
        expect(later).to_be_visible()
    for hours in (6, 24):
        page.get_by_role("button", name=f"Next {hours} hours", exact=True).click()
        for identity in identities:
            legend = page.locator(f"#{identity}-legend")
            expect(legend.locator("label").filter(has_text="Later appliance")).not_to_be_visible()
            expect(legend.get_by_role("checkbox", name="Charge point", exact=True)).to_be_visible()
    page.get_by_role("button", name=re.compile(r"^Full period:")).click()
    for identity in identities:
        later = page.locator(f"#{identity}-legend").get_by_role(
            "checkbox", name="Later appliance", exact=True
        )
        expect(later).to_be_visible()
        expect(later).not_to_be_checked()
        later.focus()
        later.press("Space")
    assert (
        page.evaluate(
            """ids => ids.map(id => Chart.getChart(document.getElementById(id)).data.datasets.map(dataset => dataset.data))""",
            identities,
        )
        == original
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    directory = Path("results/ci/appliance-legend")
    directory.mkdir(parents=True, exist_ok=True)
    if page.viewport_size["width"] < 500:
        page.set_viewport_size({"width": page.viewport_size["width"], "height": 1700})
    card = page.locator("#plan-chart").locator("..").locator("..")
    card.scroll_into_view_if_needed()
    card.screenshot(path=str(directory / f"plan-legend-{page.viewport_size['width']}.png"))
    for identity in identities:
        page.evaluate(
            """id => {
            const chart = Chart.getChart(document.getElementById(id));
            for (const dataset of chart.data.datasets.filter(dataset => dataset.deviceState)) {
                for (const point of dataset.data) point.y = null;
            }
            chart.update();
        }""",
            identity,
        )
        legend = page.locator(f"#{identity}-legend")
        expect(legend.locator("label").filter(has_text="Later appliance")).not_to_be_visible()
        expect(page.locator(f"#{identity}-device-key")).not_to_be_visible()
        expect(legend.get_by_role("checkbox", name="Used solar: DC", exact=True)).to_be_visible()
