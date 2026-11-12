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


@pytest.mark.parametrize("ownership", ["managed", "missing", "editable"])
def test_export_permission_preserves_captured_field_ownership(app_page, ownership):
    """The real component fails closed for synthetic managed/missing metadata and preserves its reload lock."""
    page, url = app_page

    def expose_ownership_probe(route):
        """Expose the real layout only in this routed test response, without a production test hook."""
        response = route.fetch()
        route.fulfill(
            response=response,
            body=response.text()
            + "\nwindow.__installationOwnership={layout,saveState,reload:()=>layout.refreshNames(config)};",
        )

    page.route("**/setup.js", expose_ownership_probe)
    page.goto(url + "/setup")
    discover(page, url)
    connect(page)
    expect(page.get_by_label("Maximum grid export (kW)", exact=True)).to_be_enabled()
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    installation_area(page, 0, edit=True)
    before = read_settings(page, url)["installation"]
    export = page.get_by_label("Maximum grid export (kW)", exact=True)
    permission = page.get_by_label("Allow grid feed-in", exact=True)
    page.evaluate(
        """ownership => {
            const {layout}=window.__installationOwnership;
            const control=document.getElementById('export');
            if(ownership==='missing') layout.controls.delete(control);
            else layout.controls.set(control,ownership==='managed');
            layout.sync();
        }""",
        ownership,
    )
    if ownership == "editable":
        expect(export).to_be_enabled()
    else:
        expect(export).to_be_disabled()
    permission.uncheck()
    expect(export).to_be_disabled()
    expect(export).to_have_value("0")
    permission.check()
    if ownership == "editable":
        expect(export).to_be_enabled()
        expect(export).to_have_value("5")
    else:
        expect(export).to_be_disabled()
    page.evaluate(
        """() => {
            const probe=window.__installationOwnership;
            probe.saveState.begin('Refreshing settings…');
            probe.reload();
        }"""
    )
    expect(export).to_be_disabled()
    expect(permission).to_be_disabled()
    page.evaluate(
        """() => {
            const {layout,saveState}=window.__installationOwnership;
            saveState.end();
            layout.sync();
        }"""
    )
    expect(permission).to_be_enabled()
    if ownership == "editable":
        expect(export).to_be_enabled()
    else:
        expect(export).to_be_disabled()
    assert read_settings(page, url)["installation"] == before


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

    page.route("**/setup.js", expose_probe)
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
