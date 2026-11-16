"""Each Planning read owns its deadline without hiding already available advice."""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_slow_listing_does_not_consume_the_plan_or_simulation_deadline(app_page):
    """Four seconds spent listing plans leave five fresh seconds for simulations, whose timeout preserves advice."""
    page, url = app_page
    clock_start = datetime.now(UTC)
    paused_time = clock_start + timedelta(minutes=1)
    page.clock.install(time=clock_start)
    page.clock.pause_at(paused_time)
    page.add_init_script("""(() => {
        const original=window.fetch;
        AbortSignal.timeout=milliseconds=>{
            const controller=new AbortController();
            setTimeout(()=>controller.abort(),milliseconds);return controller.signal;
        };
        window.fetch=(path,options={})=>{
            const address=new URL(path,location.href);
            if(address.pathname.endsWith('/v1/plans')&&address.searchParams.get('limit')==='20') {
                window.listSignal=options.signal;
                return original(path,options).then(async response=>{
                    await new Promise(resolve=>{
                        window.releaseList=resolve;document.documentElement.dataset.listHeld='true';
                    });return response;
                });
            }
            if(address.searchParams.get('view')==='plan')window.planSignal=options.signal;
            if(address.searchParams.get('view')==='simulations') {
                window.simulationSignal=options.signal;
                return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Private timeout details','AbortError')),{once:true}));
            }
            return original(path,options);
        };
    })();""")
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-list-held", "true")
    page.clock.fast_forward(4000)
    page.evaluate("window.releaseList()")
    expect(page.locator("#overview-status")).to_have_text("Plan loaded. Loading simulations…")
    assert page.evaluate(
        "window.planSignal!==window.listSignal&&window.simulationSignal!==window.planSignal"
    )
    page.clock.fast_forward(4999)
    assert page.evaluate("!window.simulationSignal.aborted")
    page.clock.fast_forward(1)
    assert page.evaluate("window.simulationSignal.aborted")
    expect(page.locator("#overview-status")).to_contain_text(
        "Plan loaded. Simulations are unavailable"
    )
    expect(page.locator("#overview-status")).to_have_text(
        "Plan loaded. Simulations are unavailable. Use Refresh overview to retry."
    )
    expect(page.locator("body")).not_to_contain_text("Private timeout details")
    expect(page.locator("#plan-chart-legend")).to_contain_text("Grid import")
    expect(page.locator("#current-price-status")).to_contain_text("Selected plan:")
