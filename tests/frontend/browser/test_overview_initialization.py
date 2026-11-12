"""Overview initialization contains rendering faults and recovers independently from stalled reads."""

from datetime import UTC, datetime

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    discover,
    open_primary_navigation,
    wait_for_product_reads,
)


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
@pytest.mark.parametrize("newer_failure", ["plan", "period"])
def test_late_savings_connection_cannot_clear_newer_view_or_period_failure(app_page, newer_failure):
    """A held successful connection cannot overwrite a later plan or Savings-period failure."""
    page, url = app_page
    held = []

    def savings_read(route):
        """Retain the first real Savings response and fail only a later selected period."""
        if not held:
            held.append(route)
            page.evaluate("window.savingsConnectionHeld = true")
        else:
            route.fulfill(status=503)

    page.route("**/v1/savings?*", savings_read)
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.wait_for_function("() => window.savingsConnectionHeld === true")
    if newer_failure == "plan":
        page.route("**/comparison?view=plan", lambda route: route.fulfill(status=503))
        open_primary_navigation(page)
        page.get_by_role("link", name="Planning", exact=True).click()
        expect(page.locator("#overview-status")).to_contain_text("503")
    else:
        page.get_by_role("button", name="Show period", exact=True).click()
        expect(page.locator("#coverage")).to_contain_text("503")
    retained_status = page.locator("#overview-status").inner_text()
    held[0].fulfill(response=held[0].fetch())
    wait_for_product_reads(page)
    expect(page.locator("#overview-status")).to_have_text(retained_status)
    if newer_failure == "period":
        expect(page.locator("#coverage")).to_contain_text("503")


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
    if stalled == 2:
        page.wait_for_function("() => window.sessionReads[0]?.completed")
        assert page.evaluate("window.sessionReads.length") == 1
        page.clock.fast_forward(30100)
    page.wait_for_function("() => window.sessionReads.length >= window.stalledSessionRead")
    page.clock.fast_forward(5100)
    page.wait_for_function(
        "() => window.sessionReads[window.stalledSessionRead-1].aborted", timeout=1500
    )
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
    page.wait_for_function("() => window.sessionReads.length > window.stalledSessionRead")
    page.wait_for_function("() => window.sessionReads.at(-1).completed")
    assert page.evaluate("window.sessionReads.every(record=>record.budget===5000)")
    assert page.evaluate(
        "new Set(window.sessionReads.map(record=>record.signal)).size===window.sessionReads.length"
    )


def overview_read_probe(page, stalled):
    """Observe full-view reads separately from financial period slices and stall one real response."""
    page.add_init_script("window.stalledOverviewKind=" + repr(stalled) + ";")
    page.add_init_script("""(() => {
      const fetchOriginal=window.fetch,budgets=new WeakMap();
      window.overviewReads=[];window.overviewPeriodReads=[];window.armOverviewReads=false;let stalled=false;
      AbortSignal.timeout=milliseconds=>{const controller=new AbortController();budgets.set(controller.signal,milliseconds);setTimeout(()=>controller.abort(),milliseconds);return controller.signal;};
      window.fetch=(path,options={})=>{
        const address=new URL(path,location.href);
        const comparison=address.pathname.endsWith('/comparison');
        const start=address.searchParams.get('start'),end=address.searchParams.get('end');
        const periodSlice=comparison&&address.searchParams.size===2&&Number.isFinite(Date.parse(start))&&Date.parse(end)>Date.parse(start);
        if(periodSlice){window.overviewPeriodReads.push({start,end,budget:budgets.get(options.signal)});return fetchOriginal(path,options);}
        if(['simulations','evidence'].includes(address.searchParams.get('view')))return fetchOriginal(path,options);
        const kind=comparison?'plan':address.pathname.endsWith('/v1/savings')?'savings':null;
        if(!kind)return fetchOriginal(path,options);
        const record={kind,signal:options.signal,budget:budgets.get(options.signal),armed:window.armOverviewReads,aborted:false,completed:false};
        window.overviewReads.push(record);
        if(window.armOverviewReads&&!stalled&&kind===window.stalledOverviewKind){
          stalled=true;
          return new Promise((resolve,reject)=>options.signal?.addEventListener('abort',()=>{
            record.aborted=true;reject(new DOMException('PRIVATE-READ-SENTINEL','AbortError'));
          },{once:true}));
        }
        return fetchOriginal(path,options).then(async response=>{await response.clone().arrayBuffer();record.completed=true;return response;});
      };
    })();""")


