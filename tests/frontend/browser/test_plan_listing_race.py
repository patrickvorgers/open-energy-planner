"""A delayed historical list cannot undo a newer explicit plan selection."""

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("newer_selection", [True, False], ids=["select-b", "retain-linked-a"])
def test_delayed_linked_plan_listing_preserves_newer_selection(app_page, newer_selection):
    """Refresh of linked A must retain an intervening B choice, but may reload A when no newer choice exists."""
    page, url = app_page
    page.clock.install()
    headers = {"Authorization": "Bearer " + TOKEN}
    summary = page.request.get(url + "/v1/plans", headers=headers).json()["plans"][0]
    original_id, other_id = summary["plan_id"], "explicit-plan-b"
    body = page.request.get(url + f"/v1/plans/{original_id}/comparison", headers=headers).json()
    other = deepcopy(body["plan"])
    other.update(plan_id=other_id, strategy="minimize consumption")
    listing = {"plans": [summary, {**summary, "plan_id": other_id}], "next_before": None}
    held, reads = [], []
    state = {"hold": False}

    def list_plans(route):
        """Hold only the manual Refresh list, leaving the existing options usable."""
        if state["hold"]:
            held.append(route)
        else:
            route.fulfill(json=listing)

    def projection(route):
        """Observe A reads and supply a distinct immutable B through the normal projection contracts."""
        address = urlsplit(route.request.url)
        identity, view = address.path.split("/")[-2], parse_qs(address.query)["view"][0]
        reads.append((identity, view))
        if identity != other_id:
            route.continue_()
        elif view == "plan":
            route.fulfill(json={"plan": other})
        else:
            assert view == "simulations"
            route.fulfill(
                json={
                    "plan_id": other_id,
                    **{
                        key: value for key, value in body.items() if key not in ("plan", "evidence")
                    },
                }
            )

    page.route("**/v1/plans?limit=20", list_plans)
    page.route("**/comparison?view=*", projection)
    page.goto(url + f"/overview?plan={original_id}#planning")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    original_reads = list(reads)
    state["hold"] = True
    with page.expect_request("**/v1/plans?limit=20"):
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    page.evaluate("() => {}")
    assert len(held) == 1
    if newer_selection:
        with page.expect_response(f"**/v1/plans/{other_id}/comparison?view=simulations"):
            page.locator("#plan-version").select_option(other_id)
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
        page.evaluate(
            "window.selectedPlanChart=Chart.getChart(document.getElementById('plan-chart'))"
        )
        description = page.locator("#plan-description").inner_text()
        options = page.locator("#plan-version option").all_text_contents()
        chart = page.evaluate("window.selectedPlanChart.data.datasets.map(dataset=>dataset.data)")
    held[0].fulfill(json={**listing, "next_before": "delayed-next-page"})
    wait_for_product_reads(page)
    if newer_selection:
        expect(page.locator("#plan-version")).to_have_value(other_id)
        expect(page.locator("#plan-description")).to_have_text(description)
        assert page.locator("#plan-version option").all_text_contents() == options
        assert page.evaluate(
            "window.selectedPlanChart===Chart.getChart(document.getElementById('plan-chart'))"
        )
        assert (
            page.evaluate("window.selectedPlanChart.data.datasets.map(dataset=>dataset.data)")
            == chart
        )
        assert [read for read in reads if read[0] == original_id] == original_reads
        expect(page.locator("#older-plans")).to_be_hidden()
    else:
        expect(page.locator("#plan-version")).to_have_value(original_id)
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        assert reads == original_reads + [(original_id, "plan"), (original_id, "simulations")]
        expect(page.locator("#older-plans")).to_be_visible()
