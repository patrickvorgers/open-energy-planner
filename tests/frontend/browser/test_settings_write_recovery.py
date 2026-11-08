"""Keep failed or unacknowledged EV/boiler saves private, recoverable and version guarded."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover, wait_for_product_reads
from tests.frontend.browser.test_appliance_settings import add_ev_appliance


@pytest.mark.parametrize("editor", ["ev", "thermal"])
@pytest.mark.parametrize("outcome", ["network", "json", "http", "conflict", "token"])
def test_settings_write_failures_are_safe_and_preserve_recovery(app_page, editor, outcome):
    """Real editor saves retain drafts, hide private errors and reload actual committed writes after lost acknowledgement."""
    page, url = app_page
    body = discover(page, url)
    body["suggestions"]["consumers"] = [{"entity_id": "sensor.roof", "upstream": None}]
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    page.goto(url + "/appliance-settings/" + editor)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    if editor == "ev":
        add_ev_appliance(page, "vehicle")
        page.locator('[data-field="soc_entity"]').select_option("sensor.soc0")
        field = page.locator('[data-field="capacity_kwh"]')
        field.fill("64")
        page.locator('[data-field="maximum_w"]').fill("11")
    else:
        page.get_by_role("button", name="Add electric boiler", exact=True).click()
        for key, value in (
            ("energy_entity", "sensor.roof"),
            ("temperature_entity", "sensor.temperature"),
            ("power_entity", "sensor.grid"),
        ):
            page.locator(f'[data-field="{key}"]').select_option(value)
        for key, value in (
            ("volume_litres", "180"),
            ("minimum_c", "45"),
            ("maximum_c", "65"),
            ("ambient_c", "20"),
            ("loss_w_per_c", "1.2"),
            ("heater_w", "2"),
        ):
            page.locator(f'[data-field="{key}"]').fill(value)
        field = page.locator('[data-field="volume_litres"]')
    wait_for_product_reads(page)
    page.evaluate(
        """({editor,outcome}) => {
        const nativeFetch=window.fetch;
        window.writeBodiesRead=0; window.writeAttempts=0; window.writeSignal=null;
        window.fetch=async(path,options)=>{
            if(!String(path).endsWith('v1/'+editor+'/configuration')) return nativeFetch(path,options);
            ++window.writeAttempts; window.writeSignal=Boolean(options.signal);
            if(outcome==='conflict') {
                const body=JSON.parse(options.body); body.expected_version='stale-version';
                return nativeFetch(path,{...options,body:JSON.stringify(body)});
            }
            if(outcome==='http'||outcome==='token') {
                const response=new Response('PRIVATE-WRITE-SENTINEL-'+ 'x'.repeat(10000),{status:outcome==='token'?401:500});
                response.text=()=>{++window.writeBodiesRead; return Promise.resolve('PRIVATE-WRITE-SENTINEL');};
                return response;
            }
            const response=await nativeFetch(path,options);
            if(!response.ok) throw new Error('Synthetic write fixture was rejected');
            if(outcome==='network') throw new TypeError('PRIVATE-NETWORK-SENTINEL');
            const malformed=new Response('PRIVATE-JSON-SENTINEL',{status:200});
            return malformed;
        };
        window.restoreWrites=()=>{window.fetch=nativeFetch;};
    }""",
        {"editor": editor, "outcome": outcome},
    )
    page.get_by_role("button", name="Save settings", exact=True).click()
    status = page.locator("#status")
    expected = (
        "The installation token was rejected."
        if outcome == "token"
        else "Refresh before saving"
        if outcome == "conflict"
        else "Reload before retrying"
    )
    expect(status).to_contain_text(expected)
    expect(status).not_to_contain_text("PRIVATE-")
    assert page.evaluate("window.writeBodiesRead") == 0
    assert page.evaluate("window.writeAttempts") == 1
    assert page.evaluate("window.writeSignal") is False
    expect(field).to_have_value("64" if editor == "ev" else "180")
    expect(field).to_be_enabled()
    state = page.request.get(url + "/v1/" + editor, headers=headers).json()
    rows = state["vehicles"] if editor == "ev" else state["configuration"]["stores"]
    assert len(rows) == (1 if outcome in {"network", "json"} else 0)
    page.evaluate("window.restoreWrites()")
    if outcome in {"network", "json"}:
        page.get_by_role("button", name="Cancel", exact=True).click()
    else:
        page.get_by_role("button", name="Save settings", exact=True).click()
    saved = page.locator("#dirty" if editor == "ev" else "#thermal-save-state")
    expect(saved).to_have_text("All changes saved.")
    final = page.request.get(url + "/v1/" + editor, headers=headers).json()
    rows = final["vehicles"] if editor == "ev" else final["configuration"]["stores"]
    assert len(rows) == 1
    assert rows[0]["capacity_kwh" if editor == "ev" else "volume_litres"] == (
        64 if editor == "ev" else 180
    )
