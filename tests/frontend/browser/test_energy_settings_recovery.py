"""Protect Energy ownership and editor loading across failed paired reads."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.app.ev.support.devices import settings
from tests.frontend.browser.helpers import (
    TOKEN,
    add_thermal_appliance,
    discover_boiler_sources,
    wait_for_product_reads,
)
from tests.frontend.browser.test_appliance_settings import add_ev_appliance
from tests.frontend.browser.test_charge_point_energy_selection import prepare_charge_point
from tests.frontend.browser.thermal_fixture import document as thermal_document


@pytest.mark.parametrize("failure", ["network", "json", "decoder", "http", "unauthorized", "abort"])
def test_appliance_session_failures_never_display_private_diagnostics(app_page, failure):
    """Session fetch, body, parser and authorization failures expose only the shared fixed public advice."""
    page, url = app_page
    page.add_init_script(
        """(() => {
            const original=window.fetch;
            window.sessionBodyReads=[];
            window.fetch=(path,options)=>{
                if(!String(path).endsWith('setup/session'))return original(path,options);
                const failure=FAILURE;
                if(failure==='network')return Promise.reject(new TypeError('PRIVATE-SESSION-NETWORK'));
                if(failure==='abort')return Promise.reject(new DOMException('PRIVATE-SESSION-ABORT','AbortError'));
                const response=new Response('PRIVATE-SESSION-BODY',{
                    status:failure==='unauthorized'?401:failure==='http'?503:200
                });
                const responseIndex=window.sessionBodyReads.push(0)-1;
                const parse=response.json.bind(response);
                response.json=()=>{
                    ++window.sessionBodyReads[responseIndex];
                    if(failure==='decoder')return Promise.reject(new Error('PRIVATE-SESSION-DECODER'));
                    return parse();
                };
                return Promise.resolve(response);
            };
        })();""".replace("FAILURE", repr(failure))
    )
    page.goto(url + "/appliance-settings")
    expect(page.locator("#status")).to_have_text(
        "The installation token was rejected."
        if failure == "unauthorized"
        else "Home Assistant data could not be read. Check the connection and retry."
    )
    expect(page.locator("body")).not_to_contain_text("PRIVATE-SESSION-")
    body_reads = page.evaluate("window.sessionBodyReads")
    if failure in ("json", "decoder"):
        assert body_reads and all(reads == 1 for reads in body_reads)
    else:
        assert not any(body_reads)
    expect(page.get_by_role("button", name="Connect", exact=True)).to_be_enabled()


@pytest.mark.parametrize("editor", ["ev", "thermal", "consumption"])
@pytest.mark.parametrize("failed", ["editor", "consumers"])
def test_failed_paired_read_holds_busy_until_peer_settles_and_retains_draft(
    app_page, editor, failed
):
    """A failed discard waits for both reads, applies neither result and permits a clean retry."""
    page, url = app_page
    discover_boiler_sources(page, url)
    page.goto(url + "/appliance-settings" + ("" if editor == "consumption" else "/" + editor))
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    if editor == "ev":
        add_ev_appliance(page, "vehicle")
        field = page.get_by_label("Usable battery capacity (kWh)", exact=True)
        field.fill("77.7")
        bar = page.locator("#ev-save-bar")
    elif editor == "thermal":
        add_thermal_appliance(page)
        field = page.get_by_label("Water volume (litres)", exact=False)
        field.fill("177")
        bar = page.locator("#thermal-save-bar")
    else:
        page.get_by_role("button", name="Add appliance", exact=True).click()
        page.locator("#source-dialog button.source-choice:enabled").first.click()
        field = page.get_by_label("Duration (minutes)", exact=True)
        field.fill("177")
        bar = page.locator("#appliance-save-bar")
    before = field.input_value()
    wait_for_product_reads(page)
    page.evaluate(
        """({editor,failed}) => {
            const original=window.fetch;
            const endpoint=editor==='consumption'?'appliances':editor;
            const bad='v1/'+(failed==='editor'?endpoint:'consumers');
            const held='v1/'+(failed==='editor'?'consumers':endpoint);
            window.heldStarted=false;window.failureSettled=false;window.peerSettled=false;
            window.fetch=(path,options)=>{
                if(String(path).split('?')[0].endsWith(bad)){
                    window.failureSettled=true;
                    return Promise.resolve(new Response('PRIVATE-FAILED-READ',{status:500}));
                }
                if(String(path).split('?')[0].endsWith(held)){
                    window.heldStarted=true;
                    return new Promise(resolve=>{window.releasePeer=async()=>{
                        const response=await original(path,options);
                        window.peerSettled=true;resolve(response);
                    };});
                }
                return original(path,options);
            };
            window.restorePairedReads=()=>{window.fetch=original;};
        }""",
        {"editor": editor, "failed": failed},
    )
    cancel = page.get_by_role("button", name="Cancel", exact=True)
    cancel.click()
    page.wait_for_function("window.heldStarted && window.failureSettled")
    # Drain microtasks after the immediate HTTP failure before observing busy ownership.
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(bar).to_have_attribute("data-save-state", "saving", timeout=1000)
    expect(field).to_be_disabled()
    expect(cancel).to_be_disabled()
    expect(field).to_have_value(before)
    page.evaluate("window.releasePeer()")
    expect(bar).to_have_attribute("data-save-state", "dirty")
    expect(field).to_have_value(before)
    expect(field).to_be_enabled()
    expect(page.locator("#status")).not_to_contain_text("PRIVATE-")
    page.evaluate("window.restorePairedReads()")
    cancel.click()
    expect(bar).to_have_attribute("data-save-state", "saved")
    expect(
        page.get_by_role(
            "button",
            name={
                "ev": "Add vehicle or charge point",
                "thermal": "Add electric boiler",
                "consumption": "Add appliance",
            }[editor],
            exact=True,
        )
    ).to_be_enabled()
    if editor == "consumption":
        expect(field).to_have_value("")
        expect(field).not_to_be_visible()
    else:
        expect(field).to_have_count(0)


@pytest.mark.parametrize("app_site", ["consumers_unreadable"], indirect=True)
@pytest.mark.parametrize("editor", ["ev", "thermal"])
def test_unreadable_real_consumer_ownership_blocks_typed_admission(app_page, editor):
    """A corrupt stored selection remains unknown and cannot be replaced by a new specialized owner."""
    page, url = app_page
    _, headers = prepare_charge_point(page, url)
    consumers = page.request.get(url + "/v1/consumers", headers=headers).json()
    assert consumers["issue"] == "consumer_configuration_unreadable"
    assert consumers["selected"] == [] and consumers["options"]
    before = page.request.get(url + "/v1/" + editor, headers=headers).json()
    configuration = settings() if editor == "ev" else thermal_document()["configuration"]
    if editor == "thermal":
        configuration["stores"][0].update(
            energy_entity="sensor.charger_energy",
            power_entity="sensor.charger_power",
            temperature_entity="sensor.temperature",
        )
    result = page.request.post(
        url + "/v1/" + editor + "/configuration",
        headers=headers,
        data={"configuration": configuration, "expected_version": before["configuration_version"]},
    )
    assert result.status == 400
    assert "Restore the saved database" in result.text()
    after = page.request.get(url + "/v1/" + editor, headers=headers).json()
    assert after["configuration_version"] == before["configuration_version"]
    assert page.request.get(url + "/v1/consumers", headers=headers).json() == consumers


@pytest.mark.parametrize("app_site", ["consumers_unreadable"], indirect=True)
@pytest.mark.parametrize("category", ["consumption", "ev", "thermal"])
def test_unreadable_real_consumer_ownership_keeps_shared_choices_unavailable(app_page, category):
    """Unknown persisted ownership cannot be presented as an available meter in any appliance chooser."""
    page, url = app_page
    prepare_charge_point(page, url)
    path = "/appliance-settings" if category == "consumption" else "/appliance-settings/" + category
    page.goto(url + path)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    if category == "ev":
        page.get_by_role("button", name="Add vehicle or charge point", exact=True).click()
        page.get_by_role("radio", name="Charge point", exact=False).check()
        page.get_by_role("button", name="Continue", exact=True).click()
    else:
        page.get_by_role(
            "button",
            name="Add appliance" if category == "consumption" else "Add electric boiler",
            exact=True,
        ).click()
    dialog = page.locator("#source-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#source-count")).to_contain_text("Restore the saved database")
    dialog.get_by_label("Show", exact=True).select_option("all")
    assert dialog.locator("button.source-choice").count() > 0
    expect(dialog.locator("button.source-choice:enabled")).to_have_count(0)


@pytest.mark.parametrize("editor", ["ev", "thermal"])
@pytest.mark.parametrize("outcome", ["error", "network", "json", "stall"])
def test_new_consumer_reads_keep_safe_errors_and_existing_deadline(app_page, editor, outcome):
    """New consumer GETs keep the shared 30-second deadline and never expose private failures."""
    page, url = app_page
    prepare_charge_point(page, url)
    if editor == "thermal":
        page.goto(url + "/appliance-settings/thermal")
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    wait_for_product_reads(page)
    page.evaluate(
        """outcome => {
            const original=window.fetch,nativeTimeout=AbortSignal.timeout.bind(AbortSignal),budgets=new WeakMap();
            window.consumerBudgets=[];window.consumerBodyReads=0;
            AbortSignal.timeout=milliseconds=>{const signal=nativeTimeout(milliseconds===30000?30:milliseconds);budgets.set(signal,milliseconds);return signal;};
            window.fetch=(path,options)=>{
                if(!String(path).split('?')[0].endsWith('v1/consumers'))return original(path,options);
                window.consumerBudgets.push(budgets.get(options.signal));
                if(outcome==='network')return Promise.reject(new TypeError('PRIVATE-CONSUMER-NETWORK'));
                if(outcome==='stall')return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('PRIVATE-CONSUMER-TIMEOUT','AbortError')),{once:true}));
                const response=new Response(outcome==='json'?'PRIVATE-CONSUMER-JSON':'PRIVATE-CONSUMER-HTTP',{status:outcome==='json'?200:500});
                response.text=()=>{++window.consumerBodyReads;return Promise.resolve('PRIVATE-CONSUMER-BODY');};
                return Promise.resolve(response);
            };
            window.restoreConsumerRead=()=>{window.fetch=original;AbortSignal.timeout=nativeTimeout;};
        }""",
        outcome,
    )
    if editor == "ev":
        page.get_by_role("button", name="Add vehicle or charge point", exact=True).click()
        page.get_by_role("radio", name="Charge point", exact=False).check()
        page.get_by_role("button", name="Continue", exact=True).click()
    else:
        page.get_by_role("button", name="Add electric boiler", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Check the connection and retry")
    expect(page.locator("#status")).not_to_contain_text("PRIVATE-")
    assert page.evaluate("window.consumerBudgets") == [30000]
    assert page.evaluate("window.consumerBodyReads") == 0
    expect(page.locator("#source-dialog")).not_to_be_visible()
    page.evaluate("window.restoreConsumerRead()")
    if editor == "ev":
        page.get_by_role("button", name="Add vehicle or charge point", exact=True).click()
        page.get_by_role("button", name="Continue", exact=True).click()
    else:
        page.get_by_role("button", name="Add electric boiler", exact=True).click()
    expect(page.locator("#source-dialog")).to_be_visible()


@pytest.mark.parametrize("endpoint", ["appliances", "consumers"])
@pytest.mark.parametrize("phase", ["headers", "body"])
def test_consumption_settings_deadline_covers_headers_body_and_preserves_draft(
    app_page, endpoint, phase
):
    """A stalled settings read releases the editor with its draft intact only after a bounded safe failure."""
    page, url = app_page
    discover_boiler_sources(page, url)
    page.goto(url + "/appliance-settings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_role("button", name="Add appliance", exact=True).click()
    page.locator("#source-dialog button.source-choice:enabled").first.click()
    field = page.get_by_label("Duration (minutes)", exact=True)
    field.fill("177")
    wait_for_product_reads(page)
    page.evaluate(
        """({endpoint,phase}) => {
            const original=window.fetch,nativeTimeout=AbortSignal.timeout.bind(AbortSignal),budgets=new WeakMap();
            window.readBudgets=[];
            AbortSignal.timeout=milliseconds=>{const signal=nativeTimeout(milliseconds===30000?30:milliseconds);budgets.set(signal,milliseconds);return signal;};
            window.fetch=(path,options)=>{
                if(!String(path).split('?')[0].endsWith('v1/'+endpoint))return original(path,options);
                window.readBudgets.push(budgets.get(options.signal));
                const failure=()=>new DOMException('PRIVATE-SETTINGS-TIMEOUT','AbortError');
                if(phase==='headers')return new Promise((resolve,reject)=>options.signal?.addEventListener('abort',()=>reject(failure()),{once:true}));
                const stream=new ReadableStream({start(controller){
                    controller.enqueue(new TextEncoder().encode('{'));
                    options.signal?.addEventListener('abort',()=>controller.error(failure()),{once:true});
                }});
                return Promise.resolve(new Response(stream,{headers:{'Content-Type':'application/json'}}));
            };
            window.restoreSettingsReads=()=>{window.fetch=original;AbortSignal.timeout=nativeTimeout;};
        }""",
        {"endpoint": endpoint, "phase": phase},
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Check the connection and retry", timeout=1500)
    expect(page.locator("#status")).not_to_contain_text("PRIVATE-")
    assert page.evaluate("window.readBudgets") == [30000]
    expect(page.locator("#appliance-save-bar")).to_have_attribute("data-save-state", "dirty")
    expect(field).to_have_value("177")
    expect(field).to_be_enabled()
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=f"results/ci/appliance-settings-read-recovery-{page.viewport_size['width']}.png",
        full_page=True,
    )
    page.evaluate("window.restoreSettingsReads()")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#appliance-save-bar")).to_have_attribute("data-save-state", "saved")
    expect(page.get_by_role("button", name="Add appliance", exact=True)).to_be_enabled()
