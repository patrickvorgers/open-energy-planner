"""Publication polling retains its busy guard until supplemental evidence settles."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_publication_retry_waits_for_disclosed_evidence_before_releasing_polling(app_page):
    """An automatic retry renders graphs early but cannot admit a second publication poll during its evidence read."""
    page, url = app_page
    page.clock.install()
    page.add_init_script("""(() => {
        const interval=window.setInterval,fetchOriginal=window.fetch;
        window.pollReads=0;
        window.setInterval=(callback,delay,...args)=>{
            if(delay===15000)window.pollPlan=callback;
            return interval(callback,delay,...args);
        };
        window.fetch=(path,options)=>{
            const address=new URL(path,location.href);
            if(address.pathname.endsWith('/v1/plans')&&address.searchParams.get('limit')==='1')window.pollReads++;
            return fetchOriginal(path,options);
        };
    })();""")
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    page.route("**/comparison?view=simulations", lambda route: route.fulfill(status=503))
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text(
        "Plan loaded. Simulations are unavailable"
    )
    wait_for_product_reads(page)
    page.unroute("**/comparison?view=simulations")
    evidence, simulations = [], []
    page.route("**/comparison?view=evidence", lambda route: evidence.append(route))
    page.route("**/comparison?view=simulations", lambda route: simulations.append(route))
    page.evaluate("() => {window.publicationRead=window.pollPlan();}")
    expect(page.locator("#overview-status")).to_have_text("Plan loaded. Loading simulations…")
    page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence-status")).to_have_text("Loading original plan evidence…")
    page.get_by_text("Selected interval details", exact=True).click()
    assert len(evidence) == len(simulations) == 1
    simulations[0].fulfill(response=simulations[0].fetch())
    expect(page.locator("#overview-status")).to_have_text(
        "Plan loaded. Loading original plan evidence…"
    )
    expect(page.locator("#self-consumption-chart-legend")).to_contain_text("SoC")
    assert page.evaluate("async () => {await window.pollPlan();return window.pollReads;}") == 1
    assert len(evidence) == len(simulations) == 1
    evidence[0].fulfill(response=evidence[0].fetch())
    expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    page.evaluate("async () => {await window.publicationRead;}")
    assert page.evaluate("async () => {await window.pollPlan();return window.pollReads;}") == 2
