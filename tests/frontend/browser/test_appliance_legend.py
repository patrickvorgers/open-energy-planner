"""Keep Planning legends limited to appliances contributing within the selected window."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["device_planning_legend"], indirect=True)
def test_appliance_legends_follow_window_without_losing_visibility_choices(app_page):
    """Sub-100-W use is omitted, exactly 100 W follows the window, and choices remain recoverable."""
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
        expect(legend.locator("label").filter(has_text="Dishwasher")).not_to_be_visible()
        assert page.evaluate(
            """id => {const chart = Chart.getChart(document.getElementById(id));
                return chart.data.datasets.filter(dataset => /^(Dishwasher|Unused appliance):/.test(dataset.label))
                    .every(dataset => !chart.isDatasetVisible(chart.data.datasets.indexOf(dataset)));}""",
            identity,
        )
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
            if hours == 6:
                expect(legend.locator("label").filter(has_text="Dryer")).not_to_be_visible()
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
        expect(legend.get_by_role("checkbox", name="Later appliance", exact=True)).to_be_visible()
        expect(page.locator(f"#{identity}-device-key")).to_be_visible()
        expect(legend.get_by_role("checkbox", name="Used solar: DC", exact=True)).to_be_visible()


@pytest.mark.parametrize("app_site", ["device_planning_legend"], indirect=True)
def test_bulk_legend_selection_is_local_and_keeps_rounding_filter(app_page):
    """Keyboard bulk actions toggle eligible groups atomically without changing other charts or data."""
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
        button = legend.get_by_role("button", name="Deselect all", exact=True)
        expect(button).to_have_attribute("aria-controls", identity)
        button.focus()
        button.press("Enter")
        expect(legend.get_by_role("button", name="Select all", exact=True)).to_be_focused()
        assert page.evaluate(
            """id => {const chart = Chart.getChart(document.getElementById(id));
                return chart.data.datasets.every((_dataset, index) => !chart.isDatasetVisible(index));}""",
            identity,
        )
        for other in identities:
            if other != identity:
                expect(
                    page.locator(f"#{other}-legend").get_by_role(
                        "checkbox", name="Charge point", exact=True
                    )
                ).to_be_checked()
        legend.get_by_role("button", name="Select all", exact=True).press("Space")
        expect(legend.get_by_role("button", name="Deselect all", exact=True)).to_be_visible()
        legend.get_by_role("checkbox", name="Washing machine", exact=True).uncheck()
        legend.get_by_role("button", name="Select all", exact=True).click()
        expect(legend.get_by_role("checkbox", name="Washing machine", exact=True)).to_be_checked()
        assert page.evaluate(
            """id => {const chart = Chart.getChart(document.getElementById(id));
                return chart.data.datasets.filter(dataset => dataset.label.startsWith('Washing machine:'))
                    .every(dataset => chart.isDatasetVisible(chart.data.datasets.indexOf(dataset)))
                    && chart.data.datasets.filter(dataset => dataset.label.startsWith('Dishwasher:'))
                    .every(dataset => !chart.isDatasetVisible(chart.data.datasets.indexOf(dataset)));}""",
            identity,
        )
    assert (
        page.evaluate(
            """ids => ids.map(id => Chart.getChart(document.getElementById(id)).data.datasets.map(dataset => dataset.data))""",
            identities,
        )
        == original
    )


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
def test_rounding_uses_combined_displayed_bar_and_preserves_unknowns(app_page):
    """Renderer boundaries use combined quarter powers, including scheduled use and unavailable bars."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    wait_for_product_reads(page)
    legend = page.locator("#plan-chart-legend")
    for expected, scheduled, visible in (
        (-0.025, -0.025, False),
        (-0.049, -0.05, False),
        (-0.05, -0.05, True),
        (0, -0.1, True),
        (-0.101, 0, True),
        (None, 0, True),
    ):
        result = page.evaluate(
            """([expected, scheduled]) => {
                const chart = Chart.getChart(document.getElementById('plan-chart'));
                const devices = chart.data.datasets.filter(dataset => dataset.label.startsWith('Washing machine:'));
                for (const dataset of devices) for (const point of dataset.data) point.y = 0;
                devices.find(dataset => dataset.deviceState === 'expected').data[0].y = expected;
                devices.find(dataset => dataset.deviceState === 'scheduled').data[0].y = scheduled;
                chart.update();
                return devices.map(dataset => chart.isDatasetVisible(chart.data.datasets.indexOf(dataset)));
            }""",
            [expected, scheduled],
        )
        assert result == [visible, visible]
        label = legend.locator("label").filter(has_text="Washing machine")
        if visible:
            expect(label).to_be_visible()
        else:
            expect(label).not_to_be_visible()
