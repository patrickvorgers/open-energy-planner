"""Planning charts stay usable while optional simulations and disclosed evidence load."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from open_energy_planner.savings.evidence import evidence as plan_evidence
from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, open_chart_series, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
@pytest.mark.parametrize("simulation_fails", [False, True])
def test_simulation_starts_before_plan_arrives_and_early_result_preserves_plan(
    app_page, simulation_fails
):
    """Concurrent reads overlap. An early simulation result cannot render or replace the pending original plan."""
    page, url = app_page
    held = {}

    def hold(route):
        """Retain both actual responses while exposing which requests reached the server boundary."""
        view = parse_qs(urlsplit(route.request.url).query)["view"][0]
        held[view] = route
        page.evaluate(
            "name => {window.overlappingReads ||= {}; window.overlappingReads[name] = true;}", view
        )

    page.route("**/comparison?view=plan", hold)
    page.route("**/comparison?view=simulations", hold)
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.wait_for_function(
        "() => window.overlappingReads?.plan && window.overlappingReads?.simulations"
    )
    expect(page.locator("#plan-description")).to_have_text("Loading selected plan…")
    if simulation_fails:
        held["simulations"].fulfill(status=503)
    else:
        held["simulations"].fulfill(response=held["simulations"].fetch())
    expect(page.locator("#plan-description")).to_have_text("Loading selected plan…")
    held["plan"].fulfill(response=held["plan"].fetch())
    expect(page.locator("#plan-chart-legend")).to_contain_text("Grid import")
    if simulation_fails:
        expect(page.locator("#overview-status")).to_contain_text(
            "Plan loaded. Simulations are unavailable"
        )
    else:
        expect(page.locator("#overview-status")).to_contain_text("Connected")


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_charts_precede_simulations_and_evidence_waits_for_disclosure(app_page):
    """A complete synthetic horizon appears before held simulations and remains usable during lazy evidence."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    simulations, evidence = [], []
    page.route("**/comparison?view=simulations", lambda route: simulations.append(route))

    def hold_evidence(route):
        """Expose arrival after the route is held, independently of the earlier loading text."""
        evidence.append(route)
        page.evaluate("window.evidenceReadHeld = true")

    page.route("**/comparison?view=evidence", hold_evidence)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Plan loaded. Loading simulations…")
    expect(page.locator("#plan-chart-legend")).to_contain_text("Grid import")
    expect(page.locator("#price-chart-legend")).to_contain_text("Import rate")
    for name in (
        "Detailed interval energy",
        "Expected state and commands per battery",
        "Expected solar production and commands",
        "Original and repriced expected costs",
    ):
        expect(page.get_by_text(name, exact=True)).to_have_count(0)
    expect(page.locator("#inspect-time")).to_have_attribute("max", "191")
    assert evidence == []
    page.evaluate(
        "window.immediatePlanChart = Chart.getChart(document.getElementById('plan-chart'))"
    )
    open_chart_series(page, "plan-chart")
    page.locator("#plan-chart-legend").get_by_label("Grid import: AC", exact=True).uncheck()
    open_chart_series(page, "energy-chart").get_by_label("Optimizer", exact=True).uncheck()
    assert len(simulations) == 1
    simulations.pop().fulfill(response=simulations_response(page, url))
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    for chart in ("plan-chart", "reference-chart", "self-consumption-chart", "price-chart"):
        expect(page.locator("#" + chart)).to_be_visible()
    assert page.evaluate(
        "window.immediatePlanChart === Chart.getChart(document.getElementById('plan-chart'))"
    )
    expect(
        page.locator("#plan-chart-legend").get_by_label("Grid import: AC", exact=True)
    ).not_to_be_checked()
    expect(
        open_chart_series(page, "energy-chart").get_by_label("Optimizer", exact=True)
    ).not_to_be_checked()
    assert evidence == []
    captures = Path("results/ci/progressive-planning")
    captures.mkdir(parents=True, exist_ok=True)
    series = open_chart_series(page, "plan-chart").get_by_text("Chart series", exact=True)
    page.locator("#plan-chart-legend").get_by_label("Grid import: AC", exact=True).check()
    series.press("Escape")
    page.locator(".optimizer-scenario").screenshot(
        path=str(captures / f"optimizer-{page.viewport_size['width']}.png")
    )
    page.screenshot(
        path=str(captures / f"charts-{page.viewport_size['width']}.png"), full_page=True
    )
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence-status")).to_have_text("Loading original plan evidence…")
    page.wait_for_function("() => window.evidenceReadHeld === true")
    assert len(evidence) == 1
    page.get_by_text("Selected interval details", exact=True).click()
    page.locator("#inspect-time").press("ArrowRight")
    expect(page.locator("#inspect-values")).to_contain_text("Grid import")
    expect(page.locator("#evidence-issue")).to_contain_text("Loading")
    assert len(evidence) == 1
    page.locator("#plan-evidence-panel").screenshot(
        path=str(captures / f"evidence-loading-{page.viewport_size['width']}.png")
    )
    technical = page.locator("#inspect-values > details")
    summary = technical.locator("summary")
    summary.focus()
    summary.press("Enter")
    pending = evidence.pop()
    pending.fulfill(response=pending.fetch())
    expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    expect(page.locator("#plan-evidence")).not_to_contain_text("Forecast version")
    expect(page.locator("#interval-evidence")).to_contain_text("Forecast and original prices")
    expect(summary).to_be_focused()
    expect(technical).to_have_attribute("open", "")
    summary.press("Enter")
    expect(technical).not_to_have_attribute("open", "")
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Understand this plan", exact=True).click()
    wait_for_product_reads(page)
    assert evidence == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def simulations_response(page, url):
    """Read the actual selected plan's simulation projection independently of the held browser fetch."""
    identity = page.locator("#plan-version").input_value()
    response = page.request.get(
        url + f"/v1/plans/{identity}/comparison?view=simulations",
        headers={"Authorization": "Bearer " + TOKEN},
    )
    assert response.ok
    return response


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
@pytest.mark.parametrize("failure", ["http-error", "wrong-plan"])
def test_simulation_failure_keeps_plan_and_evidence_can_retry_independently(app_page, failure):
    """Neither a simulation failure nor a failed evidence disclosure removes the plan or its prices."""
    page, url = app_page
    page.clock.set_fixed_time(START)

    def fail_simulations(route):
        """Return a failed request or a successful response belonging to a different plan."""
        if failure == "http-error":
            route.fulfill(status=503)
        else:
            route.fulfill(json={"plan_id": "different-plan"})

    page.route("**/comparison?view=simulations", fail_simulations)
    page.route("**/comparison?view=evidence", lambda route: route.fulfill(status=503))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text(
        "Plan loaded. Simulations are unavailable"
    )
    expect(page.locator("#plan-chart-legend")).to_contain_text("Grid import")
    expect(page.locator("#current-price-status")).to_be_empty()
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence-status")).to_contain_text(
        "Close and reopen this panel to retry"
    )
    page.unroute("**/comparison?view=evidence")
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    expect(page.locator("#overview-status")).to_contain_text("Simulations are unavailable")
    page.unroute("**/comparison?view=simulations")
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#reference-chart")).to_be_visible()


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("stage", ["simulations", "evidence"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_late_supplement_never_changes_newly_selected_plan(app_page, stage, outcome):
    """An old plan's late simulation or evidence success/failure cannot alter the newly selected plan."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    original = page.request.get(url + "/v1/plans", headers=headers).json()["plans"][0]
    body = page.request.get(
        url + f"/v1/plans/{original['plan_id']}/comparison", headers=headers
    ).json()
    selected = deepcopy(body)
    selected["plan"].update(plan_id="selected-plan", strategy="minimize consumption")
    selected["evidence"]["plan_id"] = "selected-plan"
    selected["evidence"]["summary"][0]["title"] = "Selected plan evidence"
    body["evidence"]["summary"][0]["title"] = "Old plan evidence"
    body["scenarios"]["self_consumption"]["issue"] = "Old plan simulation"
    page.clock.set_fixed_time(
        datetime.fromisoformat(body["plan"]["input_snapshot"]["problem"]["intervals"][0]["start"])
    )
    held = []
    page.add_init_script("window.projectionRouteHeld = false")
    page.route(
        "**/v1/plans?**",
        lambda route: route.fulfill(
            json={
                "plans": [original, {**original, "plan_id": "selected-plan"}],
                "next_before": None,
            }
        ),
    )

    def projection(route):
        """Keep only the selected old-plan projection pending and return coherent paired synthetic documents."""
        address = urlsplit(route.request.url)
        identity = address.path.split("/")[-2]
        view = parse_qs(address.query)["view"][0]
        source = selected if identity == "selected-plan" else body
        if identity == original["plan_id"] and view == stage:
            held.append(route)
            page.evaluate("window.projectionRouteHeld = true")
            return
        if view == "plan":
            response = {"plan": source["plan"]}
        elif view == "evidence":
            response = {"plan_id": identity, "evidence": source["evidence"]}
        else:
            response = {
                "plan_id": identity,
                **{key: value for key, value in source.items() if key not in ("plan", "evidence")},
            }
        route.fulfill(json=response)

    page.route("**/comparison?view=*", projection)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#plan-description")).to_be_empty()
    if stage == "evidence":
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        page.get_by_text("Understand this plan", exact=True).click()
        expect(page.locator("#plan-evidence-status")).to_contain_text("Loading")
    else:
        expect(page.locator("#overview-status")).to_contain_text("Loading simulations")
    page.wait_for_function("() => window.projectionRouteHeld")
    assert len(held) == 1
    open_chart_series(page, "energy-chart").get_by_label("Optimizer", exact=True).uncheck()
    page.locator("#plan-version").select_option("selected-plan")
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-version option:checked")).to_contain_text("Minimize consumption")
    if stage == "simulations":
        page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence")).to_contain_text("Selected plan evidence")
    before = page.locator("#ems-costs").inner_text()
    if outcome == "failure":
        held[0].fulfill(status=503)
    else:
        late = {
            "plan_id": original["plan_id"],
            **(
                {"evidence": body["evidence"]}
                if stage == "evidence"
                else {key: value for key, value in body.items() if key not in ("plan", "evidence")}
            ),
        }
        held[0].fulfill(json=late)
    wait_for_product_reads(page)
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-version option:checked")).to_contain_text("Minimize consumption")
    expect(page.locator("#plan-evidence")).to_contain_text("Selected plan evidence")
    expect(page.locator("body")).not_to_contain_text("Old plan evidence")
    expect(page.locator("body")).not_to_contain_text("Old plan simulation")
    expect(
        open_chart_series(page, "energy-chart").get_by_label("Optimizer", exact=True)
    ).to_be_checked()
    assert page.locator("#ems-costs").inner_text() == before


@pytest.mark.parametrize("app_site", ["solar_boundaries"], indirect=True)
def test_compact_planning_preserves_commands_costs_and_original_archive(app_page):
    """Removed technical tables leave keyboard-accessible costs, selected commands and original data intact."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    identity = page.locator("#plan-version").input_value()
    headers = {"Authorization": "Bearer " + TOKEN}
    original = page.request.get(url + "/v1/plans/" + identity, headers=headers).json()
    for label in (
        "Detailed interval energy",
        "Expected state and commands per battery",
        "Expected solar production and commands",
        "Original and repriced expected costs",
    ):
        expect(page.get_by_text(label, exact=True)).to_have_count(0)
    for host in ("ems-costs", "self-consumption-costs", "reference-costs"):
        disclosure = page.locator("#" + host).get_by_text("Cost breakdown", exact=True)
        import_cost = page.locator("#" + host).get_by_text("Import cost", exact=True).first
        expect(import_cost).not_to_be_visible()
        disclosure.focus()
        disclosure.press("Enter")
        expect(import_cost).to_be_visible()
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    expect(page.locator("#plan-evidence")).not_to_contain_text("Forecast version")
    page.get_by_text("Selected interval details", exact=True).click()
    expect(page.locator("#interval-evidence")).to_contain_text("Forecast and original prices")
    expect(page.locator("#interval-evidence")).not_to_contain_text("Forecast version")
    expect(page.get_by_role("heading", name="Detailed interval values", exact=True)).to_be_visible()
    page.locator("#inspect-time").press("Home")
    expect(page.locator("#inspect-values")).to_contain_text("Example home battery")
    expect(page.locator("#inspect-values")).to_contain_text("Example AC roof")
    expect(page.locator("#inspect-values")).to_contain_text("Planned grid exchange")
    expect(page.locator("#plan-evidence")).not_to_contain_text("Original contract version")
    values = page.locator("#inspect-values")
    expect(
        values.get_by_role(
            "heading", name="Forecast and original prices: Selected interval", exact=True
        )
    ).to_be_visible()
    expect(values.get_by_text("Stored energy", exact=True)).to_be_visible()
    technical = values.locator("details")
    expect(technical.get_by_text("Installation energy balance", exact=True)).not_to_be_visible()
    for control in page.locator(".inspector summary, #inspect-time").all():
        assert control.bounding_box()["height"] >= 44
    if page.viewport_size["width"] < 500:
        summary = technical.locator("summary")
        summary.scroll_into_view_if_needed()
        bounds = summary.bounding_box()
        page.touchscreen.tap(bounds["x"] + 10, bounds["y"] + bounds["height"] - 2)
        expect(technical.get_by_text("Installation energy balance", exact=True)).to_be_visible()
        summary.press("Enter")
        expect(technical.get_by_text("Installation energy balance", exact=True)).not_to_be_visible()
    technical.locator("summary").focus()
    technical.locator("summary").press("Enter")
    expect(technical.get_by_text("Installation energy balance", exact=True)).to_be_visible()
    expect(technical.get_by_text("Native resolution", exact=True)).to_be_visible()
    expect(page.locator("#inspect-time")).to_have_value("0")
    page.locator("#inspect-time").press("ArrowRight")
    expect(page.locator("#inspect-time")).to_have_value("1")
    expect(page.locator("#inspect-time")).to_be_focused()
    expect(technical).to_have_attribute("open", "")
    groups = page.locator("#interval-evidence .evidence-inverter-group")
    assert groups.count() == len(original["input_snapshot"]["problem"]["inverters"])
    for inverter_index, inverter in enumerate(original["input_snapshot"]["problem"]["inverters"]):
        group = page.locator("#interval-evidence").get_by_role(
            "region", name=f"Inverter {inverter_index + 1}", exact=True
        )
        for source in original["input_snapshot"]["problem"]["pv_sources"]:
            if source["inverter_id"] == inverter["inverter_id"]:
                expect(group).to_contain_text(original["comparison_names"][source["source_id"]])
    expect(page.locator("#plan-evidence-panel")).to_contain_text("not measurements")
    assert page.request.get(url + "/v1/plans/" + identity, headers=headers).json() == original
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    captures = Path("results/ci/compact-planning")
    captures.mkdir(parents=True, exist_ok=True)
    page.evaluate("document.fonts.ready")
    page.screenshot(
        path=str(captures / f"planning-{page.viewport_size['width']}.png"), full_page=True
    )
    if page.viewport_size["width"] < 500:
        page.set_viewport_size({"width": 390, "height": 1700})
    panel = page.locator("#plan-evidence-panel")
    panel.evaluate("element => element.scrollIntoView({block:'center'})")
    panel.screenshot(path=str(captures / f"commands-{page.viewport_size['width']}.png"))
    page.locator("#inspect-time").evaluate(
        "input => {input.value = Math.min(48, Number(input.max)); input.dispatchEvent(new Event('input', {bubbles:true}));}"
    )
    selected = page.locator("#interval-evidence-panel")
    technical = page.locator("#inspect-values > details")
    technical.locator("summary").click()
    if page.viewport_size["width"] < 500:
        page.set_viewport_size(
            {"width": 390, "height": int(selected.bounding_box()["height"]) + 200}
        )
        selected.evaluate(
            "element => window.scrollTo(0, window.scrollY + element.getBoundingClientRect().top - 100)"
        )
    selected.screenshot(path=str(captures / f"interval-{page.viewport_size['width']}.png"))
    technical.locator("summary").click()
    if page.viewport_size["width"] < 500:
        page.set_viewport_size(
            {"width": 390, "height": int(selected.bounding_box()["height"]) + 200}
        )
        selected.evaluate(
            "element => window.scrollTo(0, window.scrollY + element.getBoundingClientRect().top - 100)"
        )
    selected.screenshot(path=str(captures / f"technical-{page.viewport_size['width']}.png"))


@pytest.mark.parametrize("app_site", ["solar_boundaries"], indirect=True)
def test_scenarios_apply_one_shared_scale_batch_after_progressive_first_plan(app_page):
    """The optimizer appears first. Simulations redraw changed charts and unchanged range requests draw nothing."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    held = []
    page.route("**/comparison?view=simulations", lambda route: held.append(route))
    page.add_init_script("""(() => {
        window.balanceDraws=[];window.chartDraws=[];window.chartAssignments=0;
        let implementation;
        Object.defineProperty(window,'Chart',{configurable:true,get:()=>implementation,set:value=>{
            window.chartAssignments++;
            if (implementation === value) return;
            implementation=value;
            const update=value.prototype.update;
            value.prototype.update=function(...args) {
                if (args[0] !== 'resize') window.chartDraws.push(this.canvas?.id);
                if (args[0] !== 'resize' && ['plan-chart','reference-chart','self-consumption-chart'].includes(this.canvas?.id)
                    && this.data.datasets.some(dataset=>dataset.data.length)) window.balanceDraws.push(this.canvas.id);
                return update.apply(this,args);
            };
        }});
    })();""")
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Plan loaded. Loading simulations…")
    page.evaluate("document.fonts.ready")
    initial = page.locator("#plan-chart").evaluate("""canvas => {
        const chart=Chart.getChart(canvas);
        return {data:JSON.stringify(chart.data.datasets),bounds:[chart.scales.y.min,chart.scales.y.max]};
    }""")
    assert initial["bounds"][0] < 0 < initial["bounds"][1]
    assert (
        page.locator("#reference-chart").evaluate(
            "canvas => Chart.getChart(canvas).data.datasets.length"
        )
        == 0
    )
    assert len(held) == 1
    page.evaluate("window.balanceDraws=[];window.chartDraws=[]")
    held[0].fulfill(response=held[0].fetch())
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    assert (
        page.evaluate("window.chartDraws.filter(id=>['forecast-chart','price-chart'].includes(id))")
        == []
    )
    identities = ["plan-chart", "reference-chart", "self-consumption-chart"]
    for action in (None, "Full period", "Next 6 hours"):
        if action is not None:
            page.evaluate("window.balanceDraws=[]")
            page.get_by_role("button", name=action, exact=False).click()
        result = page.evaluate(
            """ids => ({
            counts:ids.map(id=>window.balanceDraws.filter(value=>value===id).length),
            bounds:ids.map(id=>{const chart=Chart.getChart(document.getElementById(id));return [chart.scales.y.min,chart.scales.y.max];})
        })""",
            identities,
        )
        expected_counts = {None: [0, 1, 1], "Full period": [0, 0, 0], "Next 6 hours": [1, 1, 1]}
        assert result["counts"] == expected_counts[action], {
            **result,
            "chart_assignments": page.evaluate("window.chartAssignments"),
        }
        assert result["bounds"][0] == result["bounds"][1] == result["bounds"][2]
    page.evaluate("window.chartDraws=[]")
    page.get_by_role("button", name="Next 6 hours", exact=False).click()
    assert page.evaluate("window.chartDraws") == []
    assert (
        page.locator("#plan-chart").evaluate(
            "canvas => JSON.stringify(Chart.getChart(canvas).data.datasets)"
        )
        == initial["data"]
    )
    page.evaluate("window.balanceDraws=[]")
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Plan loaded. Loading simulations…")
    assert page.evaluate(
        "ids => ids.map(id=>window.balanceDraws.filter(value=>value===id).length)", identities
    ) == [1, 0, 0]
    assert len(held) == 2
    page.evaluate("window.balanceDraws=[]")
    held[1].fulfill(response=held[1].fetch())
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    assert page.evaluate(
        "ids => ids.map(id=>window.balanceDraws.filter(value=>value===id).length)", identities
    ) == [0, 1, 1]


@pytest.mark.parametrize("app_site", ["solar_boundaries"], indirect=True)
def test_archived_appliance_cycle_remains_visible_after_interval_evidence_arrives(app_page):
    """Canonical interval cards retain an independently supplied appliance's original 0.12-kWh schedule."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    identity = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    comparison = page.request.get(
        url + f"/v1/plans/{identity}/comparison?view=plan", headers=headers
    ).json()
    original = page.request.get(url + f"/v1/plans/{identity}", headers=headers).json()
    plan = comparison["plan"]
    intervals = plan["input_snapshot"]["problem"]["intervals"]
    plan["comparison_names"]["example-cycle"] = "Example scheduled appliance"
    plan["appliance_schedule"] = [
        {
            "entity_id": "example-cycle",
            "start": intervals[0]["start"],
            "end": intervals[1]["start"],
            "interval_kwh": [0.12] + [0] * (len(intervals) - 1),
        }
    ]
    page.route("**/comparison?view=plan", lambda route: route.fulfill(json=comparison))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Connected")
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Selected interval details", exact=True).click()
    values = page.locator("#interval-evidence")
    expect(
        values.get_by_role("heading", name="Household forecast: This interval", exact=True)
    ).to_be_visible()
    appliance = values.locator("article").filter(
        has=page.get_by_role("heading", name="Example scheduled appliance", exact=True)
    )
    expect(appliance).to_contain_text("0.12 kWh")
    assert (
        appliance.get_by_text("Cycle starts", exact=True).evaluate(
            "term => term.nextElementSibling.textContent"
        )
        == "11/4/2026, 12:00 AM"
    )
    assert (
        appliance.get_by_text("Cycle ends", exact=True).evaluate(
            "term => term.nextElementSibling.textContent"
        )
        == "11/4/2026, 12:15 AM"
    )
    assert page.request.get(url + f"/v1/plans/{identity}", headers=headers).json() == original


@pytest.mark.parametrize("app_site", ["solar_boundaries"], indirect=True)
def test_missing_physical_evidence_retains_original_solar_command_and_household_value(app_page):
    """Evidence arrival retains curtailment and the archived house quantity without inventing solar use."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    identity = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    comparison = page.request.get(
        url + f"/v1/plans/{identity}/comparison?view=plan", headers=headers
    ).json()
    plan = comparison["plan"]
    plan["expected_flows"] = []
    source = next(
        unit["source_id"]
        for unit in plan["input_snapshot"]["problem"]["pv_sources"]
        if unit["can_curtail"]
    )
    plan["steps"][0]["devices"][source] = {"enabled": False}
    projected = plan_evidence(plan)
    assert projected["intervals"][0]["issue"]
    pending = []
    page.route("**/comparison?view=plan", lambda route: route.fulfill(json=comparison))
    page.route("**/comparison?view=evidence", lambda route: pending.append(route))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Connected")
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Selected interval details", exact=True).click()
    values = page.locator("#interval-evidence")
    fallback_solar = values.locator("article").filter(
        has=page.get_by_role("heading", name=plan["comparison_names"][source], exact=True)
    )
    expect(fallback_solar.get_by_text("Curtailment requested", exact=True)).to_be_visible()
    fallback = values.get_by_text("Household demand: AC", exact=True)
    assert fallback.evaluate("term => term.nextElementSibling.textContent") == "0.113 kWh"
    assert len(pending) == 1
    pending.pop().fulfill(json={"plan_id": identity, "evidence": projected})
    solar = values.locator("article").filter(
        has=page.get_by_role(
            "heading", name=plan["comparison_names"][source] + ": Solar", exact=True
        )
    )
    expect(solar.get_by_text("Curtailment requested", exact=True)).to_be_visible()
    assert (
        solar.get_by_text("Planned use: DC", exact=True).evaluate(
            "term => term.nextElementSibling.textContent"
        )
        == "Not recorded"
    )
    recorded = values.get_by_text("Household demand: AC: This interval", exact=True)
    assert recorded.evaluate("term => term.nextElementSibling.textContent") == "0.113 kWh"
