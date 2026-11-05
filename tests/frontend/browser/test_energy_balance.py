"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    MOBILE_CHART_LEGEND_BREAKPOINT_PX,
    TOKEN,
    balances,
    physical_plan,
)


@pytest.mark.parametrize(
    "app_site,correction",
    [
        ("savings", ""),
        (
            "savings_corrected",
            (
                "Contract updated. Self-consumption costs were repriced without re-optimization. "
                "See the separate cost table. "
            ),
        ),
    ],
    indirect=["app_site"],
)
def test_reference_balances_and_original_price_inputs_cover_the_complete_plan(app_page, correction):
    """Desktop/mobile lead with the optimizer, retain both references and keep .1/.3/0 original price inputs."""
    page, url = app_page
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#reference-scenario")).to_have_count(0)
    expect(page.locator("#reference-heading")).to_contain_text("Without battery")
    expect(page.locator("#reference-issue")).to_have_text("")
    expect(page.locator("#expected-benefit")).to_have_text(
        "Self-consumption and smart planning start from the same measured SoC and opening bonus budgets. "
        "Expected benefits include the difference in remaining battery value. "
        "Benefits versus no battery include storage and smart planning. Excludes investment costs. "
        f"{correction}Not evaluated lifetime savings. See Savings for evaluated results."
    )
    expect(page.locator("#reference-costs")).to_contain_text("Expected economic cost")
    expect(page.locator("#self-consumption-costs")).to_contain_text(
        "Expected benefit versus without battery"
    )
    expect(page.locator("#ems-costs")).to_contain_text("Expected benefit versus without battery")
    state = page.evaluate("""() => {
        const reference = Chart.getChart(document.getElementById('reference-chart'));
        const prices = Chart.getChart(document.getElementById('price-chart'));
        return {reference: reference.data.datasets.map(dataset => dataset.label),
            scales: ['reference-chart','self-consumption-chart','plan-chart'].map(id => {
                const axis = Chart.getChart(document.getElementById(id)).scales.y;
                return [axis.min,axis.max,...axis.ticks.map(tick => tick.value)];
            }),
            prices: prices.data.datasets.map(dataset => ({label: dataset.label, values: dataset.data.map(point => point.y)})),
            scenarioOrder: ['plan-chart','self-consumption-chart','reference-chart','price-chart'].map(id => document.getElementById(id).closest('figure').getBoundingClientRect().top),
            evidenceLast: document.getElementById('plan-chart').getBoundingClientRect().top < document.getElementById('plan-evidence-panel').getBoundingClientRect().top};
    }""")
    assert not any("Battery" in name for name in state["reference"])
    assert state["scales"][0] == state["scales"][1] == state["scales"][2]
    assert state["scenarioOrder"] == sorted(state["scenarioOrder"])
    assert len(set(state["scenarioOrder"])) == 4
    assert state["evidenceLast"]
    assert [row["label"] for row in state["prices"]] == [
        "Spot market rate",
        "Import rate",
        "Export rate",
    ]
    assert set(state["prices"][0]["values"]) == {0.1}
    assert set(state["prices"][1]["values"]) == {0.3}
    assert set(state["prices"][2]["values"]) == {0}
    expect(page.locator("#self-consumption-chart")).to_be_visible()
    expect(page.locator("#self-consumption-chart-legend")).to_contain_text("SoC")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("named", [False, True])
def test_multiple_vehicle_soc_lines_have_distinct_names_colors_and_physical_progress(
    app_page, named
):
    """Each car receives .4 usable kWh per quarter. Three quarters raise a 10-kWh car 50→62% and a 20-kWh car 25→31%."""
    page, url = app_page
    start = datetime(2026, 10, 26, tzinfo=UTC)
    plan = {
        "comparison_names": {"car-a": "City car", "car-b": "Family car"} if named else {},
        "input_snapshot": {
            "problem": {
                "intervals": [
                    {
                        "start": (start + timedelta(minutes=15 * interval_index)).isoformat(),
                        "duration_h": 0.25,
                        "load_kwh": 0,
                    }
                    for interval_index in range(3)
                ],
                "batteries": [],
                "pv_sources": [],
                "inverters": [],
                "ev_work": [
                    {
                        "vehicle_id": "car-a",
                        "session_id": "a",
                        "capacity_kwh": 10,
                        "initial_soc": 50,
                    },
                    {
                        "vehicle_id": "car-b",
                        "session_id": "b",
                        "capacity_kwh": 20,
                        "initial_soc": 25,
                    },
                ],
            }
        },
        "expected_flows": [
            {
                "grid_import": 1,
                "grid_export": 0,
                "ev": [
                    {"session_id": session_id, "ac_kwh": 0.5, "stored_kwh": 0.4}
                    for session_id in ("a", "b")
                ],
            }
            for _ in range(3)
        ],
    }
    group = balances(page, url, plan)[0]
    assert group["missing"] == 0
    soc = [series for series in group["series"] if series.get("axis") == "soc"]
    assert [series["name"] for series in soc] == (
        ["City car: SoC", "Family car: SoC"] if named else ["Vehicle 1: SoC", "Vehicle 2: SoC"]
    )
    assert len({series["color"] for series in soc}) == 2
    assert [series["points"][0][1] for series in soc] == [50, 25]
    assert [series["points"][-1][1] for series in soc] == pytest.approx([62, 31])


