"""Exercise supplemental rendering faults, coordinated completion and linked-plan recovery."""

from copy import deepcopy

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("stage", ["evidence", "simulations"])
def test_supplemental_render_errors_never_expose_private_exception_values(app_page, stage):
    """A rendering failure shows canonical retry advice and can recover without publishing its private cause."""
    page, url = app_page
    page.add_init_script("window.failedProjection=" + repr(stage) + ";")
    page.add_init_script("""(() => {
        const text=Object.getOwnPropertyDescriptor(Node.prototype,'textContent');let fail=true;
        Object.defineProperty(Node.prototype,'textContent',{...text,set(value){
            const evidence=window.failedProjection==='evidence'&&this.id==='evidence-period'&&String(value).startsWith('From ');
            const simulation=window.failedProjection==='simulations'&&this.id==='expected-benefit'&&String(value).includes('Not evaluated lifetime savings');
            if(fail&&(evidence||simulation)){fail=false;throw new Error('PRIVATE-PROJECTION-RENDER-SENTINEL');}
            text.set.call(this,value);
        }});
    })();""")
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    if stage == "evidence":
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        page.get_by_text("Understand this plan", exact=True).click()
        status = page.locator("#plan-evidence-status")
        expect(status).to_contain_text("Close and reopen this panel to retry")
    else:
        status = page.locator("#overview-status")
        expect(status).to_contain_text("Use Refresh overview to retry")
    expect(page.locator("body")).not_to_contain_text("PRIVATE-PROJECTION-RENDER-SENTINEL")
    expect(page.locator("#plan-chart-legend")).to_contain_text("Grid import")
    if stage == "evidence":
        page.get_by_text("Understand this plan", exact=True).click()
        page.get_by_text("Understand this plan", exact=True).click()
        expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    else:
        page.get_by_role("button", name="Refresh overview", exact=True).click()
        expect(page.locator("#overview-status")).to_contain_text("Connected")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("disclosure", ["before-load", "during-simulation"])
def test_coordinated_load_waits_for_open_or_already_pending_evidence(app_page, disclosure):
    """Initial loading blocks publication polling until the one shared evidence read and simulations both settle."""
    page, url = app_page
    page.clock.install()
    page.add_init_script("""(() => {
        const interval=window.setInterval,fetchOriginal=window.fetch;
        window.pollReads=0;window.ordinaryReads=[];
        window.setInterval=(callback,delay,...args)=>{
            if(delay===15000)window.pollPlan=callback;
            return interval(callback,delay,...args);
        };
        window.fetch=(path,options)=>{
            const address=new URL(path,location.href);
            if(address.pathname.endsWith('/v1/plans')&&address.searchParams.get('limit')==='1')window.pollReads++;
            const request=fetchOriginal(path,options);
            if(address.searchParams.get('view')!=='evidence'&&!address.pathname.endsWith('/v1/events')){
                window.ordinaryReads.push(request.then(response=>response.clone().arrayBuffer()).catch(()=>{}));
            }
            return request;
        };
    })();""")
    evidence, simulations = [], []
    page.route("**/comparison?view=evidence", lambda route: evidence.append(route))
    page.route("**/comparison?view=simulations", lambda route: simulations.append(route))
    page.goto(url + "/overview")
    if disclosure == "before-load":
        page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Plan loaded. Loading simulations…")
    if disclosure == "during-simulation":
        page.get_by_text("Understand this plan", exact=True).click()
    expect(page.locator("#plan-evidence-status")).to_have_text("Loading original plan evidence…")
    page.get_by_text("Selected interval details", exact=True).click()
    assert len(evidence) == len(simulations) == 1
    response = simulations[0].fetch()
    simulations[0].fulfill(response=response)
    expect(page.locator("#self-consumption-chart-legend")).to_contain_text("SoC")
    page.evaluate("async () => {await Promise.all(window.ordinaryReads);await Promise.resolve();}")
    assert page.evaluate("async () => {await window.pollPlan();return window.pollReads;}") == 0
    assert len(evidence) == 1
    evidence[0].fulfill(response=evidence[0].fetch())
    expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    wait_for_product_reads(page)
    assert page.evaluate("async () => {await window.pollPlan();return window.pollReads;}") == 1


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_linked_plan_success_survives_simulation_failure_and_preserves_retry_selection(app_page):
    """A linked historical plan remains successfully selected despite failed simulations and can be retried independently."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    summary = page.request.get(url + "/v1/plans", headers=headers).json()["plans"][0]
    original_id = summary["plan_id"]
    original = page.request.get(url + f"/v1/plans/{original_id}/comparison", headers=headers).json()
    latest = deepcopy(original)
    latest["plan"].update(plan_id="newer-plan", strategy="minimize consumption")
    page.route(
        "**/v1/plans?**",
        lambda route: route.fulfill(
            json={"plans": [{**summary, "plan_id": "newer-plan"}, summary], "next_before": None}
        ),
    )
    page.route(
        "**/v1/plans/newer-plan/comparison?view=plan",
        lambda route: route.fulfill(json={"plan": latest["plan"]}),
    )
    page.route(
        "**/v1/plans/newer-plan/comparison?view=simulations",
        lambda route: route.fulfill(
            json={
                "plan_id": "newer-plan",
                "scenarios": latest["scenarios"],
                "comparison": latest["comparison"],
            }
        ),
    )
    failed = f"**/v1/plans/{original_id}/comparison?view=simulations"
    page.route(failed, lambda route: route.fulfill(status=503))
    page.goto(url + f"/overview?plan={original_id}#planning")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#plan-version")).to_have_value(original_id)
    expect(page.locator("#overview-status")).to_contain_text(
        "Plan loaded. Simulations are unavailable"
    )
    expect(page.locator("#plan-description")).to_contain_text("Generated:")
    page.unroute(failed)
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-version")).to_have_value(original_id)
    page.locator("#plan-version").select_option("newer-plan")
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-version")).to_have_value("newer-plan")
