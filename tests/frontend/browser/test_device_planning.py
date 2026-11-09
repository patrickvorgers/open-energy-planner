"""Verify per-device attribution in all three real Planning charts on desktop and mobile."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
def test_three_planning_charts_show_expected_and_scheduled_devices(app_page):
    """Two acknowledged cycles and four individual expectations retain a conserved full 48-hour horizon."""
    page, url = app_page
    from tests.frontend.browser.documentation_fixture import START

    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    original_rates = page.evaluate(
        """() => Chart.getChart(document.getElementById('forecast-chart')).data.datasets.find(dataset => dataset.label === 'Household demand forecast: AC').data.map(point => point.y)"""
    )
    assert sorted({round(value, 4) for value in original_rates}) == [0.45, 2.2, 2.7]
    for identity in ("plan-chart", "self-consumption-chart", "reference-chart"):
        legend = page.locator("#" + identity + "-legend")
        expect(legend).to_contain_text("Washing machine: Scheduled")
        expect(legend).to_contain_text("Dryer: Scheduled")
        expect(legend).to_contain_text("Dishwasher: Expected")
        expect(legend).to_contain_text("Charge point: Expected")
        data = page.evaluate(
            """id => {
            const chart = Chart.getChart(document.getElementById(id));
            return {labels: chart.data.datasets.map(dataset => dataset.label),
                bars: chart.data.datasets.filter(dataset => dataset.type === 'bar').map(dataset => ({label: dataset.label, values: dataset.data.map(point => point.y)})),
                start: chart.scales.x.min, end: chart.scales.x.max};
        }""",
            identity,
        )
        assert data["end"] - data["start"] == 48 * 3600000
        assert "Household base: AC" in data["labels"]
        assert "Scheduled appliances: AC" not in data["labels"]
        assert len(data["bars"][0]["values"]) >= 192
        for values in zip(*(series["values"] for series in data["bars"]), strict=True):
            assert all(value is not None for value in values)
            assert sum(values) == pytest.approx(0, abs=0.00001)
        scheduled = next(
            series for series in data["bars"] if series["label"] == "Washing machine: Scheduled"
        )
        assert any(value < 0 for value in scheduled["values"])
        assert any(value == 0 for value in scheduled["values"])
        directory = Path("results/ci/device-planning")
        directory.mkdir(parents=True, exist_ok=True)
        capture = page.locator("#" + identity).locator("..").locator("..")
        if page.viewport_size["width"] < 500:
            # A tall narrow viewport captures the full mobile card without a
            # sticky header crossing an offscreen element screenshot.
            page.set_viewport_size({"width": page.viewport_size["width"], "height": 1800})
        capture.screenshot(path=str(directory / f"{identity}-{page.viewport_size['width']}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_corrupt_component_attribution_is_a_gap_instead_of_a_double_count(app_page):
    """A .2-kWh device cannot be overlaid on a household load whose declared base already consumes it."""
    page, url = app_page
    from tests.frontend.browser.helpers import balances, physical_plan

    plan = physical_plan()
    step = {"start": "2026-10-23T00:00:00Z", "duration_h": 0.25, "kwh": 1}
    plan["input_snapshot"]["load_components"] = [
        {"entity_id": None, "steps": [step]},
        {"entity_id": "sensor.washer", "steps": [{**step, "kwh": 0.2}]},
    ]
    result = balances(page, url, plan)[0]
    assert result["missing"] == 3
    assert all(
        interval["value"] is None
        for series in result["series"]
        if series.get("type") == "bar"
        for interval in series["intervals"]
    )
