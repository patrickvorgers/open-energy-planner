"""Verify per-device attribution in all three real Planning charts on desktop and mobile."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize(
    "app_site", ["device_planning", "device_planning_precision"], indirect=True
)
def test_three_planning_charts_show_expected_and_scheduled_devices(app_page, request):
    """Two acknowledged cycles and four individual expectations retain a conserved full 48-hour horizon."""
    page, url = app_page
    from tests.frontend.browser.documentation_fixture import START

    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    wait_for_product_reads(page)
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    original_rates = page.evaluate(
        """() => Chart.getChart(document.getElementById('forecast-chart')).data.datasets.find(dataset => dataset.label === 'Household demand forecast: AC').data.map(point => point.y)"""
    )
    assert sorted({round(value, 4) for value in original_rates}) == [0.45, 2.2, 2.7]
    for identity in ("plan-chart", "self-consumption-chart", "reference-chart"):
        legend = page.locator("#" + identity + "-legend")
        for name in ("Washing machine", "Dryer", "Dishwasher", "Charge point"):
            expect(legend.get_by_role("checkbox", name=name, exact=True)).to_have_count(1)
        expect(legend).to_contain_text(
            "Striped bars show expected use. Solid bars show scheduled use."
        )
        washer = legend.get_by_role("checkbox", name="Washing machine", exact=True)
        expect(washer).to_have_attribute("aria-describedby", identity + "-device-key")
        washer.focus()
        washer.press("Space")
        assert page.evaluate(
            """id => {const chart = Chart.getChart(document.getElementById(id));
                return chart.data.datasets.filter(dataset => dataset.label.startsWith('Washing machine:'))
                    .every(dataset => !chart.isDatasetVisible(chart.data.datasets.indexOf(dataset)));}""",
            identity,
        )
        washer.press("Space")
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
        assert page.evaluate(
            """id => {const chart = Chart.getChart(document.getElementById(id));
                const expected = chart.data.datasets.find(dataset => dataset.label === 'Washing machine: Expected');
                const scheduled = chart.data.datasets.find(dataset => dataset.label === 'Washing machine: Scheduled');
                return expected.backgroundColor instanceof CanvasPattern
                    && typeof scheduled.backgroundColor === 'string'
                    && chart.isDatasetVisible(chart.data.datasets.indexOf(expected))
                    && chart.isDatasetVisible(chart.data.datasets.indexOf(scheduled));}""",
            identity,
        )
        assert len(data["bars"][0]["values"]) >= 192
        for interval_index, values in enumerate(
            zip(*(series["values"] for series in data["bars"]), strict=True)
        ):
            assert all(value is not None for value in values)
            assert sum(values) == pytest.approx(0, abs=0.00001), (
                identity,
                interval_index,
                [(series["label"], value) for series, value in zip(data["bars"], values)],
            )
        scheduled = next(
            series for series in data["bars"] if series["label"] == "Washing machine: Scheduled"
        )
        assert any(value < 0 for value in scheduled["values"])
        assert any(value == 0 for value in scheduled["values"])
        page.locator("#" + identity).scroll_into_view_if_needed()
        hover = page.evaluate(
            """async id => {const chart = Chart.getChart(document.getElementById(id));
                const {when} = await import('./setup/display.js');
                const datasetIndex = chart.data.datasets.findIndex(dataset => dataset.label === 'Washing machine: Scheduled');
                const intervalIndex = chart.data.datasets[datasetIndex].data.findIndex(point => point.y < 0);
                const bar = chart.getDatasetMeta(datasetIndex).data[intervalIndex];
                const bounds = chart.canvas.getBoundingClientRect();
                const pixel = Math.round(bar.x), time = chart.scales.x.getValueForPixel(pixel);
                const quarter = chart.data.datasets[datasetIndex].data.find(point => point.start <= time && time < point.end);
                return {x: bounds.left + pixel, y: bounds.top + (bar.y + bar.base) / 2,
                    title: `From ${when(quarter.start)} to ${when(quarter.end)}`};}""",
            identity,
        )
        page.mouse.move(hover["x"], hover["y"])
        expect(page.locator("#" + identity + "-tooltip .chart-tooltip-title")).to_have_text(
            hover["title"]
        )
        expect(page.locator("#" + identity + "-tooltip")).to_contain_text(
            "Washing machine: Scheduled"
        )
        expect(page.locator("#" + identity + "-tooltip")).to_contain_text(
            "Washing machine: Expected"
        )
        page.mouse.move(0, 0)
        directory = Path(
            "results/ci/device-planning-precision"
            if request.node.callspec.params["app_site"] == "device_planning_precision"
            else "results/ci/device-planning"
        )
        directory.mkdir(parents=True, exist_ok=True)
        capture = page.locator("#" + identity).locator("..").locator("..")
        if page.viewport_size["width"] < 500:
            # A tall narrow viewport captures the full mobile card without a
            # sticky header crossing an offscreen element screenshot.
            page.set_viewport_size({"width": page.viewport_size["width"], "height": 1800})
            capture.evaluate(
                "element => element.scrollIntoView({block: 'center', behavior: 'instant'})"
            )
            assert (
                capture.bounding_box()["y"]
                >= page.locator(".settings-mobilebar").bounding_box()["height"]
            )
        capture.screenshot(path=str(directory / f"{identity}-{page.viewport_size['width']}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_text("Understand this plan", exact=True).click()
    totals = page.locator("#plan-evidence article").filter(
        has_text="Forecast totals: Full planning horizon"
    )
    expect(totals).to_contain_text("Total demand forecast: AC")
    # Two days contain 46.6 kWh. The ready windows replace .9 kWh with 2.7 kWh of cycles.
    expect(totals.locator("dd").first).to_have_text("48.4 kWh")
    breakdown = page.locator("#plan-evidence article").filter(
        has_text="Household forecast: Whole period"
    )
    expect(breakdown).to_contain_text(
        "The original load breakdown records expected and remaining device use. "
        "Scheduled cycles, EV charging and heating come from the plan. "
        "Each contribution is counted once."
    )
    if request.node.callspec.params["app_site"] == "device_planning":
        totals.screenshot(
            path=f"results/ci/device-planning/forecast-total-{page.viewport_size['width']}.png"
        )
        breakdown.screenshot(
            path=f"results/ci/device-planning/demand-breakdown-{page.viewport_size['width']}.png"
        )


def test_corrupt_component_attribution_is_a_gap_instead_of_a_double_count(app_page):
    """Only the first interval double counts .2 kWh. Full coverage preserves the other two independent balances."""
    page, url = app_page
    from tests.frontend.browser.helpers import balances, physical_plan

    plan = physical_plan()
    plan["comparison_names"] = {"sensor.washer": "Washing machine"}
    # Extra first-interval import also balances the corrupt 1.2-kWh breakdown.
    # Only the independent attribution guard can reject that duplicated .2 kWh.
    plan["expected_flows"][0]["grid_import"] = 0.2
    plan["input_snapshot"]["load_components"] = [
        {
            "entity_id": None,
            "steps": [
                {"start": "2026-10-23T00:00:00Z", "duration_h": 0.25, "kwh": 1},
                {"start": "2026-10-23T00:15:00Z", "duration_h": 1, "kwh": 1.5},
                {"start": "2026-10-23T01:15:00Z", "duration_h": 1, "kwh": 0.8},
            ],
        },
        {
            "entity_id": "sensor.washer",
            "steps": [
                {"start": "2026-10-23T00:00:00Z", "duration_h": 0.25, "kwh": 0.2},
                {"start": "2026-10-23T00:15:00Z", "duration_h": 1, "kwh": 0.5},
                {"start": "2026-10-23T01:15:00Z", "duration_h": 1, "kwh": 0.2},
            ],
        },
    ]
    result = balances(page, url, plan)[0]
    assert result["missing"] == 1
    bars = {
        series["name"]: [interval["value"] for interval in series["intervals"]]
        for series in result["series"]
        if series.get("type") == "bar"
    }
    assert all(values[0] is None for values in bars.values())
    expected = {
        "Used solar: DC": [0, 0],
        "Grid import: AC": [0.56, 3],
        "Battery discharge: DC": [2, 0],
        "Household base: AC": [-1.5, -0.8],
        "Grid export: AC": [0, 0],
        "Battery charge: DC": [0, -1.44],
        "Conversion losses": [-0.56, -0.56],
        "Washing machine: Expected": [-0.5, -0.2],
    }
    assert set(bars) == set(expected)
    for name, values in expected.items():
        assert bars[name][1:] == pytest.approx(values)
    assert sum(values[1] for values in bars.values()) == pytest.approx(0, abs=0.00001)
    assert sum(values[2] for values in bars.values()) == pytest.approx(0, abs=0.00001)


def test_real_component_gaps_overlaps_and_invalid_quantities_remain_unavailable(app_page):
    """A 2-ms gap or overlap is real. Invalid duration or energy cannot become a valid zero."""
    page, url = app_page
    page.goto(url + "/overview")
    result = page.evaluate(
        """async () => {
        const {householdForecastEnergy} = await import('./setup/balance.js');
        const interval = {start:'2026-11-04T00:00:00Z', duration_h:.25};
        const original = [{start:interval.start,duration_h:.125,kwh:.1},
            {start:'2026-11-04T00:07:30Z',duration_h:.125,kwh:.1}];
        const samples = [structuredClone(original)];
        for (const change of [
            {start:'2026-11-04T00:07:30.002Z'},
            {start:'2026-11-04T00:07:29.998Z'},
            {duration_h:.125 - 2/3600000},
            {duration_h:0}, {duration_h:-.125}, {duration_h:Infinity},
            {kwh:-.00000001}, {kwh:NaN}, {kwh:Infinity}]) {
            samples.push([original[0],{...original[1],...change}]);
        }
        return samples.map(steps => householdForecastEnergy(
            {input_snapshot:{load_components:[{forecast_steps:steps}]}},interval));
        }"""
    )
    assert result[0] == pytest.approx(0.2)
    assert result[1:] == [None] * 9


@pytest.mark.parametrize("app_site", ["device_planning"], indirect=True)
def test_equal_device_names_keep_independent_legend_switches(app_page):
    """Two HA devices named Laundry keep separate atomic expected/scheduled switches in every balance."""
    from tests.frontend.browser.documentation_fixture import START

    page, url = app_page
    page.clock.set_fixed_time(START)

    def same_friendly_names(route):
        """Change only two display names in the actual solved comparison response."""
        response = route.fetch()
        document = response.json()
        for entity in ("sensor.washer_energy", "sensor.dryer_energy"):
            document["plan"]["comparison_names"][entity] = "Laundry"
        route.fulfill(response=response, json=document)

    page.route("**/v1/plans/*/comparison", same_friendly_names)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    wait_for_product_reads(page)
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    for identity in ("plan-chart", "self-consumption-chart", "reference-chart"):
        switches = page.locator("#" + identity + "-legend").get_by_role(
            "checkbox", name="Laundry", exact=True
        )
        expect(switches).to_have_count(2)
        switches.nth(0).uncheck()
        assert page.evaluate(
            """id => {const chart = Chart.getChart(document.getElementById(id));
                const devices = chart.data.datasets.filter(dataset => dataset.label.startsWith('Laundry:'));
                return devices.length === 4 && devices.filter(dataset => chart.isDatasetVisible(chart.data.datasets.indexOf(dataset))).length === 2;}""",
            identity,
        )
        switches.nth(1).uncheck()
        assert page.evaluate(
            """id => {const chart = Chart.getChart(document.getElementById(id));
                return chart.data.datasets.filter(dataset => dataset.label.startsWith('Laundry:'))
                    .every(dataset => !chart.isDatasetVisible(chart.data.datasets.indexOf(dataset)));}""",
            identity,
        )
