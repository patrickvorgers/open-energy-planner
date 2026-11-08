"""A delayed metadata reload cannot take ownership from a newer installation edit or save."""

from copy import deepcopy

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    connect,
    discover,
    finish,
    grid,
    installation_area,
    read_settings,
    wait_for_product_reads,
)


@pytest.mark.parametrize("action", ["edit", "edit-only", "save"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_delayed_background_reload_preserves_newer_installation_ownership(
    app_page, action, outcome
):
    """Release the actual source-name reload's held strategy response after Edit or Save and retain its draft/revisions."""
    page, url = app_page

    def expose_probe(route):
        """Expose existing reload and read-only config without changing their implementation."""
        response = route.fetch()
        route.fulfill(
            response=response,
            body=response.text()
            + "\nwindow.__installationProbe={refresh:refreshSourceNames,config:()=>structuredClone(config)};",
        )

    page.route("**/setup/setup.js", expose_probe)
    page.goto(url + "/setup")
    discover(page, url)
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    installation_area(page, 0)
    wait_for_product_reads(page)
    before = page.evaluate("window.__installationProbe.config()")
    changed = deepcopy(read_settings(page, url))
    changed["solar"]["version"] = "b" * 64
    page.route("**/v1/setup", lambda route: route.fulfill(json=changed))
    page.evaluate(
        """outcome => {
        const nativeFetch=window.fetch;
        window.strategyHeld=false;
        window.fetch=(path,options)=>{
            if(String(path).endsWith('v1/strategy')&&options.signal) {
                window.strategyHeld=true;
                return new Promise((resolve,reject)=>{window.completeStrategy=()=>outcome==='failure'?reject(new TypeError('PRIVATE-STAGED-READ')):resolve(new Response(JSON.stringify({requested:'minimize cost'})));});
            }
            return nativeFetch(path,options);
        };
        window.pendingRefresh=window.__installationProbe.refresh(AbortSignal.timeout(65000));
    }""",
        outcome,
    )
    page.wait_for_function("() => window.strategyHeld")
    page.get_by_role("button", name="Edit installation", exact=True).click()
    maximum = page.get_by_label("Maximum grid import (kW)", exact=True)
    if action != "edit-only":
        maximum.fill("7.25")
    if action == "save":
        page.locator("#save").click()
        expect(page.locator("#installation-save-state")).to_contain_text("Installation saved.")
    owned = page.evaluate("window.__installationProbe.config()")
    owned_status = page.locator("#installation-save-state").inner_text()
    if action == "save":
        assert owned["installation"]["version"] != before["installation"]["version"]
        assert (
            read_settings(page, url)["installation"]["version"] == owned["installation"]["version"]
        )
    page.evaluate("async()=>{window.completeStrategy();await window.pendingRefresh;}")
    assert page.evaluate("window.__installationProbe.config()") == owned
    expect(maximum).to_have_value("5" if action == "edit-only" else "7.25")
    expect(page.locator("#installation-save-state")).to_have_text(owned_status)
    if action != "save":
        expect(maximum).to_be_enabled()
        if action == "edit":
            expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    else:
        expect(maximum).to_be_disabled()
        # The next real write must use the new revision, never the delayed snapshot's guards.
        page.get_by_role("button", name="Edit installation", exact=True).click()
        maximum.fill("8")
        page.locator("#save").click()
        expect(page.locator("#installation-save-state")).to_contain_text("Installation saved.")
        assert (
            read_settings(page, url)["installation"]["version"] != owned["installation"]["version"]
        )
