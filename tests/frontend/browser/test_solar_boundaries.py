"""Keep mixed solar measurement boundaries and their conversion losses independently balanced."""

from copy import deepcopy

import pytest

from tests.frontend.browser.helpers import balances


def mixed_solar_plan():
    """Use 2 DC + 1.8 AC to supply 2 household + 1 battery + .7 export + .1 conversion loss.

    Each source has 3 kWh available but uses only 2 kWh. The separate solar
    converter loses .2 kWh before the AC measurement boundary. The hybrid
    converts 1 DC kWh into .9 AC kWh while 1 DC kWh charges the battery.
    Internal charge efficiency .95 raises stored energy from 5 to 5.95 kWh.
    """
    return {
        "input_snapshot": {
            "problem": {
                "intervals": [
                    {
                        "start": "2026-11-04T12:00:00Z",
                        "duration_h": 1,
                        "load_kwh": 2,
                        "pv_kwh": 6,
                        "pv_sources": [["roof", 3], ["garage", 3]],
                    }
                ],
                "inverters": [
                    {"inverter_id": "hybrid", "hybrid": True, "efficiency": 0.9},
                    {"inverter_id": "solar", "hybrid": False, "efficiency": 0.9},
                ],
                "pv_sources": [
                    {"source_id": "roof", "inverter_id": "hybrid"},
                    {"source_id": "garage", "inverter_id": "solar"},
                ],
                "batteries": [
                    {
                        "device_id": "battery",
                        "inverter_id": "hybrid",
                        "dc_efficiency": 1,
                        "battery": {
                            "capacity_kwh": 10,
                            "initial_kwh": 5,
                            "charge_efficiency": 0.95,
                        },
                    }
                ],
            }
        },
        "expected_flows": [
            {
                "grid_import": 0,
                "grid_export": 0.7,
                "solar": [
                    {"source_id": "roof", "used_kwh": 2},
                    {"source_id": "garage", "used_kwh": 2},
                ],
                "inverters": [
                    {"inverter_id": "hybrid", "import_kwh": 0, "export_kwh": 0.9},
                    {"inverter_id": "solar", "import_kwh": 0, "export_kwh": 1.8},
                ],
                "batteries": [
                    {"device_id": "battery", "charge_kwh": 1, "discharge_kwh": 0, "end_kwh": 5.95}
                ],
            }
        ],
    }


@pytest.mark.parametrize("storage", [True, False])
def test_mixed_boundaries_preserve_hybrid_identity_and_original_data(app_page, storage):
    """Separate PV's .2 loss stays outside. Hybrid .1 loss and battery terminal flow stay inside."""
    page, url = app_page
    plan = mixed_solar_plan()
    if not storage:
        plan["input_snapshot"]["problem"]["batteries"] = []
        plan["expected_flows"][0]["batteries"] = []
        plan["expected_flows"][0]["inverters"][0]["export_kwh"] = 1.8
        plan["expected_flows"][0]["grid_export"] = 1.6
    page.goto(url + "/overview")
    result = page.evaluate(
        """async plan => {
            const {energyBalances} = await import('./setup/balance.js');
            const before = JSON.stringify(plan);
            const groups = energyBalances(plan, new Map([['battery','Home battery']]));
            return {groups, unchanged:JSON.stringify(plan) === before};
        }""",
        plan,
    )
    assert result["unchanged"]
    groups = result["groups"]
    assert all(group["missing"] == 0 for group in groups)
    bars = {
        series["name"]: series["intervals"][0]["value"]
        for series in groups[0]["series"]
        if series.get("type") == "bar"
    }
    assert bars["Used solar: DC"] == pytest.approx(2)
    assert bars["Used solar: AC"] == pytest.approx(1.8)
    assert bars["Conversion losses"] == pytest.approx(-0.1 if storage else -0.2)
    assert bars["Grid export: AC"] == pytest.approx(-0.7 if storage else -1.6)
    assert sum(bars.values()) == pytest.approx(0)
    if storage:
        assert bars["Battery charge: DC"] == pytest.approx(-1)
        soc = next(series for series in groups[0]["series"] if series.get("axis") == "soc")
        assert [point[1] for point in soc["points"]] == pytest.approx([50, 59.5])
        converter = {
            series["name"]: series["intervals"][0]["value"]
            for series in groups[1]["series"]
            if series.get("type") == "bar"
        }
        assert converter["Used solar: DC"] == pytest.approx(2)
        assert converter["AC supply to converter"] == pytest.approx(0)
        assert converter["Converter output to AC"] == pytest.approx(-0.9)
        assert converter["Conversion losses"] == pytest.approx(-0.1)
        assert "Used solar: AC" not in converter
    colors = {series["name"]: series["color"] for series in groups[0]["series"]}
    assert colors["Used solar: DC"] != colors["Used solar: AC"]