def test_terminal_balance_losses_soc_and_mixed_duration_are_independent(app_page):
    """4 DC in = 1 load + .8 export + 1.6 charge + .6 loss.

    SoC starts at 5/10 kWh (50%). 1.6 * .95 = 1.52 kWh raises it to 6.52 kWh (65.2%).
    The .95 internal charge efficiency is an explicit assumption of physical_plan.
    """
    page, url = app_page
    plan = physical_plan()
    original = deepcopy(plan)
    groups = balances(page, url, plan)
    assert len(groups) == 2
    for group in groups:
        assert group["missing"] == 0
        bars = [series for series in group["series"] if series.get("type") == "bar"]
        for index in range(3):
            assert sum(series["intervals"][index]["value"] for series in bars) == pytest.approx(0)
    series = {series["name"]: series for series in groups[0]["series"]}
    assert [point["value"] for point in series["Conversion losses"]["intervals"]] == pytest.approx(
        [-2.4, -0.56, -0.56]
    )
    assert series["Used solar: DC"]["intervals"][0]["value"] == 16
    assert [point[1] for point in series["Home battery: SoC"]["points"]][:2] == pytest.approx(
        [50, 65.2]
    )
    assert groups[1]["series"][1]["intervals"][2]["value"] == 2
    assert plan == original


