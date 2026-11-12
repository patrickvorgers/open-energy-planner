"""Same-plan refreshes retain readable comparisons and share the original evidence read."""

from datetime import datetime

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("missing", ["self_consumption", "without_battery"])
def test_scenario_legend_choices_follow_scenarios_when_a_comparison_recovers(app_page, missing):
    """A recovered scenario starts visible without inheriting another scenario's checkbox or device semantics."""
    page, url = app_page
    simulations = []

    def comparison(route):
        """Remove one independent comparison only from the first real simulation response."""
        response = route.fetch().json()
        if not simulations:
            if missing == "self_consumption":
                response["comparison"] = None
            else:
                response["scenarios"]["without_battery"]["comparison"] = None
        simulations.append(response)
        route.fulfill(json=response)

    page.route("**/comparison?view=simulations", comparison)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    names = {
        "self_consumption": "Battery self-consumption",
        "without_battery": "Without battery: Baseline",
    }
    recovered = names[missing]
    retained = names["without_battery" if missing == "self_consumption" else "self_consumption"]
    legend = page.locator("#energy-chart-legend")
    expect(legend.get_by_role("checkbox", name=recovered, exact=True)).to_have_count(0)
    retained_choice = legend.get_by_role("checkbox", name=retained, exact=True)
    retained_choice.focus()
    retained_choice.press("Space")
    expect(retained_choice).not_to_be_checked()
    legend.get_by_role("checkbox", name="Optimizer", exact=True).uncheck()
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(legend.get_by_role("checkbox", name=recovered, exact=True)).to_be_visible()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    expect(legend.get_by_role("checkbox", name=retained, exact=True)).not_to_be_checked()
    expect(legend.get_by_role("checkbox", name=recovered, exact=True)).to_be_checked()
    expect(legend.get_by_role("checkbox", name="Optimizer", exact=True)).not_to_be_checked()
    for name in ("Optimizer", retained, recovered):
        choice = legend.get_by_role("checkbox", name=name, exact=True)
        expect(choice).to_be_visible()
        assert choice.get_attribute("aria-describedby") is None
    expect(page.locator("#energy-chart-device-key")).to_have_count(0)
    expect(
        page.get_by_role("img", name="Expected EMS and reference net grid power", exact=True)
    ).to_be_visible()


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("evidence_state", ["loaded", "pending"])
@pytest.mark.parametrize("replacement", ["success", "failure"])
def test_same_plan_refresh_keeps_comparisons_and_evidence(app_page, evidence_state, replacement):
    """Successful original reads keep prior supplemental data until a replacement settles."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    original = page.request.get(url + "/v1/plans/browser-original-plan", headers=headers).json()
    page.clock.install(time=datetime.fromisoformat(original["generated_at"]))
    evidence, evidence_reads, simulations = [], [], []
    page.on(
        "request",
        lambda request: (
            evidence_reads.append(request.url)
            if request.url.endswith("/comparison?view=evidence")
            else None
        ),
    )
    if evidence_state == "pending":
        page.route("**/comparison?view=evidence", lambda route: evidence.append(route))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    page.get_by_text("Understand this plan", exact=True).click()
    if evidence_state == "loaded":
        expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
        wait_for_product_reads(page)
    else:
        expect(page.locator("#plan-evidence-status")).to_have_text(
            "Loading original plan evidence…"
        )
    assert len(evidence_reads) == 1
    page.locator("#reference-chart-legend").get_by_label("Grid import: AC", exact=True).uncheck()
    for label in (
        "Original and repriced expected costs",
        "Detailed interval energy",
        "Expected state and commands per battery",
    ):
        page.get_by_text(label, exact=True).click()
    expect(page.locator("#expected-cost-rows tr")).to_have_count(6)
    expect(page.locator("#plan-rows tr")).to_have_count(2)
    expect(page.locator("#plan-battery-rows tr")).to_have_count(2)
    costs = page.locator("#expected-cost-rows").inner_text()
    rows = page.locator("#plan-rows").inner_text()
    battery_rows = page.locator("#plan-battery-rows").inner_text()
    evidence_text = page.locator("#plan-evidence").inner_text()
    page.evaluate("""() => {
        window.confirmedComparisons=['reference-chart','self-consumption-chart'].map(id=>({
            id,chart:Chart.getChart(document.getElementById(id)),
            data:JSON.stringify(Chart.getChart(document.getElementById(id)).data.datasets)
        }));
    }""")
    page.route("**/comparison?view=simulations", lambda route: simulations.append(route))
    with page.expect_request("**/comparison?view=simulations"):
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Plan loaded. Loading simulations…")
    assert len(simulations) == 1
    for chart in ("reference-chart", "self-consumption-chart"):
        expect(page.locator("#" + chart)).to_be_visible()
    assert page.locator("#expected-cost-rows").inner_text() == costs
    assert page.locator("#plan-rows").inner_text() == rows
    assert page.locator("#plan-battery-rows").inner_text() == battery_rows
    assert page.locator("#plan-evidence").inner_text() == evidence_text
    assert len(evidence_reads) == 1
    expect(
        page.locator("#reference-chart-legend").get_by_label("Grid import: AC", exact=True)
    ).not_to_be_checked()
    assert page.evaluate("""window.confirmedComparisons.every(saved=>
        saved.chart===Chart.getChart(document.getElementById(saved.id))&&
        saved.data===JSON.stringify(saved.chart.data.datasets))""")
    if evidence_state == "pending":
        assert len(evidence) == 1
        evidence[0].fulfill(response=evidence[0].fetch())
        expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    if replacement == "failure":
        simulations[0].fulfill(status=503)
        expect(page.locator("#overview-status")).to_contain_text("Use Refresh overview to retry")
    else:
        response = simulations[0].fetch().json()
        response["valuation_status"] = {"issue": "Synthetic replacement repricing notice"}
        simulations[0].fulfill(json=response)
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        expect(page.locator("#expected-benefit")).to_contain_text(
            "Synthetic replacement repricing notice"
        )
    wait_for_product_reads(page)
    assert len(evidence_reads) == 1
    expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    expect(page.locator("#expected-cost-rows tr")).to_have_count(6)
    assert page.locator("#expected-cost-rows").inner_text() == costs
    for chart in ("reference-chart", "self-consumption-chart"):
        expect(page.locator("#" + chart)).to_be_visible()
    assert (
        page.request.get(url + "/v1/plans/browser-original-plan", headers=headers).json()
        == original
    )