@pytest.mark.parametrize("storage", [False, True])
def test_ac_solar_is_counted_once_and_battery_conversion_remains_inside(app_page, storage):
    """Two sources produce 3.6 AC once. Storage takes 2 AC into 1.44 DC with .56 internal-boundary loss."""
    page, url = app_page
    plan = mixed_solar_plan()
    problem, flow = plan["input_snapshot"]["problem"], plan["expected_flows"][0]
    problem["pv_sources"][0]["inverter_id"] = "solar"
    problem["intervals"][0]["load_kwh"] = 1
    flow["inverters"][1]["export_kwh"] = 3.6
    flow["inverters"][0].update(import_kwh=2 if storage else 0, export_kwh=0)
    flow["grid_export"] = 0.6 if storage else 2.6
    if storage:
        problem["batteries"][0]["dc_efficiency"] = 0.8
        flow["batteries"][0].update(charge_kwh=1.44, end_kwh=6.368)
    else:
        problem["batteries"] = []
        flow["batteries"] = []
    groups = balances(page, url, plan)
    assert all(group["missing"] == 0 for group in groups)
    bars = {
        series["name"]: series["intervals"][0]["value"]
        for series in groups[0]["series"]
        if series.get("type") == "bar"
    }
    assert bars["Used solar: DC"] == 0
    assert bars["Used solar: AC"] == pytest.approx(3.6)
    assert bars["Conversion losses"] == pytest.approx(-0.56 if storage else 0)
    assert sum(bars.values()) == pytest.approx(0)
    if storage:
        assert bars["Battery charge: DC"] == pytest.approx(-1.44)


@pytest.mark.parametrize("defect", ["source_mismatch", "missing_ac_flow", "invalid_efficiency"])
def test_ac_projection_cannot_hide_invalid_original_evidence(app_page, defect):
    """Moving the boundary must not make corrupt raw DC quantities or missing AC evidence look valid."""
    page, url = app_page
    plan = deepcopy(mixed_solar_plan())
    if defect == "source_mismatch":
        plan["expected_flows"][0]["solar"][1]["used_kwh"] = 2.5
    elif defect == "missing_ac_flow":
        plan["expected_flows"][0]["inverters"].pop()
    else:
        plan["input_snapshot"]["problem"]["inverters"][1]["efficiency"] = 0
    group = balances(page, url, plan)[0]
    assert group["missing"] == 1
    assert all(
        series["intervals"][0]["value"] is None
        for series in group["series"]
        if series.get("type") == "bar"
    )