@pytest.mark.parametrize(
    "defect",
    [
        "missing_solar",
        "missing_converter",
        "missing_battery",
        "missing_grid",
        "inconsistent",
        "bad_efficiency",
    ],
)
def test_missing_or_inconsistent_evidence_cannot_be_presented_as_balanced(app_page, defect):
    """Omit each physical boundary or alter conservation. Bars become unavailable instead of inventing losses."""
    page, url = app_page
    plan = physical_plan()
    flow = plan["expected_flows"][0]
    if defect.startswith("missing_"):
        flow.pop(
            {
                "missing_solar": "solar",
                "missing_converter": "inverters",
                "missing_battery": "batteries",
                "missing_grid": "grid_import",
            }[defect]
        )
    elif defect == "inconsistent":
        flow["grid_export"] = 10
    else:
        plan["input_snapshot"]["problem"]["inverters"][0]["efficiency"] = 0
    group = balances(page, url, plan)[0]
    assert group["missing"] >= 1
    assert all(
        series["intervals"][0]["value"] is None
        for series in group["series"]
        if series.get("type") == "bar"
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_balance_axis_bounds_change_after_real_legend_and_time_controls(app_page):
    """A 102-kW late import widens all three charts until hidden or outside the six-hour window.

    The last interval consumes 100 kWh and charges the converter with 2 kWh.
    Its 102-kW grid import exceeds the earlier quarter's 4/0.25 = 16-kW used solar.
    This deliberately large simulated peak independently proves both UI handlers.
    """
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans?limit=20", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    response = page.request.get(url + f"/v1/plans/{plan_id}/comparison", headers=headers).json()
    fixture = physical_plan()
    fixture["input_snapshot"]["problem"]["intervals"][-1].update(
        start="2026-10-23T07:00:00Z", load_kwh=100
    )
    fixture["expected_flows"][-1]["grid_import"] = 102
    page.clock.set_fixed_time(
        datetime.fromisoformat(fixture["input_snapshot"]["problem"]["intervals"][0]["start"])
    )
    response["plan"]["input_snapshot"]["problem"] = fixture["input_snapshot"]["problem"]
    response["plan"]["expected_flows"] = fixture["expected_flows"]
    response["comparison"], response["scenarios"] = None, {}
    page.route("**/v1/plans/*/comparison", lambda route: route.fulfill(json=response))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    read_bounds = "['reference-chart','self-consumption-chart','plan-chart'].map(id => {const scale=Chart.getChart(document.getElementById(id)).scales.y;return [scale.min,scale.max,...scale.ticks.map(tick=>tick.value)];})"
    full_bounds = page.evaluate(read_bounds)
    assert full_bounds[2][:2] == pytest.approx([-127.5, 127.5])
    assert full_bounds[0] == full_bounds[1] == full_bounds[2]
    grid_import = (
        page.locator("#plan-chart-legend label").filter(has_text="Grid import").locator("input")
    )
    grid_import.uncheck()
    hidden_bounds = page.evaluate(read_bounds)
    assert hidden_bounds[0] == hidden_bounds[1] == hidden_bounds[2]
    assert hidden_bounds[2][:2] == pytest.approx([-116.75, 30.75])
    grid_import.check()
    assert page.evaluate(read_bounds) == full_bounds
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    limited_bounds = page.evaluate(read_bounds)
    assert limited_bounds[0] == limited_bounds[1] == limited_bounds[2]
    assert limited_bounds[2][:2] == pytest.approx([-20, 20])


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_rendered_balances_keep_real_widths_zero_sum_soc_and_accessible_inspection(app_page):
    """Desktop/mobile use equal quarter widths and zero sums while preserving exact interval inspection and SoC."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans?limit=20", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    body = page.request.get(url + f"/v1/plans/{plan_id}/comparison", headers=headers).json()
    fixture = physical_plan()
    body["plan"]["input_snapshot"]["problem"] = fixture["input_snapshot"]["problem"]
    body["plan"]["expected_flows"] = fixture["expected_flows"]
    page.clock.set_fixed_time(
        datetime.fromisoformat(fixture["input_snapshot"]["problem"]["intervals"][0]["start"])
    )
    body["comparison"] = None
    body["scenarios"] = {}
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/v1/plans/*/comparison", lambda route: route.fulfill(json=body))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    state = page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('plan-chart'));
        return {widths: chart.getDatasetMeta(0).data.map(bar => bar.width),
            sums: [0,1,2].map(index => chart.data.datasets.filter(dataset => dataset.type === 'bar').reduce((sum, dataset) => sum + dataset.data[index].y, 0)),
            limits: [chart.scales.y.min, chart.scales.y.max],
            stackExtremes: [0,1,2].flatMap(index => [
                chart.data.datasets.filter(dataset => dataset.type === 'bar').reduce((sum, dataset) => sum + Math.min(0, dataset.data[index].y), 0),
                chart.data.datasets.filter(dataset => dataset.type === 'bar').reduce((sum, dataset) => sum + Math.max(0, dataset.data[index].y), 0)]),
            soc: [chart.scales.soc.min, chart.scales.soc.max],
            socLabels: chart.scales.soc.ticks.map(tick => tick.label),
            timeTitle: chart.options.scales.x.title.display,
            wattLabels: [0.2,0.4].map(value => Chart.getChart(document.getElementById("energy-chart")).options.scales.y.ticks.callback(value)),
            values: chart.data.datasets.map(dataset => ({label:dataset.label,color:dataset.backgroundColor,axis:dataset.yAxisID}))};
    }""")
    assert len(state["widths"]) == 9
    assert state["widths"] == pytest.approx([state["widths"][0]] * 9)
    assert state["sums"] == pytest.approx([0, 0, 0])
    lowest, highest = min(state["stackExtremes"]), max(state["stackExtremes"])
    margin = (highest - lowest) * 0.1
    assert state["limits"][0] <= lowest - margin
    assert state["limits"][1] >= highest + margin
    assert state["soc"] == [0, 100]
    assert state["socLabels"] and all(label.endswith(".0") for label in state["socLabels"])
    assert state["timeTitle"] is False
    assert state["wattLabels"] == ["0.2", "0.4"]
    assert len({entry["color"] for entry in state["values"] if entry["axis"] == "y"}) == 7
    expect(page.locator("[id^=converter-balance]")).to_have_count(0)
    expect(page.get_by_role("heading", name="converter balance", exact=False)).to_have_count(0)
    expect(page.get_by_role("heading", name="Battery converter", exact=False)).to_have_count(0)
    expect(page.locator("#plan-chart-legend .series-swatch")).to_have_count(8)
    canvas = page.locator("#plan-chart")
    canvas.scroll_into_view_if_needed()
    bounds = canvas.bounding_box()
    point = page.evaluate(
        "Chart.getChart(document.getElementById('plan-chart')).getDatasetMeta(0).data[0].getCenterPoint()"
    )
    if page.viewport_size["width"] < 500:
        page.touchscreen.tap(bounds["x"] + point["x"], bounds["y"] + point["y"])
    # Locator hover scrolls the canvas back into view after a touch opens the inspector.
    canvas.hover(position={"x": point["x"], "y": point["y"]})
    popup = page.locator("#plan-chart-tooltip")
    expect(popup).to_be_visible()
    formatted_timestamps = page.evaluate(
        "values => values.map(value => new Date(value).toLocaleString('en-US', {year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC'}))",
        ["2026-10-23T00:00:00Z", "2026-10-23T00:15:00Z"],
    )
    expect(popup).to_contain_text(f"From {formatted_timestamps[0]} to {formatted_timestamps[1]}")
    expect(popup).to_contain_text("Used solar")
    expect(popup.get_by_role("heading")).to_have_text(
        ["Production", "Consumption", "Battery state"]
    )
    expect(popup.locator("section").nth(2)).to_contain_text("SoC at start")
    expect(popup.locator("section").nth(2).locator(".chart-tooltip-value")).to_have_text("50.0")
    sections = popup.locator("section")
    expect(sections.nth(0)).to_contain_text("Used solar")
    expect(sections.nth(0)).to_contain_text("Grid import")
    expect(sections.nth(0)).to_contain_text("Battery discharge")
    expect(sections.nth(1)).to_contain_text("Household demand")
    expect(sections.nth(1)).to_contain_text("Grid export")
    expect(sections.nth(1)).to_contain_text("Battery charge")
    expect(sections.nth(1)).to_contain_text("Conversion losses")
    assert sections.nth(1).locator(".chart-tooltip-value").all_text_contents() == [
        "-4",
        "-3.2",
        "-6.4",
        "-2.4",
    ]
    plot_bounds = canvas.bounding_box()
    legend_bounds = page.locator("#plan-chart-legend").bounding_box()
    if page.viewport_size["width"] <= MOBILE_CHART_LEGEND_BREAKPOINT_PX:
        assert legend_bounds["y"] >= plot_bounds["y"] + plot_bounds["height"]
    else:
        assert legend_bounds["x"] >= plot_bounds["x"] + plot_bounds["width"]
    edges = popup.locator(".chart-tooltip-value").evaluate_all(
        "cells => cells.map(cell => cell.getBoundingClientRect().right)"
    )
    assert len(edges) >= 7
    assert max(edges) - min(edges) <= 1
    popup_bounds = popup.bounding_box()
    assert popup_bounds["x"] >= 0
    assert popup_bounds["x"] + popup_bounds["width"] <= page.viewport_size["width"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('plan-chart'));
        const datasetIndex = chart.data.datasets.findIndex(dataset => dataset.yAxisID === 'soc');
        chart.tooltip.setActiveElements([{datasetIndex, index: 0}], {x: 0, y: 0});
        chart.update('none');
    }""")
    expect(popup.get_by_role("heading")).to_have_text(["Battery state"])
    expect(popup).to_contain_text("SoC")
    expect(popup).to_contain_text("50")
    if not page.get_by_label("Inspect planning time", exact=True).is_visible():
        if not page.get_by_text("Selected interval details", exact=True).is_visible():
            page.get_by_text("Understand this plan", exact=True).click()
        page.get_by_text("Selected interval details", exact=True).click()
    page.get_by_label("Inspect planning time", exact=True).focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#inspect-values")).to_contain_text("Battery converter 1")
    expect(page.locator("#inspect-values")).to_contain_text("AC supply to converter")
    assert errors == []


@pytest.mark.parametrize("storage", [False, True])
def test_separate_solar_and_shared_battery_converters_keep_ac_routes(app_page, storage):
    """4kWh PV yields 3.6 AC. Separate storage takes 2 AC into 1.44 terminal kWh with .56 conversion loss."""
    page, url = app_page
    plan = physical_plan()
    problem = plan["input_snapshot"]["problem"]
    problem["intervals"] = problem["intervals"][1:2]
    problem["intervals"][0]["load_kwh"] = 1
    problem["pv_sources"] = [
        {"source_id": "east", "inverter_id": "solar"},
        {"source_id": "west", "inverter_id": "solar"},
    ]
    problem["inverters"] = [{"inverter_id": "solar", "efficiency": 0.9}]
    problem["batteries"] = []
    flow = {
        "grid_import": 0,
        "grid_export": 2.6,
        "solar": [{"source_id": "east", "used_kwh": 2}, {"source_id": "west", "used_kwh": 2}],
        "inverters": [{"inverter_id": "solar", "import_kwh": 0, "export_kwh": 3.6}],
        "batteries": [],
    }
    if storage:
        problem["inverters"].append({"inverter_id": "plant", "efficiency": 0.9})
        for identity in ("battery", "spare"):
            problem["batteries"].append(
                {
                    "device_id": identity,
                    "inverter_id": "plant",
                    "dc_efficiency": 0.8,
                    "battery": {"capacity_kwh": 10, "initial_kwh": 1.5},
                }
            )
            flow["batteries"].append(
                {"device_id": identity, "charge_kwh": 0.72, "discharge_kwh": 0, "end_kwh": 2.184}
            )
        flow["inverters"].append({"inverter_id": "plant", "import_kwh": 2, "export_kwh": 0})
        flow["grid_export"] = 0.6
    plan["expected_flows"] = [flow]
    groups = balances(page, url, plan)
    assert len(groups) == (2 if storage else 1)
    for group in groups:
        assert group["missing"] == 0
        assert sum(
            series["intervals"][0]["value"]
            for series in group["series"]
            if series.get("type") == "bar"
        ) == pytest.approx(0)
    if storage:
        converter = {series["name"]: series for series in groups[1]["series"]}
        assert converter["Used solar: DC"]["intervals"][0]["value"] == 0
        assert converter["AC supply to converter"]["intervals"][0]["value"] == 2
        assert converter["Battery charge: DC"]["intervals"][0]["value"] == pytest.approx(-1.44)
        assert converter["Conversion losses"]["intervals"][0]["value"] == pytest.approx(-0.56)
        assert len([series for series in groups[1]["series"] if series.get("axis") == "soc"]) == 2
    else:
        assert not any(series.get("axis") == "soc" for series in groups[0]["series"])


@pytest.mark.parametrize(
    "noise,missing", [(-0.00000000000000005, 0), (-0.00000001, 0), (-0.01, 1), (None, 1)]
)
def test_balance_accepts_only_finite_zero_noise_without_rewriting_archives(
    app_page, noise, missing
):
    """Solver-scale negative zero renders as zero. Material negative or absent grid energy leaves a gap."""
    page, url = app_page
    plan = physical_plan()
    plan["expected_flows"][0]["grid_import"] = noise
    group = balances(page, url, plan)[0]
    assert group["missing"] == missing
    imported = next(series for series in group["series"] if series["name"] == "Grid import: AC")
    assert imported["intervals"][0]["value"] == (None if missing else 0)
    assert plan["expected_flows"][0]["grid_import"] == noise


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("missing", ["battery", "interval"])
def test_incomplete_first_flow_keeps_metadata_names_gap_and_later_inspection(app_page, missing):
    """Missing first evidence retains its slot. The second slot still shows its own independently known 4kW discharge."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    identity = page.request.get(url + "/v1/plans?limit=20", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    body = page.request.get(url + f"/v1/plans/{identity}/comparison", headers=headers).json()
    body["plan"]["comparison_names"] = {"battery": "Home battery"}
    if missing == "battery":
        body["plan"]["expected_flows"][0].pop("batteries")
    else:
        body["plan"]["expected_flows"][0] = None
    page.route("**/v1/plans/*/comparison", lambda route: route.fulfill(json=body))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("[id^=converter-balance]")).to_have_count(0)
    expect(page.locator("#plan-chart-legend")).to_contain_text("Home battery: SoC")
    expect(page.locator("#plan-chart-legend .balance-warning")).to_contain_text("1 interval(s)")
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Selected interval details", exact=True).click()
    page.get_by_label("Inspect planning time", exact=True).focus()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_label("Inspect planning time", exact=True)).to_have_value("1")
    expect(page.locator("#inspect-values")).to_contain_text("Battery discharge: DC 4 kW")
    expect(page.locator("#inspect-values")).to_contain_text("Home battery: SoC 0.0%")
    if missing == "interval":
        assert page.evaluate(
            "Chart.getChart(document.getElementById('energy-chart')).data.datasets[0].data.slice(0,2).map(point => point.y)"
        ) == [None, None]
