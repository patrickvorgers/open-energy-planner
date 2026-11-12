"""Full-horizon synthetic plans exercise shared legend layout without changing their data."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads

WEB = Path(__file__).resolve().parents[3] / "src/open_energy_planner/web/public"
LONG_NAME = "Synthetic upstairs heat pump with an unusually long descriptive meter name"


@pytest.mark.parametrize("app_site", ["device_planning_legend"], indirect=True)
@pytest.mark.parametrize(
    "app_page",
    [{"width": width, "height": 1800} for width in (1600, 1280, 768, 390)],
    indirect=True,
)
def test_shared_legends_use_full_plot_width_and_keep_controls(app_page):
    """Real 48-hour charts retain data, complete labels and keyboard controls on four widths."""
    page, url = app_page
    page.clock.set_fixed_time(START)

    def name_device(route):
        """Vary only a display name in the real projection, retaining all stored numerical inputs."""
        document = route.fetch().json()
        document["plan"]["comparison_names"]["sensor.charger_energy"] = LONG_NAME
        route.fulfill(json=document)

    page.route("**/comparison?view=plan", name_device)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    for identity in (
        "plan-chart",
        "self-consumption-chart",
        "reference-chart",
        "price-chart",
        "forecast-chart",
        "energy-chart",
    ):
        canvas = page.locator("#" + identity)
        legend = page.locator("#" + identity + "-legend")
        plot = canvas.locator("..").bounding_box()
        bounds = legend.bounding_box()
        assert bounds["y"] >= plot["y"] + plot["height"] - 1
        assert bounds["x"] == pytest.approx(plot["x"], abs=1)
        assert bounds["width"] == pytest.approx(plot["width"], abs=1)
        for label in legend.locator("label:visible").all():
            label_bounds = label.bounding_box()
            assert label_bounds["x"] >= bounds["x"] - 1
            assert label_bounds["x"] + label_bounds["width"] <= bounds["x"] + bounds["width"] + 1
        original = canvas.evaluate(
            "canvas => JSON.stringify(Chart.getChart(canvas).data.datasets.map(dataset=>dataset.data))"
        )
        checkbox = legend.get_by_role("checkbox").first
        checkbox.focus()
        page.keyboard.press("Space")
        expect(checkbox).not_to_be_checked()
        page.keyboard.press("Space")
        expect(checkbox).to_be_checked()
        assert (
            canvas.evaluate(
                "canvas => JSON.stringify(Chart.getChart(canvas).data.datasets.map(dataset=>dataset.data))"
            )
            == original
        )
    legend = page.locator("#plan-chart-legend")
    expect(legend.get_by_role("checkbox", name=LONG_NAME, exact=True)).to_be_visible()
    expect(legend.get_by_role("checkbox", name="Unused appliance", exact=True)).not_to_be_visible()
    if page.viewport_size["width"] > 640:
        assert (
            legend.evaluate(
                "legend => new Set([...legend.querySelectorAll('label:not([hidden])')].map(label=>Math.round(label.getBoundingClientRect().left))).size"
            )
            > 1
        )
    bulk = legend.get_by_role("button", name="Deselect all", exact=True)
    bulk.focus()
    page.keyboard.press("Space")
    expect(legend.get_by_role("button", name="Select all", exact=True)).to_be_focused()
    page.keyboard.press("Space")
    expect(legend.get_by_role("checkbox", name="Grid import: AC", exact=True)).to_be_checked()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    directory = Path("results/ci/chart-legend-columns")
    directory.mkdir(parents=True, exist_ok=True)
    page.locator(".optimizer-scenario").screenshot(
        path=str(directory / f"planning-{page.viewport_size['width']}.png")
    )


@pytest.mark.parametrize("width", [1280, 390])
def test_native_legends_stay_below_for_existing_new_and_resized_charts(browser, width):
    """Use the shipped renderer with synthetic 24-hour series and native legend hit targets."""
    with browser.new_context(viewport={"width": width, "height": 800}) as context:
        page = context.new_page()
        page.set_content('<div style="height:300px"><canvas id="plot"></canvas></div>')
        page.add_script_tag(path=str(WEB / "chart.umd.min.js"))
        page.evaluate("""() => {
            window.chart=new Chart(document.getElementById('plot'),{type:'bar',
                data:{labels:Array.from({length:24},(_,hour)=>hour),datasets:[
                    {label:'Measured energy',data:Array.from({length:24},(_,hour)=>hour%4)},
                    {label:'Expected energy',data:Array.from({length:24},(_,hour)=>1+hour%3)}]},
                options:{animation:false,responsive:true,maintainAspectRatio:false}});
        }""")
        page.add_script_tag(path=str(WEB / "chart_theme.js"), type="module")
        page.wait_for_function("Chart.registry.plugins.get('valueAxisHeadroom')")
        assert page.evaluate("Chart.defaults.plugins.legend.position") == "bottom"
        original = page.evaluate(
            "JSON.stringify(window.chart.data.datasets.map(dataset=>dataset.data))"
        )
        for viewport_width in (width, 390 if width > 640 else 1280, width):
            page.set_viewport_size({"width": viewport_width, "height": 800})
            page.wait_for_function(
                "window.chart.width===document.getElementById('plot').parentElement.clientWidth"
            )
            assert page.evaluate("window.chart.legend.position") == "bottom"
            assert page.evaluate("window.chart.legend.top >= window.chart.chartArea.bottom")
            assert (
                page.evaluate(
                    "JSON.stringify(window.chart.data.datasets.map(dataset=>dataset.data))"
                )
                == original
            )
        bounds = page.locator("#plot").bounding_box()
        target = page.evaluate("window.chart.legend.legendHitBoxes[0]")
        page.mouse.click(
            bounds["x"] + target["left"] + target["width"] / 2,
            bounds["y"] + target["top"] + target["height"] / 2,
        )
        page.wait_for_function("!window.chart.isDatasetVisible(0)")
        page.evaluate(
            "window.chart.destroy();window.chart=new Chart(document.getElementById('plot'),{type:'line',data:{labels:[0,1],datasets:[{label:'New forecast',data:[1,2]}]},options:{animation:false}})"
        )
        assert page.evaluate("window.chart.legend.position") == "bottom"