def test_overview_probe_retains_unexpected_comparison_queries(app_page):
    """Only a valid start/end period slice is separate from full-plan refresh evidence."""
    page, url = app_page
    overview_read_probe(page, "none")
    page.goto(url + "/setup")
    page.evaluate("""async () => {
      const path='/v1/plans/browser-original-plan/comparison';
      await Promise.all([
        '?start=2026-11-10T12:00:00Z&end=2026-11-10T12:15:00Z',
        '?unexpected=1',
        '?start=2026-11-10T12:00:00Z',
        '?start=invalid&end=2026-11-10T12:15:00Z',
        '?start=2026-11-10T12:00:00Z&end=2026-11-10T12:15:00Z&unexpected=1',
      ].map(query=>fetch(path+query)));
    }""")
    assert page.evaluate("window.overviewReads.map(record=>record.kind)") == ["plan"] * 4
    assert page.evaluate("window.overviewPeriodReads.length") == 1


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("stalled", ["plan", "savings"])
@pytest.mark.parametrize(
    "savings_now,browser_now,quarter_start,quarter_end",
    [
        (
            datetime(2026, 11, 11, tzinfo=UTC),
            datetime(2026, 11, 10, 12, 13, 30, tzinfo=UTC),
            "2026-11-10T12:15:00.000Z",
            "2026-11-10T12:30:00.000Z",
        ),
        (
            datetime(2026, 11, 12, tzinfo=UTC),
            datetime(2026, 11, 11, 12, 13, 30, tzinfo=UTC),
            "2026-11-11T12:15:00.000Z",
            "2026-11-11T12:30:00.000Z",
        ),
    ],
    ids=["before-midnight", "after-midnight"],
)
def test_source_name_refresh_bounds_both_views_and_retries_the_revision(
    app_page, stalled, savings_now, browser_now, quarter_start, quarter_end
):
    """Explicitly visit both views, abort either at five seconds and retry names across a UTC-quarter cost refresh."""
    page, url = app_page
    page.clock.install(time=browser_now)
    metadata = discover(page, url)
    overview_read_probe(page, stalled)
    page.goto(url + "/overview#planning")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    open_primary_navigation(page)
    with page.expect_response("**/v1/savings?*"):
        page.get_by_role("link", name="Savings", exact=True).click()
    expect(page.locator("#savings-view")).to_be_visible()
    wait_for_product_reads(page)
    assert page.evaluate(
        "window.overviewReads.filter(record=>!record.armed).every(record=>record.budget===5000)"
    )
    assert page.evaluate("window.overviewReads[0].signal!==window.overviewReads[1].signal")
    page.locator("#period-start").fill("2026-10-01")
    page.locator("#period-end").fill("2026-10-30")
    if stalled == "plan":
        open_primary_navigation(page)
        page.get_by_role("link", name="Planning", exact=True).click()
        expect(page.locator("#planning-view")).to_be_visible()
    page.evaluate("window.armOverviewReads=true")
    next(entry for entry in metadata["entities"] if entry["entity_id"] == "sensor.incoming")[
        "energy_name"
    ] = "Renamed synthetic source"
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": "Bearer " + TOKEN}, data=metadata
    ).ok
    page.clock.fast_forward(31000)
    page.wait_for_function("() => window.overviewReads.some(record=>record.armed)")
    assert page.evaluate("window.overviewReads.filter(record=>record.armed).length") == 1
    open_primary_navigation(page)
    page.get_by_role(
        "link", name="Savings" if stalled == "plan" else "Planning", exact=True
    ).click()
    expect(page.locator("#savings-view" if stalled == "plan" else "#planning-view")).to_be_visible()
    page.wait_for_function("() => window.overviewReads.filter(record=>record.armed).length>=2")
    assert page.evaluate(
        "window.overviewReads.filter(record=>record.armed).every(record=>record.budget===5000&&!record.signal.aborted)"
    )
    page.wait_for_function(
        "() => window.overviewReads.some(record=>record.armed&&record.kind!==window.stalledOverviewKind&&record.completed)"
    )
    page.clock.fast_forward(5100)
    page.wait_for_function("() => window.overviewReads.some(record=>record.aborted)", timeout=1500)
    expect(page.locator("#overview-status" if stalled == "plan" else "#coverage")).to_contain_text(
        "took too long"
    )
    expect(page.locator("body")).not_to_contain_text("PRIVATE-READ-SENTINEL")
    open_primary_navigation(page)
    page.get_by_role(
        "link", name="Planning" if stalled == "plan" else "Savings", exact=True
    ).click()
    expect(page.locator("#planning-view" if stalled == "plan" else "#savings-view")).to_be_visible()
    wait_for_product_reads(page)
    page.clock.fast_forward(31000)
    page.wait_for_function(
        "() => window.overviewReads.filter(record=>record.armed&&record.completed).length>=3"
    )
    wait_for_product_reads(page)
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#coverage")).not_to_contain_text("took too long")
    expect(page.locator("#period-start")).to_have_value("2026-10-01")
    expect(page.locator("#period-end")).to_have_value("2026-10-30")
    assert page.evaluate(
        "() => {const reads=window.overviewReads.filter(record=>record.armed);return new Set(reads.map(record=>record.signal)).size===reads.length;}"
    )
    if stalled == "savings":
        open_primary_navigation(page)
        page.get_by_role("link", name="Planning", exact=True).click()
        expect(page.locator("#planning-view")).to_be_visible()
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        wait_for_product_reads(page)
    count = page.evaluate("window.overviewReads.length")
    page.clock.fast_forward(31000)
    assert page.evaluate("window.overviewReads.length") == count
    assert page.evaluate(
        """expected => window.overviewPeriodReads.some(record=>
          record.start===expected.start&&record.end===expected.end&&record.budget===5000
        )""",
        {"start": quarter_start, "end": quarter_end},
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_manual_plan_selection_has_a_fresh_read_deadline(app_page):
    """A manually selected archived plan times out safely and the same selection can be retried."""
    page, url = app_page
    page.clock.install()
    overview_read_probe(page, "plan")
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    page.evaluate("window.armOverviewReads=true")
    page.locator("#plan-version").select_option("browser-original-plan")
    page.wait_for_function("() => window.overviewReads.some(record=>record.armed)")
    assert page.evaluate("window.overviewReads.at(-1).budget===5000")
    page.clock.fast_forward(5100)
    page.wait_for_function("() => window.overviewReads.at(-1).aborted", timeout=1500)
    expect(page.locator("#overview-status")).to_contain_text("took too long")
    page.locator("#plan-version").select_option("browser-original-plan")
    page.wait_for_function(
        "() => window.overviewReads.at(-1).armed&&window.overviewReads.at(-1).completed"
    )
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    assert page.evaluate("window.overviewReads.at(-1).signal!==window.overviewReads.at(-2).signal")
