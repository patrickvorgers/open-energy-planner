"""Planning charts stay usable while optional simulations and disclosed evidence load."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_charts_precede_simulations_and_evidence_waits_for_disclosure(app_page):
    """A complete synthetic horizon appears before held simulations and remains usable during lazy evidence."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    simulations, evidence = [], []
    page.route("**/comparison?view=simulations", lambda route: simulations.append(route))
    page.route("**/comparison?view=evidence", lambda route: evidence.append(route))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Plan loaded. Loading simulations…")
    expect(page.locator("#plan-chart-legend")).to_contain_text("Grid import")
    expect(page.locator("#price-chart-legend")).to_contain_text("Import rate")
    expect(page.locator("#plan-rows tr")).to_have_count(0)
    page.get_by_text("Detailed interval energy", exact=True).click()
    expect(page.locator("#plan-rows tr")).to_have_count(120)
    page.get_by_text("Detailed interval energy", exact=True).click()
    assert evidence == []
    page.evaluate(
        "window.immediatePlanChart = Chart.getChart(document.getElementById('plan-chart'))"
    )
    page.locator("#plan-chart-legend").get_by_label("Grid import: AC", exact=True).uncheck()
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
    assert evidence == []
    captures = Path("results/ci/progressive-planning")
    captures.mkdir(parents=True, exist_ok=True)
    page.locator("#plan-chart-legend").get_by_label("Grid import: AC", exact=True).check()
    page.locator(".optimizer-scenario").screenshot(
        path=str(captures / f"optimizer-{page.viewport_size['width']}.png")
    )
    page.screenshot(
        path=str(captures / f"charts-{page.viewport_size['width']}.png"), full_page=True
    )
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence-status")).to_have_text("Loading original plan evidence…")
    assert len(evidence) == 1
    page.get_by_text("Selected interval details", exact=True).click()
    page.locator("#inspect-time").press("ArrowRight")
    expect(page.locator("#inspect-values")).to_contain_text("Grid:")
    expect(page.locator("#evidence-issue")).to_contain_text("Loading")
    assert len(evidence) == 1
    page.locator("#plan-evidence-panel").screenshot(
        path=str(captures / f"evidence-loading-{page.viewport_size['width']}.png")
    )
    pending = evidence.pop()
    pending.fulfill(response=pending.fetch())
    expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    expect(page.locator("#interval-evidence")).to_contain_text("Forecast and original prices")
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
def test_simulation_failure_keeps_plan_and_evidence_can_retry_independently(app_page):
    """Neither a simulation failure nor a failed evidence disclosure removes the plan or its prices."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    page.route("**/comparison?view=simulations", lambda route: route.fulfill(status=503))
    page.route("**/comparison?view=evidence", lambda route: route.fulfill(status=503))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text(
        "Plan loaded. Simulations are unavailable"
    )
    expect(page.locator("#plan-chart-legend")).to_contain_text("Grid import")
    expect(page.locator("#current-price-status")).to_contain_text("Selected plan:")
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
    expect(page.locator("#plan-description")).to_contain_text("Generated:")
    if stage == "evidence":
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        page.get_by_text("Understand this plan", exact=True).click()
        expect(page.locator("#plan-evidence-status")).to_contain_text("Loading")
    else:
        expect(page.locator("#overview-status")).to_contain_text("Loading simulations")
    assert len(held) == 1
    page.locator("#plan-version").select_option("selected-plan")
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
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
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    expect(page.locator("#plan-evidence")).to_contain_text("Selected plan evidence")
    expect(page.locator("body")).not_to_contain_text("Old plan evidence")
    expect(page.locator("body")).not_to_contain_text("Old plan simulation")
    assert page.locator("#ems-costs").inner_text() == before
