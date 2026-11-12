"""Bound settings reads and preserve usable installation state after a cancelled staged refresh."""

from copy import deepcopy

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    add_thermal_appliance,
    connect,
    discover,
    discover_boiler_sources,
    finish,
    grid,
    installation_area,
    read_settings,
    wait_for_product_reads,
)


def test_cancelled_strategy_read_retains_installation_and_allows_source_retry(app_page):
    """A changed-panel refresh cannot commit configuration or withdraw readiness before its strategy read settles."""
    page, url = app_page

    def expose_probe(route):
        """Expose existing private functions and read-only state in this synthetic test only, without changing logic."""
        response = route.fetch()
        route.fulfill(
            response=response,
            body=response.text()
            + "\nwindow.__installationProbe = {refresh:refreshSourceNames, ready:()=>installationReady, config:()=>structuredClone(config)};",
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
    before_strategy = page.locator("#strategy").input_value()
    assert page.evaluate("window.__installationProbe.ready()")
    changed = deepcopy(read_settings(page, url))
    changed["solar"]["version"] = "b" * 64
    page.route("**/v1/setup", lambda route: route.fulfill(json=changed))
    page.evaluate("""() => {
        const fetchOriginal=window.fetch;
        window.setupReads=0; window.strategyHeld=false;
        window.fetch=(path,options)=>{
            if(String(path).endsWith('v1/setup')) ++window.setupReads;
            if(String(path).endsWith('v1/strategy') && options.signal){
                window.strategyHeld=true;
                return new Promise((resolve,reject)=>{
                    window.completeOldStrategy=()=>resolve(new Response(JSON.stringify({requested:'minimize consumption'})));
                    options.signal.addEventListener('abort',()=>reject(new DOMException('Synthetic timeout','AbortError')),{once:true});
                });
            }
            return fetchOriginal(path,options);
        };
        window.restoreFetch=()=>{window.fetch=fetchOriginal;};
        window.pendingRefresh=window.__installationProbe.refresh(AbortSignal.timeout(1500)).then(()=>false,()=>true);
    }""")
    page.wait_for_function("() => window.strategyHeld")
    page.get_by_role("button", name="Edit installation", exact=True).click()
    maximum = page.get_by_label("Maximum grid import (kW)", exact=True)
    maximum.fill("7.25")
    assert page.evaluate("window.pendingRefresh")
    assert page.evaluate("window.__installationProbe.ready()")
    assert page.evaluate("window.__installationProbe.config()") == before
    expect(maximum).to_have_value("7.25")
    page.evaluate("() => {window.completeOldStrategy(); window.restoreFetch();}")
    expect(page.locator("#strategy")).to_have_value(before_strategy)
    page.evaluate("() => window.__installationProbe.refresh(AbortSignal.timeout(5000))")
    assert page.evaluate("window.__installationProbe.ready()")
    expect(maximum).to_have_value("7.25")


@pytest.mark.parametrize("flow", ["ev-connect", "thermal-connect", "thermal-evidence"])
@pytest.mark.parametrize("outcome", ["stall", "error", "token", "network"])
def test_ordinary_settings_reads_are_bounded_safe_and_independently_retryable(
    app_page, flow, outcome
):
    """Clients without an explicit source deadline bound GETs, keep drafts and never consume private HTTP errors."""
    page, url = app_page
    body = discover_boiler_sources(page, url)
    editor = "ev" if flow.startswith("ev") else "thermal"
    page.goto(url + "/appliance-settings/" + editor)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    if flow == "thermal-evidence":
        page.get_by_role("button", name="Connect", exact=True).click()
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/v1/ha/source-choices/thermal")
            )
        ) as pending:
            add_thermal_appliance(page)
        assert page.request.post(
            url + "/v1/ha/source-choices",
            headers={"Authorization": "Bearer " + TOKEN},
            data={"scope": "thermal", "id": pending.value.json()["id"], "page": body},
        ).ok
        expect(page.locator("#status")).to_contain_text("sources refreshed")
        volume = page.get_by_label("Water volume (litres)", exact=False)
        volume.fill("180")
        action = page.get_by_role("button", name="Check saved settings", exact=True)
    else:
        action = page.get_by_role("button", name="Connect", exact=True)
    wait_for_product_reads(page)
    page.evaluate(
        """({editor,outcome}) => {
        const fetchOriginal=window.fetch, nativeTimeout=AbortSignal.timeout.bind(AbortSignal);
        window.readSignals=[]; window.readTimeouts=[]; window.bodyReads=0;
        const budgets=new WeakMap();
        AbortSignal.timeout=milliseconds=>{const signal=nativeTimeout(milliseconds===30000?30:milliseconds);budgets.set(signal,milliseconds);return signal;};
        window.fetch=(path,options)=>{
            if(!String(path).endsWith('v1/'+editor)) return fetchOriginal(path,options);
            window.readSignals.push(Boolean(options.signal));
            window.readTimeouts.push(options.signal ? budgets.get(options.signal) : null);
            if(outcome==='error'||outcome==='token'){
                const response=new Response('PRIVATE-SETTINGS-SENTINEL-'+ 'x'.repeat(10000),{status:outcome==='token'?401:500});
                response.text=()=>{++window.bodyReads;return Promise.resolve('PRIVATE-SETTINGS-SENTINEL');};
                return Promise.resolve(response);
            }
            if(outcome==='network') return Promise.reject(new TypeError('PRIVATE-NETWORK-SENTINEL'));
            return new Promise((resolve,reject)=>options.signal?.addEventListener('abort',()=>reject(new DOMException('PRIVATE-TIMEOUT-SENTINEL','AbortError')),{once:true}));
        };
        window.restoreRead=()=>{window.fetch=fetchOriginal;AbortSignal.timeout=nativeTimeout;};
    }""",
        {"editor": editor, "outcome": outcome},
    )
    action.click()
    page.wait_for_function("() => window.readSignals.length>0")
    assert page.evaluate("window.readSignals[0]")
    assert page.evaluate("window.readTimeouts") == [30000]
    expected = (
        "The installation token was rejected."
        if outcome == "token"
        else "Check the connection and retry"
    )
    expect(page.locator("#status")).to_contain_text(expected)
    expect(page.locator("#status")).not_to_contain_text("PRIVATE-")
    assert page.evaluate("window.bodyReads") == 0
    page.evaluate("window.restoreRead()")
    action.click()
    if flow == "thermal-evidence":
        expect(volume).to_have_value("180")
        expect(page.locator("#status")).not_to_contain_text("Check the connection and retry")
    else:
        expect(
            page.get_by_role(
                "button",
                name="Add vehicle or charge point" if editor == "ev" else "Add electric boiler",
                exact=True,
            )
        ).to_be_visible()


@pytest.mark.parametrize("editor", ["ev", "thermal"])
def test_rejected_installation_token_explains_recovery_and_accepts_correct_token(app_page, editor):
    """A real rejected Connect request explains the token failure and permits a corrected retry."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/appliance-settings/" + editor)
    token = page.get_by_label("Installation API token", exact=True)
    token.fill("wrong-installation-token")
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_have_text("The installation token was rejected.")
    token.fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(
        page.get_by_role(
            "button",
            name="Add vehicle or charge point" if editor == "ev" else "Add electric boiler",
            exact=True,
        )
    ).to_be_visible()
