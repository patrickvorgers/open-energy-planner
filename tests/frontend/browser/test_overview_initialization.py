"""Overview initialization contains rendering faults and recovers independently from stalled reads."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_initialization_render_error_is_safe_and_explicit_retry_recovers(app_page):
    """A private initialization exception never reaches the UI. Connect retries the failed read/render step."""
    page, url = app_page
    page.add_init_script("""(() => {
      const value=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
      let fail=true;
      Object.defineProperty(HTMLInputElement.prototype,'value',{...value,set(next){
        if(this.id==='period-start'&&fail){fail=false;throw new Error('PRIVATE-INIT-SENTINEL');}
        value.set.call(this,next);
      }});
    })();""")
    page.goto(url + "/overview#savings")
    expect(page.locator("#overview-status")).to_have_text(
        "Cannot initialize this view: Check the connection and retry."
    )
    expect(page.locator("body")).not_to_contain_text("PRIVATE-INIT-SENTINEL")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("stalled", [1, 2], ids=["initial", "name-refresh"])
def test_session_reads_timeout_and_retry_with_fresh_signals(app_page, stalled):
    """Both initial and metadata reads abort at five seconds, then retry with a new live deadline."""
    page, url = app_page
    page.clock.install()
    page.add_init_script(f"window.stalledSessionRead={stalled};")
    page.add_init_script("""(() => {
      const fetchOriginal=window.fetch,budgets=new WeakMap();
      window.sessionReads=[];
      AbortSignal.timeout=milliseconds=>{const controller=new AbortController();budgets.set(controller.signal,milliseconds);setTimeout(()=>controller.abort(),milliseconds);return controller.signal;};
      window.fetch=(path,options={})=>{
        if(typeof path!=='string'||!path.endsWith('setup/session'))return fetchOriginal(path,options);
        const record={signal:options.signal,budget:budgets.get(options.signal),aborted:false};
        window.sessionReads.push(record);
        if(window.sessionReads.length!==window.stalledSessionRead)return fetchOriginal(path,options).then(response=>{record.completed=true;return response;});
        return new Promise((resolve,reject)=>options.signal?.addEventListener('abort',()=>{
          record.aborted=true;reject(new DOMException('PRIVATE-SESSION-SENTINEL','AbortError'));
        },{once:true}));
      };
    })();""")
    page.goto(url + "/overview#savings")
    page.wait_for_function("window.sessionReads.length >= window.stalledSessionRead")
    page.clock.fast_forward(5100)
    page.wait_for_function("window.sessionReads[window.stalledSessionRead-1].aborted", timeout=1500)
    expect(page.locator("body")).not_to_contain_text("PRIVATE-SESSION-SENTINEL")
    if stalled == 1:
        expect(page.locator("#overview-status")).to_contain_text("took too long")
        page.locator("#period-start").fill("2026-10-01")
        page.locator("#period-end").fill("2026-10-30")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    if stalled == 1:
        expect(page.locator("#period-start")).to_have_value("2026-10-01")
        expect(page.locator("#period-end")).to_have_value("2026-10-30")
    if stalled == 2:
        page.clock.fast_forward(31000)
    page.wait_for_function("window.sessionReads.length > window.stalledSessionRead")
    page.wait_for_function("window.sessionReads.at(-1).completed")
    assert page.evaluate("window.sessionReads.every(record=>record.budget===5000)")
    assert page.evaluate(
        "new Set(window.sessionReads.map(record=>record.signal)).size===window.sessionReads.length"
    )
