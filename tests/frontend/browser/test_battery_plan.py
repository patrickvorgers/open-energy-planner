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
        const optimizerDataset = name => optimizer.data.datasets.find(dataset => dataset.label === name);
        const optimizerColor = name => optimizerDataset(name).backgroundColor;
        const powerError = (label, sign) => Math.max(...optimizerDataset(label).data.map((point, index) =>
            Math.abs(point.y - chart.data.datasets.filter(dataset => dataset.type === 'bar')
                .reduce((total, dataset) => {
                    const power = dataset.data[index].y;
                    return sign * power > 0 ? total + power : total;
                }, 0))));
        return {soc: [chart.scales.soc.min, chart.scales.soc.max],
            range: [chart.scales.x.min, chart.scales.x.max],
            optimizerRange: [optimizer.scales.x.min, optimizer.scales.x.max],
            pvColor: optimizerColor("Used solar: DC"),
            pvAcColor: optimizerColor("Used solar: AC"),
            gridColor: optimizerColor("Grid import: AC"),
            householdBaseColor: optimizerColor("Household demand: AC"),
            chargeError: powerError("Battery charge: DC", -1),
            dischargeError: powerError("Battery discharge: DC", 1),
            series: chart.data.datasets.map(dataset => ({
                name: dataset.label,
                edge: dataset.borderColor, color: dataset.backgroundColor, stack: dataset.stack,
                min: Math.min(...dataset.data.map(point => point.y)),
                max: Math.max(...dataset.data.map(point => point.y)),
                first: dataset.data[0].y,
            }))};
    }""")
    assert state["soc"] == [0, 100]
    assert state["range"] == state["optimizerRange"]
    assert state["chargeError"] < 0.00001
    assert state["dischargeError"] < 0.00001
    assert len(state["series"]) == 16
    for offset, initial_soc in [(0, 40), (8, 62.5)]:
        pv_dc, pv_ac, grid_import, other_charge, own_use, grid_export, other_discharge, soc = state[
            "series"
        ][offset : offset + 8]
        assert all(bar["max"] == 0 for bar in (pv_dc, pv_ac, grid_import, other_charge))
        assert all(bar["min"] == 0 for bar in (own_use, grid_export, other_discharge))
        assert pv_dc["color"] == state["pvColor"]
        assert pv_ac["color"] == state["pvAcColor"]
        assert grid_import["color"] == grid_export["color"] == state["gridColor"]
        assert own_use["color"] != state["householdBaseColor"]
        assert all(
            bar["stack"] == "energy" and bar["edge"] == soc["color"]
            for bar in state["series"][offset : offset + 7]
        )
        assert soc["first"] == initial_soc
    assert state["series"][0]["edge"] != state["series"][8]["edge"]
    for label, bound in [
        ("PV DC", "min"),
        ("PV AC", "min"),
        ("Grid import", "min"),
        ("Own use", "max"),
        ("Grid export", "max"),
    ]:
        assert any(
            abs(series[bound]) > 0.01
            for series in state["series"]
            if series["name"].endswith(f": {label}")
        )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    capture = panel if width < 500 else page.locator(".optimizer-scenario")
    capture.screenshot(path=str(images / f"expanded-{width}.png"))

    canvas.scroll_into_view_if_needed()
    point = canvas.evaluate("""element => {
        const chart = Chart.getChart(element);
        const rect = element.getBoundingClientRect();
        const index = chart.data.datasets[0].data.findIndex(point => point.y < -0.1);
        const bar = chart.getDatasetMeta(0).data[index];
        const pvDcKw = -chart.data.datasets[0].data[index].y;
        return {x: rect.x + bar.x, y: rect.y + (bar.y + bar.base) / 2, index, pvDcKw};
    }""")
    if width < 500:
        page.touchscreen.tap(point["x"], point["y"])
    else:
        page.mouse.move(point["x"], point["y"])
    tooltip = page.locator("#battery-plan-chart-tooltip")
    expect(tooltip).to_contain_text("Example home battery: PV DC")
    expect(tooltip).to_contain_text("Example garage battery: SoC at start")
    source_color = tooltip.get_by_text("Example home battery: PV DC", exact=True).locator(
        ".series-swatch"
    )
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
        export_point = canvas.evaluate("""element => {
            const chart = Chart.getChart(element);
            const dataset = chart.data.datasets.findIndex(series => series.label.endsWith(': Grid export')
                && series.data.some(point => point.y > 0.1));
            const index = chart.data.datasets[dataset].data.findIndex(point => point.y > 0.1);
            const bar = chart.getDatasetMeta(dataset).data[index];
            const rect = element.getBoundingClientRect();
            const stack = chart.getSortedVisibleDatasetMetas()
                .filter(meta => meta.type === 'bar' && chart.data.datasets[meta.index].data[index].y > 0.001)
                .map(meta => ({base: meta.data[index].base, route: meta.label.split(': ').at(-1)}))
                .sort((left, right) => right.base - left.base).map(segment => segment.route);
            return {x: rect.x + bar.x, y: rect.y + (bar.y + bar.base) / 2, stack};
        }""")
        assert export_point["stack"] == ["Own use", "Own use", "Grid export", "Grid export"]
        page.mouse.move(export_point["x"], export_point["y"])
        expect(tooltip).to_contain_text("Grid export")
        expect(tooltip).to_contain_text("Own use")
        tooltip.screenshot(path=str(images / "discharge-destinations.png"))

    garage.uncheck()
    assert (
        page.evaluate("""() => {
        const chart = Chart.getChart('battery-plan-chart');
        return chart.data.datasets.map((dataset, index) => chart.isDatasetVisible(index));
    }""")
        == [True] * 8 + [False] * 8
    )
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
