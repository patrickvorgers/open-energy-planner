"""Evidence follows the actively displayed plan, including retained and revisited identities."""

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_failed_same_plan_refresh_settles_retained_evidence(app_page, outcome):
    """A failed refresh retains A and its active evidence read, which must display data or usable retry guidance."""
    page, url = app_page
    page.clock.install()
    evidence = []
    page.route("**/comparison?view=evidence", lambda route: evidence.append(route))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    identity = page.locator("#plan-version").input_value()
    description = page.locator("#plan-description").inner_text()
    page.evaluate("window.retainedChart=Chart.getChart(document.getElementById('plan-chart'))")
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence-status")).to_have_text("Loading original plan evidence…")
    assert len(evidence) == 1
    page.route("**/comparison?view=plan", lambda route: route.fulfill(status=503))
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Showing the previously loaded plan")
    expect(page.locator("#plan-version")).to_have_value(identity)
    expect(page.locator("#plan-description")).to_have_text(description)
    if outcome == "success":
        evidence[0].fulfill(response=evidence[0].fetch())
    else:
        evidence[0].fulfill(status=503)
    wait_for_product_reads(page)
    if outcome == "success":
        expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
        expect(page.locator("#plan-evidence-status")).not_to_contain_text("Loading")
    else:
        expect(page.locator("#plan-evidence-status")).to_have_text(
            "Original plan evidence is unavailable. Close and reopen this panel to retry."
        )
        page.unroute("**/comparison?view=evidence")
        page.get_by_text("Understand this plan", exact=True).click()
        page.get_by_text("Understand this plan", exact=True).click()
        expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    assert len(evidence) == 1
    assert page.evaluate(
        "window.retainedChart===Chart.getChart(document.getElementById('plan-chart'))"
    )
    expect(page.locator("#plan-description")).to_have_text(description)


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_old_evidence_cannot_replace_a_new_read_after_a_b_a_selection(app_page, outcome):
    """The original A read stays obsolete after B and a newly loaded A, even though the final plan ID matches."""
    page, url = app_page
    page.clock.install()
    headers = {"Authorization": "Bearer " + TOKEN}
    summary = page.request.get(url + "/v1/plans", headers=headers).json()["plans"][0]
    identity = summary["plan_id"]
    original = page.request.get(url + f"/v1/plans/{identity}/comparison", headers=headers).json()
    other = deepcopy(original["plan"])
    other.update(plan_id="plan-b", strategy="minimize consumption")
    current_evidence = deepcopy(original["evidence"])
    current_evidence["summary"][0]["title"] = "Current evidence for revisited A"
    old_evidence = deepcopy(original["evidence"])
    old_evidence["summary"][0]["title"] = "Obsolete evidence from first A"
    held = []
    page.route(
        "**/v1/plans?**",
        lambda route: route.fulfill(
            json={"plans": [summary, {**summary, "plan_id": "plan-b"}], "next_before": None}
        ),
    )

    def projection(route):
        """Hold only the first A evidence response while coherent B and revisited-A reads finish."""
        address = urlsplit(route.request.url)
        plan_id, view = address.path.split("/")[-2], parse_qs(address.query)["view"][0]
        if view == "evidence":
            if plan_id == identity and not held:
                held.append(route)
            else:
                route.fulfill(
                    json={"plan_id": plan_id, "evidence": {**current_evidence, "plan_id": plan_id}}
                )
        elif plan_id == "plan-b":
            route.fulfill(
                json={"plan": other}
                if view == "plan"
                else {
                    "plan_id": plan_id,
                    "scenarios": original["scenarios"],
                    "comparison": original["comparison"],
                }
            )
        else:
            route.continue_()

    page.route("**/comparison?view=*", projection)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence-status")).to_contain_text("Loading")
    assert len(held) == 1
    page.locator("#plan-version").select_option("plan-b")
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    page.locator("#plan-version").select_option(identity)
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-evidence")).to_contain_text("Current evidence for revisited A")
    if outcome == "success":
        held[0].fulfill(json={"plan_id": identity, "evidence": old_evidence})
    else:
        held[0].fulfill(status=503)
    wait_for_product_reads(page)
    expect(page.locator("#plan-evidence")).to_contain_text("Current evidence for revisited A")
    expect(page.locator("body")).not_to_contain_text("Obsolete evidence from first A")
    expect(page.locator("#plan-evidence-status")).to_have_text("")