@pytest.mark.parametrize("app_site", ["solar_boundaries"], indirect=True)
def test_three_scenarios_show_independent_solar_with_balanced_day_and_night(app_page):
    """Render validated mixed PV in all scenarios, preserving data through keyboard and bulk toggles."""
    from pathlib import Path

    from playwright.sync_api import expect

    from tests.frontend.browser.documentation_fixture import START
    from tests.frontend.browser.helpers import wait_for_product_reads
    from tests.frontend.browser.test_documentation_examples import authenticate

    page, url = app_page
    page.clock.set_fixed_time(START)
    authenticate(page, url, "/overview")
    wait_for_product_reads(page)
    page.locator('[data-hours="0"]').click()
    identities = ("plan-chart", "self-consumption-chart", "reference-chart")
    original = page.evaluate(
        """ids => ids.map(id => {
        const chart = Chart.getChart(document.getElementById(id));
        return chart.data.datasets.map(dataset => ({name:dataset.label, type:dataset.type,
            data:dataset.data, color:dataset.backgroundColor}));
    })""",
        identities,
    )
    directory = Path("results/ci/solar-boundaries")
    directory.mkdir(parents=True, exist_ok=True)
    for identity, datasets in zip(identities, original, strict=True):
        bars = [dataset for dataset in datasets if dataset["type"] == "bar"]
        solar = [
            next(dataset for dataset in bars if dataset["name"] == name)
            for name in ("Used solar: DC", "Used solar: AC")
        ]
        for dataset in solar:
            values = [point["y"] for point in dataset["data"]]
            assert len(values) >= 192
            assert all(value is not None for value in values)
            assert max(values) > 0.5 and min(values) == 0, (identity, dataset["name"], max(values))
        assert solar[0]["color"] != solar[1]["color"]
        for index in range(len(bars[0]["data"])):
            assert sum(dataset["data"][index]["y"] for dataset in bars) == pytest.approx(
                0, abs=0.00005
            )
        legend = page.locator(f"#{identity}-legend")
        dc = legend.get_by_role("checkbox", name="Used solar: DC", exact=True)
        ac = legend.get_by_role("checkbox", name="Used solar: AC", exact=True)
        dc.focus()
        dc.press("Space")
        expect(dc).not_to_be_checked()
        expect(ac).to_be_checked()
        legend.get_by_role("button", name="Select all", exact=True).click()
        expect(dc).to_be_checked()
        legend.get_by_role("button", name="Deselect all", exact=True).click()
        expect(ac).not_to_be_checked()
        legend.get_by_role("button", name="Select all", exact=True).click()
        if page.viewport_size["width"] < 500:
            page.set_viewport_size({"width": 390, "height": 1700})
        page.evaluate("document.fonts.ready")
        card = page.locator(f"#{identity}").locator("..").locator("..")
        card.evaluate("element => element.scrollIntoView({block: 'center'})")
        card.screenshot(path=str(directory / f"{identity}-{page.viewport_size['width']}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert (
        page.evaluate(
            """ids => ids.map(id => Chart.getChart(document.getElementById(id)).data.datasets.map(
        dataset => ({name:dataset.label,type:dataset.type,data:dataset.data,color:dataset.backgroundColor})))""",
            identities,
        )
        == original
    )


@pytest.mark.parametrize("app_site", ["solar_boundaries"], indirect=True)
def test_missing_ac_evidence_leaves_a_visible_gap_without_changing_archive(app_page):
    """A synthetic missing noon converter flow leaves a gap and warning in the real optimizer chart."""
    from pathlib import Path

    from playwright.sync_api import expect

    from tests.frontend.browser.documentation_fixture import START
    from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads
    from tests.frontend.browser.test_documentation_examples import authenticate

    page, url = app_page
    page.clock.set_fixed_time(START)

    def omit_ac_flow(route):
        """Corrupt only the response copy, retaining the immutable stored evidence."""
        response = route.fetch()
        document = response.json()
        document["plan"]["expected_flows"][48]["inverters"] = [
            flow
            for flow in document["plan"]["expected_flows"][48]["inverters"]
            if flow["inverter_id"] != "solar-ac"
        ]
        route.fulfill(response=response, json=document)

    page.route("**/comparison?view=plan", omit_ac_flow)
    authenticate(page, url, "/overview")
    wait_for_product_reads(page)
    expect(page.locator("#plan-chart-legend .balance-warning")).to_contain_text(
        "missing or inconsistent physical evidence"
    )
    bars = page.locator("#plan-chart").evaluate("""canvas => Chart.getChart(canvas).data.datasets
        .filter(dataset => dataset.type === 'bar').map(dataset => dataset.data)""")
    assert all(points[48]["y"] is None for points in bars)
    assert all(points[47]["y"] is not None and points[49]["y"] is not None for points in bars)
    plan_id = page.locator("#plan-version").input_value()
    original = page.request.get(
        url + "/v1/plans/" + plan_id + "/comparison?view=plan",
        headers={"Authorization": "Bearer " + TOKEN},
    ).json()["plan"]
    assert any(
        flow["inverter_id"] == "solar-ac" for flow in original["expected_flows"][48]["inverters"]
    )
    directory = Path("results/ci/solar-boundaries")
    directory.mkdir(parents=True, exist_ok=True)
    if page.viewport_size["width"] < 500:
        page.set_viewport_size({"width": 390, "height": 1700})
    page.locator("#plan-chart").locator("..").locator("..").screenshot(
        path=str(directory / f"missing-evidence-{page.viewport_size['width']}.png")
    )
