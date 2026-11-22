"""Verify the optional Optimizer chart using a validated two-battery plan."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", ["battery_planning"], indirect=True)
@pytest.mark.parametrize(
    "app_page",
    [{"width": width, "height": 900} for width in (1440, 1024, 768, 390)],
    indirect=True,
)
def test_optimizer_battery_plan_shares_the_window_and_groups_each_battery(app_page):
    """A real two-battery plan opens on demand with signed power and independently stored SoC."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")

    panel = page.locator(".optimizer-scenario #battery-plan-panel")
    summary = panel.locator("summary")
    expect(summary).to_have_text("Battery plan")
    expect(summary).to_be_visible()
    canvas = panel.get_by_role("img", name="Planned power and state of charge by battery")
    expect(canvas).to_be_hidden()
    assert not page.evaluate("Boolean(Chart.getChart('battery-plan-chart'))")
    images = Path("results/ci/battery-plan")
    images.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    if width == 1440:
        page.locator(".optimizer-scenario").screenshot(path=str(images / "collapsed-desktop.png"))
    if width >= 500:
        summary.focus()
        summary.press("Enter")
    else:
        summary.tap()

    expect(canvas).to_be_visible()
    garage = panel.get_by_role("checkbox", name="Example garage battery")
    home = panel.get_by_role("checkbox", name="Example home battery")
    expect(garage).to_be_checked()
    expect(home).to_be_checked()
    state = page.evaluate("""() => {
        const chart = Chart.getChart('battery-plan-chart');
        const optimizer = Chart.getChart('plan-chart');
        const optimizerColor = name => optimizer.data.datasets.find(dataset => dataset.label === name).backgroundColor;
        return {soc: [chart.scales.soc.min, chart.scales.soc.max],
            range: [chart.scales.x.min, chart.scales.x.max],
            optimizerRange: [optimizer.scales.x.min, optimizer.scales.x.max],
            chargeColor: optimizerColor("Battery charge: DC"),
            dischargeColor: optimizerColor("Battery discharge: DC"),
            pvColor: optimizerColor("Used solar: DC"),
            series: chart.data.datasets.map(dataset => ({
                edge: dataset.borderColor, color: dataset.backgroundColor, stack: dataset.stack,
                min: Math.min(...dataset.data.map(point => point.y)),
                max: Math.max(...dataset.data.map(point => point.y)),
                first: dataset.data[0].y,
            }))};
    }""")
    assert state["soc"] == [0, 100]
    assert state["range"] == state["optimizerRange"]
    assert len(state["series"]) == 6
    for offset, initial_soc in [(0, 40), (3, 62.5)]:
        charge, discharge, soc = state["series"][offset : offset + 3]
        assert charge["min"] < 0 and charge["max"] == 0
        assert discharge["min"] == 0 and discharge["max"] > 0
        assert charge["stack"] == discharge["stack"] == "energy"
        assert charge["color"] == state["chargeColor"]
        assert discharge["color"] == state["dischargeColor"]
        assert charge["edge"] == discharge["edge"] == soc["color"]
        assert soc["first"] == initial_soc
    assert state["series"][0]["edge"] != state["series"][3]["edge"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    capture = panel if width < 500 else page.locator(".optimizer-scenario")
    capture.screenshot(path=str(images / f"expanded-{width}.png"))

    canvas.scroll_into_view_if_needed()
    point = canvas.evaluate("""element => {
        const chart = Chart.getChart(element);
        const rect = element.getBoundingClientRect();
        const index = chart.data.datasets[0].data.findIndex(point => point.chargingSources?.pv_dc > 0.1);
        const bar = chart.getDatasetMeta(0).data[index];
        const pvDcKw = chart.data.datasets[0].data[index].chargingSources.pv_dc;
        return {x: rect.x + bar.x, y: rect.y + (bar.y + bar.base) / 2, index, pvDcKw};
    }""")
    if width < 500:
        page.touchscreen.tap(point["x"], point["y"])
    else:
        page.mouse.move(point["x"], point["y"])
    tooltip = page.locator("#battery-plan-chart-tooltip")
    expect(tooltip).to_contain_text("Example home battery: Charging")
    expect(tooltip).to_contain_text("Example garage battery: SoC at start")
    expect(tooltip).to_contain_text("PV DC:")
    source_color = tooltip.get_by_text("PV DC:", exact=False).first.locator(".series-swatch")
    assert source_color.evaluate(
        """(element, color) => {
            const comparison = document.createElement('span');
            comparison.style.backgroundColor = color;
            return comparison.style.backgroundColor === element.style.backgroundColor;
        }""",
        state["pvColor"],
    )
    if width == 1440:
        tooltip.screenshot(path=str(images / "charging-sources.png"))

    garage.uncheck()
    assert page.evaluate("""() => {
        const chart = Chart.getChart('battery-plan-chart');
        return chart.data.datasets.map((dataset, index) => chart.isDatasetVisible(index));
    }""") == [True, True, True, False, False, False]
    page.get_by_role("button", name="Next 6 hours").click()
    assert (
        page.evaluate("""() => {
        const {min, max} = Chart.getChart('battery-plan-chart').scales.x;
        return max - min;
    }""")
        == 6 * 60 * 60 * 1000
    )
    summary.click()
    expect(canvas).to_be_hidden()
    summary.click()
    expect(garage).not_to_be_checked()

    if width == 1440:
        evidence = page.locator("#plan-evidence-panel > summary")
        evidence.press("Enter")
        page.locator("#interval-evidence-panel > summary").press("Enter")
        inspector = page.get_by_role("slider", name="Inspect planning time")
        inspector.press("Home")
        # The captured PV example is within the first day of original quarter hours.
        for _ in range(point["index"]):
            inspector.press("ArrowRight")
        page.get_by_text("Energy balance and source details", exact=True).press("Enter")
        sources = page.locator("#inspect-values article").filter(
            has=page.get_by_role("heading", name="Example home battery: Charging sources")
        )
        expect(sources.get_by_text("PV DC", exact=True)).to_be_visible()
        amount = sources.locator("dd").first
        expect(amount).to_contain_text(" kWh")
        energy_kwh = float(amount.inner_text().removesuffix(" kWh"))
        assert energy_kwh == pytest.approx(point["pvDcKw"] * 0.25, abs=0.0005)
        sources.screenshot(path=str(images / "charging-sources-inspector.png"))
        inspector.press("Home")
        expect(sources.get_by_text("PV DC", exact=True)).to_have_count(0)
