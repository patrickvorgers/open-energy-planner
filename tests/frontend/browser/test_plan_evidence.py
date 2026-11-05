"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


@pytest.fixture
def automatic_plan_overview(app_page):
    """Serve immutable synthetic plan versions while controlling publication and browser time.

    The seeded plan begins on a UTC quarter boundary. Pause the browser clock 14 minutes
    later, leaving four 15-second publication polls until the next quarter. Older and newer
    versions clone that plan under distinct identities. The newer plan's first buy price is
    0.91. Current prices reuse the seeded tariff intervals with a buy price of 0.3 by default.
    Publication tests change that current price to 0.88.
    """
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    original = page.request.get(url + "/v1/plans?limit=20", headers=headers).json()["plans"][0]
    comparison = page.request.get(
        url + f"/v1/plans/{original['plan_id']}/comparison", headers=headers
    ).json()
    beginning = datetime.fromisoformat(
        comparison["plan"]["input_snapshot"]["problem"]["intervals"][0]["start"]
    )
    older = {**original, "plan_id": "older-plan"}
    newer = {**original, "plan_id": "newly-published-plan", "strategy": "minimize consumption"}
    older_comparison = deepcopy(comparison)
    older_comparison["plan"].update(plan_id=older["plan_id"], strategy="minimize consumption")
    new_comparison = deepcopy(comparison)
    new_comparison["plan"].update(plan_id=newer["plan_id"], strategy=newer["strategy"])
    new_comparison["plan"]["input_snapshot"]["tariff_evidence"][0]["buy_price"] = 0.91
    state = {
        "page": page,
        "beginning": beginning,
        "plans": [original, older],
        "newer": newer,
        "comparisons": {
            original["plan_id"]: comparison,
            older["plan_id"]: older_comparison,
            newer["plan_id"]: new_comparison,
        },
        "comparison_reads": [],
        "failed_plans": set(),
        "failed_prices": False,
        "hold_comparison": False,
        "held_comparisons": [],
        "hold_prices": False,
        "held_prices": [],
        "poll_reads": 0,
        "hold_poll": False,
        "held_polls": [],
        "current_buy_price": 0.3,
    }

    def plan_listing(route):
        """Expose a publication only when the controlled archive makes it available."""
        if "limit=1" in route.request.url and "limit=20" not in route.request.url:
            state["poll_reads"] += 1
            if state["hold_poll"]:
                state["held_polls"].append(route)
                return
            route.fulfill(json={"plans": state["plans"][:1], "next_before": None})
        else:
            route.fulfill(json={"plans": state["plans"], "next_before": None})

    def plan_comparison(route):
        """Count actual detail loads and allow a temporary failure of a newly published plan."""
        plan_id = route.request.url.rsplit("/", 2)[1]
        state["comparison_reads"].append(plan_id)
        if state["hold_comparison"]:
            state["held_comparisons"].append(route)
        elif plan_id in state["failed_plans"]:
            route.fulfill(status=503, body="Temporary archive failure")
        else:
            route.fulfill(json=state["comparisons"][plan_id])

    def current_prices(route):
        """Use independent current prices so a publication must reload both kinds of chart data."""
        if state["hold_prices"]:
            state["held_prices"].append(route)
            return
        if state["failed_prices"]:
            route.fulfill(status=503, body="Temporary price read failure")
            return
        intervals = deepcopy(comparison["plan"]["input_snapshot"]["tariff_evidence"])
        for interval in intervals:
            interval["buy_price"] = state["current_buy_price"]
        route.fulfill(
            json={"as_of": beginning.isoformat(), "currency": "EUR", "intervals": intervals}
        )

    page.clock.install(time=beginning)
    page.clock.pause_at(beginning + timedelta(minutes=14))
    page.add_init_script(
        "window.forceHidden = false; Object.defineProperty(document, 'hidden', {get: () => window.forceHidden});"
    )
    page.route("**/v1/plans?**", plan_listing)
    page.route("**/v1/plans/*/comparison", plan_comparison)
    page.route("**/v1/prices/inputs", current_prices)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#current-price-status")).to_contain_text("Saved contract rates")
    page.wait_for_load_state("networkidle")
    page.evaluate("window.initialPlanChart = Chart.getChart(document.getElementById('plan-chart'))")
    return state


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("refresh", ["publication", "manual"])
def test_plan_refresh_keeps_complete_comparison_until_replacement_arrives(
    automatic_plan_overview, refresh
):
    """A delayed refresh retains the original amounts and evidence until a complete response arrives."""
    state = automatic_plan_overview
    page = state["page"]
    benefit = page.locator("#smart-benefit-value").inner_text()
    costs = page.locator("#optimizer-total-costs").inner_text()
    evidence = page.locator("#plan-evidence").inner_text()
    description = page.locator("#plan-description").inner_text()
    state["hold_comparison"] = True
    if refresh == "publication":
        state["plans"].insert(0, state["newer"])
        page.clock.run_for(15000)
    else:
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#smart-benefit-value")).to_have_text(benefit)
    expect(page.locator("#overview-status")).to_contain_text("Showing the previously loaded plan")
    assert state["held_comparisons"]
    assert page.locator("#optimizer-total-costs").inner_text() == costs
    assert page.locator("#plan-evidence").inner_text() == evidence
    expect(page.locator("#plan-description")).to_have_text(description)
    expect(page.locator("#inspect-time")).to_be_enabled()
    assert page.evaluate(
        "window.initialPlanChart === Chart.getChart(document.getElementById('plan-chart'))"
    )
    page.locator(".optimizer-scenario").screenshot(
        path=f"results/ci/planning-retained-{refresh}-{page.viewport_size['width']}.png"
    )
    page.locator("#overview-status").scroll_into_view_if_needed()
    page.screenshot(
        path=f"results/ci/planning-refresh-status-{refresh}-{page.viewport_size['width']}.png"
    )
    held = state["held_comparisons"].pop()
    plan_id = held.request.url.rsplit("/", 2)[1]
    held.fulfill(json=state["comparisons"][plan_id])
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    if refresh == "publication":
        expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("failed", [False, True])
def test_historical_selection_clears_previous_plan_until_its_own_read_succeeds(
    automatic_plan_overview, failed
):
    """Another selection never presents the previous plan's graphs or tables as its own, even on failure."""
    state = automatic_plan_overview
    page = state["page"]
    state["hold_comparison"] = True
    with page.expect_request("**/v1/plans/older-plan/comparison"):
        page.locator("#plan-version").select_option("older-plan")
    expect(page.locator("#overview-status")).to_have_text("Loading selected plan…")
    expect(page.locator("#plan-description")).to_have_text("Loading selected plan…")
    for rows in ("plan-rows", "plan-battery-rows", "plan-solar-rows", "expected-cost-rows"):
        expect(page.locator(f"#{rows} tr")).to_have_count(0)
    assert page.evaluate("""() => ['plan-chart', 'reference-chart', 'self-consumption-chart',
        'energy-chart', 'forecast-chart', 'price-chart'].every(id =>
        Chart.getChart(document.getElementById(id)).data.datasets.length === 0)""")
    expect(page.locator("#smart-benefit-value")).to_have_text("-")
    expect(page.locator("#inspect-time")).to_be_disabled()
    held = state["held_comparisons"].pop()
    if failed:
        held.fulfill(status=503, body="Temporary archive read failure")
        expect(page.locator("#plan-description")).to_have_text("Selected plan could not be loaded.")
        expect(page.locator("#plan-rows tr")).to_have_count(0)
        assert page.evaluate(
            "Chart.getChart(document.getElementById('plan-chart')).data.datasets.length === 0"
        )
    else:
        held.fulfill(json=state["comparisons"]["older-plan"])
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
        expect(page.locator("#plan-rows tr")).to_have_count(2)


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_current_plan_moves_at_quarter_boundary_without_reloading_data(automatic_plan_overview):
    """Exact quarter rollover clips elapsed advice while an unchanged plan keeps its chart and data."""
    state = automatic_plan_overview
    page, beginning = state["page"], state["beginning"]
    original_reads = list(state["comparison_reads"])
    page.clock.run_for(59999)
    assert page.evaluate("window.initialPlanChart.scales.x.min") == beginning.timestamp() * 1000
    page.clock.run_for(1)
    expected_start = (beginning + timedelta(minutes=15)).timestamp() * 1000
    assert (
        page.evaluate("""() => ['plan-chart', 'reference-chart', 'self-consumption-chart',
        'forecast-chart', 'price-chart', 'current-price-chart'].map(id =>
        Chart.getChart(document.getElementById(id)).scales.x.min)""")
        == [expected_start] * 6
    )
    assert state["comparison_reads"] == original_reads
    assert page.evaluate(
        "window.initialPlanChart === Chart.getChart(document.getElementById('plan-chart'))"
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_completed_publication_refreshes_plan_and_prices_without_manual_refresh(
    automatic_plan_overview,
):
    """A new archive identity replaces current chart values and keeps the chosen six-hour range."""
    state = automatic_plan_overview
    page = state["page"]
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    state["plans"].insert(0, state["newer"])
    state["current_buy_price"] = 0.88
    page.clock.run_for(15000)
    expect(page.locator("#plan-version")).to_have_value("newly-published-plan")
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    page.wait_for_function("""() => Chart.getChart(document.getElementById('current-price-chart'))
        ?.data.datasets[1]?.data[0]?.y === 0.88""")
    assert page.evaluate("""() => ['price-chart', 'current-price-chart'].map(id =>
        Chart.getChart(document.getElementById(id)).data.datasets[1].data[0].y)""") == [0.91, 0.88]
    assert (
        page.evaluate("""() => {
        const graph = Chart.getChart(document.getElementById('plan-chart'));
        return graph.scales.x.max - graph.scales.x.min;
    }""")
        == 6 * 3600000
    )
    expect(page.get_by_role("button", name="Next 6 hours", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.locator("figure").filter(has=page.locator("#plan-chart")).screenshot(
        path=f"results/ci/planning-auto-refresh-{page.viewport_size['width']}.png"
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("plan_view", ["empty", "historical"])
def test_current_prices_recover_independently_of_plan_selection(automatic_plan_overview, plan_view):
    """Retry current prices with an empty archive or historical selection without changing that view."""
    state = automatic_plan_overview
    page = state["page"]
    state["failed_prices"] = True
    if plan_view == "empty":
        state["plans"].clear()
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#current-price-status")).to_have_text("Cannot read this view (503).")
    if plan_view == "historical":
        page.locator("#plan-version").select_option("older-plan")
        expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    else:
        expect(page.locator("#plan-version option")).to_have_count(0)
    page.wait_for_load_state("networkidle")
    comparisons = list(state["comparison_reads"])
    state["failed_prices"] = False
    state["current_buy_price"] = 0.88
    page.clock.run_for(15000)
    page.wait_for_function("""() => Chart.getChart(document.getElementById('current-price-chart'))
        ?.data.datasets[1]?.data[0]?.y === 0.88""")
    expect(page.locator("#current-price-status")).to_contain_text("Saved contract rates")
    assert state["comparison_reads"] == comparisons
    expect(page.locator("#plan-version")).to_have_value(
        "older-plan" if plan_view == "historical" else ""
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("failed_view", ["plan", "prices"])
def test_failed_overview_reload_recovers_without_a_new_publication(
    automatic_plan_overview, failed_view
):
    """Retry the same latest plan or prices after a failed manual reload without redrawing good data."""
    state = automatic_plan_overview
    page = state["page"]
    page.locator("#current-price-chart-legend").get_by_label("Import rate", exact=True).uncheck()
    current = state["plans"][0]["plan_id"]
    state["current_buy_price"] = 0.88
    if failed_view == "plan":
        state["failed_plans"].add(current)
    else:
        state["failed_prices"] = True
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    status = "#overview-status" if failed_view == "plan" else "#current-price-status"
    expect(page.locator(status)).to_have_text(
        "Cannot read this view (503). Showing the previously loaded plan. The update is not available yet."
        if failed_view == "plan"
        else "Cannot read this view (503)."
    )
    if failed_view == "prices":
        page.wait_for_function("""() => window.initialPlanChart !==
            Chart.getChart(document.getElementById('plan-chart'))""")
        page.evaluate(
            "window.reloadedPlanChart = Chart.getChart(document.getElementById('plan-chart'))"
        )
    page.wait_for_load_state("networkidle")
    comparisons = state["comparison_reads"].count(current)
    state["failed_plans"].clear()
    state["failed_prices"] = False
    page.clock.run_for(15000)
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#current-price-status")).to_contain_text("Saved contract rates")
    expect(page.locator("#plan-evidence-status")).not_to_contain_text("could not be loaded")
    expect(page.locator("#plan-version")).to_have_value(current)
    expect(
        page.locator("#current-price-chart-legend").get_by_label("Import rate", exact=True)
    ).not_to_be_checked()
    assert page.evaluate(
        "!Chart.getChart(document.getElementById('current-price-chart')).isDatasetVisible(1)"
    )
    page.wait_for_function("""() => Chart.getChart(document.getElementById('current-price-chart'))
        ?.data.datasets[1]?.data[0]?.y === 0.88""")
    assert state["comparison_reads"].count(current) == comparisons + (failed_view == "plan")
    if failed_view == "prices":
        assert page.evaluate("""window.reloadedPlanChart ===
            Chart.getChart(document.getElementById('plan-chart'))""")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("manual_timing", ["before_poll", "during_poll"])
def test_poll_retry_preserves_an_active_manual_price_read(automatic_plan_overview, manual_timing):
    """A poll cannot replace manual prices while another overview read fails or archive polling resumes."""
    state = automatic_plan_overview
    page = state["page"]
    state["failed_prices"] = True
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#current-price-status")).to_have_text("Cannot read this view (503).")
    page.wait_for_load_state("networkidle")
    if manual_timing == "before_poll":
        page.clock.run_for(14000)
        page.route(
            "**/v1/plans?limit=20",
            lambda route: route.fulfill(status=503, body="Archive unavailable"),
        )
    else:
        state["hold_poll"] = True
        with page.expect_request("**/v1/plans?limit=1"):
            page.clock.run_for(15000)
    state["failed_prices"] = False
    state["hold_prices"] = True
    with page.expect_response("**/v1/plans?limit=20"), page.expect_request("**/v1/prices/inputs"):
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    if manual_timing == "before_poll":
        page.clock.run_for(1000)
        assert state["poll_reads"] == 0
    else:
        with page.expect_event(
            "requestfinished", predicate=lambda request: "limit=1" in request.url
        ):
            state["held_polls"][0].fulfill(json={"plans": state["plans"][:1], "next_before": None})
        page.evaluate("Promise.resolve()")
    assert len(state["held_prices"]) == 1
    current = state["plans"][0]["plan_id"]
    intervals = deepcopy(state["comparisons"][current]["plan"]["input_snapshot"]["tariff_evidence"])
    for interval in intervals:
        interval["buy_price"] = 0.88
    state["held_prices"][0].fulfill(
        json={
            "as_of": state["beginning"].isoformat(),
            "currency": "EUR",
            "intervals": intervals,
        }
    )
    page.wait_for_function("""() => Chart.getChart(document.getElementById('current-price-chart'))
        ?.data.datasets[1]?.data[0]?.y === 0.88""")
    expect(page.locator("#current-price-status")).to_contain_text("Saved contract rates")
    assert len(state["held_prices"]) == 1


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_pending_price_retry_reads_after_publication_detection(automatic_plan_overview):
    """A pending price retry reads the new snapshot after archive detection, once per poll."""
    state = automatic_plan_overview
    page = state["page"]
    state["failed_prices"] = True
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#current-price-status")).to_have_text("Cannot read this view (503).")
    page.wait_for_load_state("networkidle")
    state["failed_prices"] = False
    state["hold_poll"] = True
    page.evaluate("""() => {
        const originalFetch = window.fetch;
        window.priceReads = 0;
        window.fetch = (input, options) => {
            if (String(input).includes('/v1/prices/inputs')) window.priceReads++;
            return originalFetch(input, options);
        };
    }""")
    with page.expect_request("**/v1/plans?limit=1"):
        page.clock.run_for(15000)
    assert page.evaluate("window.priceReads") == 0
    state["plans"].insert(0, state["newer"])
    state["current_buy_price"] = 0.88
    state["held_polls"][0].fulfill(json={"plans": state["plans"][:1], "next_before": None})
    page.wait_for_function("""() => Chart.getChart(document.getElementById('current-price-chart'))
        ?.data.datasets[1]?.data[0]?.y === 0.88""")
    assert page.evaluate("window.priceReads") == 1


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("hung_view", ["listing", "savings", "comparison", "prices"])
def test_stalled_overview_load_cancels_before_publication_poll(automatic_plan_overview, hung_view):
    """A bounded overview load releases polling even when one of its view reads never responds."""
    state = automatic_plan_overview
    page = state["page"]
    pattern = {
        "listing": "**/v1/plans?limit=20",
        "savings": "**/v1/savings?**",
        "comparison": "**/v1/plans/*/comparison",
        "prices": "**/v1/prices/inputs",
    }[hung_view]
    held = []

    def hold_read(route):
        """Keep only the selected overview request open until its deadline cancels it."""
        held.append(route)

    page.route(pattern, hold_read)
    with page.expect_request(pattern):
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    with page.expect_event(
        "requestfailed", predicate=lambda request: request.url == held[0].request.url
    ):
        page.clock.run_for(5001)
    assert len(held) == 1
    held[0].fulfill(status=503, body="Late response after cancellation")
    page.unroute(pattern, hold_read)
    page.wait_for_load_state("networkidle")
    state["plans"].insert(0, state["newer"])
    with page.expect_response("**/v1/plans/newly-published-plan/comparison"):
        page.clock.run_for(15000 - 5001)
    expect(page.locator("#plan-version")).to_have_value("newly-published-plan")
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    expect(page.locator("#overview-status")).to_contain_text("Connected")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_historical_selection_survives_publication_and_quarter_rollover(automatic_plan_overview):
    """New advice refreshes current prices while historical chart data and its time range stay selected."""
    state = automatic_plan_overview
    page = state["page"]
    page.locator("#plan-version").select_option("older-plan")
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    page.locator("#current-price-chart-legend").get_by_label("Import rate", exact=True).uncheck()
    before = page.evaluate("""() => {
        window.historicalChart = Chart.getChart(document.getElementById('plan-chart'));
        return {start: window.historicalChart.scales.x.min,
            data: window.historicalChart.data.datasets.map(dataset => dataset.data)};
    }""")
    original_reads = list(state["comparison_reads"])
    state["plans"].insert(0, state["newer"])
    state["current_buy_price"] = 0.88
    page.clock.run_for(15000)
    page.wait_for_function("""() => Chart.getChart(document.getElementById('current-price-chart'))
        ?.data.datasets[1]?.data[0]?.y === 0.88""")
    page.clock.run_for(45000)
    expect(
        page.locator("#current-price-chart-legend").get_by_label("Import rate", exact=True)
    ).not_to_be_checked()
    assert page.evaluate(
        "!Chart.getChart(document.getElementById('current-price-chart')).isDatasetVisible(1)"
    )
    expect(page.locator("#plan-version option")).to_have_count(3)
    expect(page.locator("#plan-version")).to_have_value("older-plan")
    assert (
        page.evaluate("""() => ({start: window.historicalChart.scales.x.min,
        data: window.historicalChart.data.datasets.map(dataset => dataset.data)})""")
        == before
    )
    assert state["comparison_reads"] == original_reads
    assert page.evaluate(
        "window.historicalChart === Chart.getChart(document.getElementById('plan-chart'))"
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("failed_view", ["plan", "prices"])
def test_failed_new_plan_comparison_retries_without_another_publication(
    automatic_plan_overview, failed_view
):
    """Retry a failed publication read without another identity or unnecessary successful-plan redraw."""
    state = automatic_plan_overview
    page = state["page"]
    state["plans"].insert(0, state["newer"])
    state["current_buy_price"] = 0.88
    if failed_view == "plan":
        state["failed_plans"].add("newly-published-plan")
    else:
        state["failed_prices"] = True
    page.clock.run_for(15000)
    expect(page.locator("#plan-version")).to_have_value("newly-published-plan")
    if failed_view == "plan":
        expect(page.locator("#overview-status")).to_have_text(
            "Cannot read this view (503). Showing the previously loaded plan. The update is not available yet."
        )
        assert page.evaluate(
            "window.initialPlanChart === Chart.getChart(document.getElementById('plan-chart'))"
        )
    else:
        expect(page.locator("#current-price-status")).to_have_text("Cannot read this view (503).")
        expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
        page.evaluate(
            "window.publishedPlanChart = Chart.getChart(document.getElementById('plan-chart'))"
        )
    state["failed_plans"].clear()
    state["failed_prices"] = False
    page.clock.run_for(15000)
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#current-price-status")).to_contain_text("Saved contract rates")
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('current-price-chart')).data.datasets[1].data[0].y"
        )
        == 0.88
    )
    assert state["comparison_reads"].count("newly-published-plan") == (
        2 if failed_view == "plan" else 1
    )
    if failed_view == "prices":
        assert page.evaluate(
            "window.publishedPlanChart === Chart.getChart(document.getElementById('plan-chart'))"
        )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("hung_view", ["comparison", "prices"])
def test_hung_publication_read_times_out_before_the_next_completed_plan(
    automatic_plan_overview, hung_view
):
    """The read deadline cancels a hanging batch so a later completed publication can load."""
    state = automatic_plan_overview
    page = state["page"]
    state["plans"].insert(0, state["newer"])
    state["hold_" + hung_view] = True
    held_path = (
        "/v1/plans/newly-published-plan/comparison"
        if hung_view == "comparison"
        else "/v1/prices/inputs"
    )
    with page.expect_event(
        "requestfailed", predicate=lambda request: request.url.endswith(held_path), timeout=8000
    ) as cancelled:
        # The archive response must start the bounded read before advancing its deadline.
        with page.expect_request("**" + held_path):
            page.clock.run_for(15000)
        page.clock.run_for(5001)
    assert cancelled.value.failure
    assert len(state["held_comparisons" if hung_view == "comparison" else "held_prices"]) == 1
    if hung_view == "comparison":
        expect(page.locator("#overview-status")).not_to_contain_text("Connected")
    else:
        expect(page.locator("#current-price-chart")).to_be_hidden()
    state["held_comparisons" if hung_view == "comparison" else "held_prices"][0].fulfill(
        status=503, body="Late response after cancellation"
    )
    page.wait_for_load_state("networkidle")
    state["hold_" + hung_view] = False
    later = {**state["newer"], "plan_id": "later-completed-plan", "strategy": "minimize cost"}
    details = deepcopy(state["comparisons"]["newly-published-plan"])
    details["plan"].update(plan_id=later["plan_id"], strategy=later["strategy"])
    details["plan"]["input_snapshot"]["tariff_evidence"][0]["buy_price"] = 0.73
    state["comparisons"][later["plan_id"]] = details
    state["plans"].insert(0, later)
    with page.expect_response("**/v1/plans/later-completed-plan/comparison", timeout=8000):
        # Reach the next poll without skipping past that request's new deadline.
        page.clock.run_for(15000 - 5001)
    expect(page.locator("#plan-version")).to_have_value("later-completed-plan")
    expect(page.locator("#plan-description")).to_contain_text("Minimize cost")
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('price-chart')).data.datasets[1].data[0].y"
        )
        == 0.73
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_hidden_page_resumes_once_and_slow_poll_keeps_user_selection(automatic_plan_overview):
    """Hidden views pause polling and one slow resumed request cannot overwrite a later historical choice."""
    state = automatic_plan_overview
    page = state["page"]
    page.evaluate("window.forceHidden = true")
    page.clock.run_for(30000)
    assert state["poll_reads"] == 0
    state["plans"].insert(0, state["newer"])
    state["hold_poll"] = True
    with page.expect_request("**/v1/plans?limit=1"):
        page.evaluate(
            "window.forceHidden = false; document.dispatchEvent(new Event('visibilitychange'))"
        )
    page.clock.run_for(3000)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    assert len(state["held_polls"]) == 1
    assert state["poll_reads"] == 1
    page.locator("#plan-version").select_option("older-plan")
    expect(page.locator("#plan-description")).to_contain_text("Minimize consumption")
    reads = list(state["comparison_reads"])
    state["held_polls"][0].fulfill(json={"plans": state["plans"][:1], "next_before": None})
    expect(page.locator("#plan-version option")).to_have_count(3)
    expect(page.locator("#plan-version")).to_have_value("older-plan")
    assert state["comparison_reads"] == reads


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_cached_page_restore_restarts_one_poll_and_updates_the_time_window(automatic_plan_overview):
    """Page exit stops timers and restoration immediately catches up without duplicate polling intervals."""
    state = automatic_plan_overview
    page = state["page"]
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    page.clock.run_for(900000)
    assert state["poll_reads"] == 0
    assert (
        page.evaluate("window.initialPlanChart.scales.x.min")
        == state["beginning"].timestamp() * 1000
    )
    with page.expect_response("**/v1/plans?limit=1"):
        page.evaluate(
            "window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))"
        )
    expected_start = (state["beginning"] + timedelta(minutes=15)).timestamp() * 1000
    assert page.evaluate("window.initialPlanChart.scales.x.min") == expected_start
    assert state["poll_reads"] == 1
    with page.expect_response("**/v1/plans?limit=1"):
        page.clock.run_for(15000)
    assert state["poll_reads"] == 2


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_future_strategy_choice_shares_measured_soc_and_shows_extra_saving(app_page):
    """Both forecasts spend the same initial 2 kWh. Equal costs show zero extra saving on desktop/mobile."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    document = page.request.get(url + f"/v1/plans/{plan_id}/comparison", headers=headers).json()
    beginning = datetime.fromisoformat(
        document["plan"]["input_snapshot"]["problem"]["intervals"][0]["start"]
    )
    page.clock.install(time=beginning)
    page.clock.pause_at(beginning + timedelta(minutes=1))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#self-consumption-chart")).to_be_visible()
    expect(page.locator("#expected-benefit")).to_contain_text(
        "Self-consumption and OEP start from the same measured SoC and opening bonus budgets."
    )
    expect(page.locator("#expected-benefit")).to_contain_text(
        "The extra expected saving includes the difference in remaining battery value"
    )
    expect(page.locator("#expected-benefit")).not_to_contain_text("provisional")
    expect(page.locator("#self-consumption-issue")).to_be_empty()
    expect(page.locator("#smart-benefit-value")).to_have_text("0.00")
    expect(page.locator("#smart-benefit-label")).to_have_text(
        "No extra expected savings from smart planning"
    )
    expect(page.locator("#optimizer-total-value")).to_have_text("0.00")
    expect(page.locator(".optimizer-scenario .planning-benefit")).to_have_attribute(
        "data-state", "zero"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#expected-benefit").scroll_into_view_if_needed()
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f"results/ci/common-soc-choice-{page.viewport_size['width']}.png")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize(
    "kinds",
    [
        ("published", "forecast"),
        ("forecast", "published"),
        ("published", "published"),
        ("forecast", "forecast"),
        (None, None),
        ("unrecognized", "unrecognized"),
    ],
)
def test_forecast_price_intervals_are_dotted_in_current_and_original_charts(app_page, kinds):
    """All tariff lines use dotted forecasts and solid published rates on desktop/mobile.

    The saved fixture has exactly two 15-minute tariff intervals for the strict
    classification pairing. Its added bonus produces four price datasets: spot,
    import, export and bonus-inclusive export. Each dataset must retain the
    independently specified style of both intervals.
    """
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    comparison = page.request.get(url + f"/v1/plans/{plan_id}/comparison", headers=headers).json()
    snapshot = comparison["plan"]["input_snapshot"]
    tariffs = snapshot["tariff_evidence"]
    page.clock.set_fixed_time(datetime.fromisoformat(tariffs[0]["start"]))
    for tariff, kind in zip(tariffs, kinds, strict=True):
        tariff["price_kind"] = kind
        # Source names must not override explicit published/forecast classification.
        tariff["source_id"] = "nord_pool" if kind == "forecast" else "other_market"
        tariff["total_export_price"] = 0.05
    snapshot["problem"]["export_bonuses"] = [{"rates": [0.05, 0.05]}]
    current = {
        "as_of": tariffs[0]["start"],
        "currency": "EUR",
        "has_bonus": True,
        "coverage": "complete",
        "issue": None,
        "intervals": deepcopy(tariffs),
    }
    page.route("**/v1/plans/*/comparison", lambda route: route.fulfill(json=comparison))
    page.route("**/v1/prices/inputs", lambda route: route.fulfill(json=current))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-chart-legend")).to_contain_text("Export rate incl. bonus")
    expect(page.locator("#current-price-chart-legend")).to_contain_text("before volume cap")

    def rendered_intervals(chart_id):
        """Read the resolved segment styles that Chart.js actually uses to stroke each interval."""
        return page.evaluate(
            """id => {
                const chart = Chart.getChart(document.getElementById(id));
                return chart.data.datasets.map((dataset, index) => {
                    const line = chart.getDatasetMeta(index).dataset;
                    return [0, 2].map(start => {
                        const segment = line.segments.find(segment => segment.start <= start && segment.end >= start + 1);
                        return (segment?.style?.borderDash || line.options.borderDash).length > 0;
                    });
                });
            }""",
            chart_id,
        )

    expected = [[kind == "forecast" for kind in kinds]] * 4
    for chart_id in ("price-chart", "current-price-chart"):
        assert rendered_intervals(chart_id) == expected
        page.locator(f"#{chart_id}-legend").get_by_label("Import rate", exact=True).uncheck()
        page.locator(f"#{chart_id}-legend").get_by_label("Import rate", exact=True).check()
        assert rendered_intervals(chart_id) == expected
    for interval_index, kind in enumerate(kinds):
        page.evaluate(
            """index => {
                const chart = Chart.getChart(document.getElementById('price-chart'));
                chart.tooltip.setActiveElements([{datasetIndex: 0, index: index * 2}], {x: 0, y: 0});
                chart.update('none');
            }""",
            interval_index,
        )
        label = {
            "published": "Published market rate",
            "forecast": "Forecast market rate",
        }.get(kind, "Market rate provenance unclassified")
        expect(page.locator("#price-chart-tooltip")).to_contain_text(label)
    if kinds == ("published", "forecast"):
        page.locator("#current-price-chart").scroll_into_view_if_needed()
        page.screenshot(path=f"results/ci/current-price-lines-{page.viewport_size['width']}.png")
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    assert rendered_intervals("price-chart") == expected
    assert rendered_intervals("current-price-chart") == expected
    for tariff in current["intervals"]:
        tariff["price_kind"] = "published"
    with page.expect_response("**/v1/prices/inputs"):
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    page.wait_for_function("""() => {
        const chart = Chart.getChart(document.getElementById('current-price-chart'));
        return chart.data.datasets.every((dataset, index) =>
            chart.getDatasetMeta(index).dataset.segments.every(segment => !segment.style?.borderDash?.length));
    }""")
    assert rendered_intervals("current-price-chart") == [[False, False]] * 4
    assert rendered_intervals("price-chart") == expected
    if kinds == ("published", "forecast"):
        page.get_by_role("button", name="Full horizon", exact=True).click()
        page.locator("#price-chart").scroll_into_view_if_needed()
        page.screenshot(path=f"results/ci/forecast-price-lines-{page.viewport_size['width']}.png")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_missing_middle_tariff_segment_stays_a_gap_in_all_original_price_series(app_page):
    """Two recorded rates separated by a missing quarter have null breaks, never an invented connecting rate."""
    page, url = app_page

    def missing_quarter(route):
        """Keep exact original endpoint rates while making a deliberately incomplete tariff archive."""
        body = route.fetch().json()
        tariffs = body["plan"]["input_snapshot"]["tariff_evidence"]
        later = deepcopy(tariffs[-1])
        later["start"] = (
            datetime.fromisoformat(later["start"]) + timedelta(minutes=15)
        ).isoformat()
        later["end"] = (datetime.fromisoformat(later["end"]) + timedelta(minutes=15)).isoformat()
        later["price_kind"] = "forecast"
        body["plan"]["input_snapshot"]["tariff_evidence"] = [tariffs[0], later]
        route.fulfill(json=body)

    page.route("**/v1/plans/*/comparison", missing_quarter)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    state = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).data.datasets.map(dataset => dataset.data.map(point => point.y))"
    )
    assert state == [
        [0.1, 0.1, None, None, 0.1, 0.1],
        [0.3, 0.3, None, None, 0.3, 0.3],
        [0, 0, None, None, 0, 0],
    ]


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_partial_first_interval_uses_quarter_axis_without_inventing_prior_energy(app_page):
    """Partial opening and closing quarters keep full widths, explicit coverage and aligned shared time axes."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    identity = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    body = page.request.get(url + f"/v1/plans/{identity}/comparison", headers=headers).json()
    first = body["plan"]["input_snapshot"]["problem"]["intervals"][0]
    beginning = datetime.fromisoformat(first["start"])
    partial = beginning + timedelta(minutes=10)
    first["start"] = partial.isoformat()
    first["duration_h"] = 5 / 60
    body["plan"]["input_snapshot"]["problem"]["intervals"][-1]["duration_h"] = 5 / 60
    body["comparison"] = None
    body["scenarios"] = {}
    page.clock.set_fixed_time(partial)
    page.route("**/v1/plans/*/comparison", lambda route: route.fulfill(json=body))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    state = page.evaluate("""() => ({
        axes: ['plan-chart','reference-chart','self-consumption-chart','forecast-chart','price-chart'].map(id => Chart.getChart(document.getElementById(id)).scales.x.min),
        ends: ['plan-chart','reference-chart','self-consumption-chart','forecast-chart','price-chart'].map(id => Chart.getChart(document.getElementById(id)).scales.x.max),
        first: Chart.getChart(document.getElementById('plan-chart')).data.datasets[0].data[0],
        last: Chart.getChart(document.getElementById('plan-chart')).data.datasets[0].data.at(-1)
    })""")
    assert state["axes"] == [beginning.timestamp() * 1000] * 5
    assert state["first"]["start"] == beginning.timestamp() * 1000
    assert state["first"]["end"] == (beginning + timedelta(minutes=15)).timestamp() * 1000
    assert state["first"]["coveredStart"] == partial.timestamp() * 1000
    assert state["ends"] == [(beginning + timedelta(minutes=30)).timestamp() * 1000] * 5
    assert state["last"]["start"] == (beginning + timedelta(minutes=15)).timestamp() * 1000
    assert state["last"]["end"] == (beginning + timedelta(minutes=30)).timestamp() * 1000
    assert state["last"]["coveredStart"] == (beginning + timedelta(minutes=15)).timestamp() * 1000
    assert state["last"]["coveredEnd"] == (beginning + timedelta(minutes=20)).timestamp() * 1000
    page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('plan-chart'));
        chart.tooltip.setActiveElements([{datasetIndex:0,index:0}],{x:0,y:0});
        chart.update('none');
    }""")
    expect(page.locator("#plan-chart-tooltip")).to_contain_text(
        "Partial quarter: Mean power covers"
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_missing_spot_price_is_a_gap_while_original_effective_prices_remain_visible(app_page):
    """Old plans without wholesale evidence show null spot values and preserve import/export interval prices."""
    page, url = app_page

    def remove_prices(route):
        """Omit historical wholesale evidence without altering the retained physical/effective plan input."""
        response = route.fetch()
        body = response.json()
        body["plan"]["input_snapshot"]["tariff_evidence"] = []
        route.fulfill(json=body)

    page.route("**/v1/plans/*/comparison", remove_prices)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    state = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).data.datasets.map(dataset => dataset.data)"
    )
    assert not state[0] or all(point["y"] is None for point in state[0])
    assert {point["y"] for point in state[1]} == {0.3}
    assert {point["y"] for point in state[2]} == {0}


@pytest.mark.parametrize("app_site", ["savings", "savings_corrected"], indirect=True)
def test_plan_evidence_is_readable_original_and_follows_selected_interval(app_page):
    """Desktop/mobile inspect original .3 prices, 4000-W discharge and 25→0% SoC even after a contract correction."""
    page, url = app_page
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Selected interval details", exact=True).click()
    panel = page.locator("#interval-evidence")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("Discharge power limit reached")
    expect(panel).to_contain_text("25.0 %")
    price_card = panel.locator("article").filter(has_text="Forecast and original prices")
    expect(price_card).to_contain_text("0.30 EUR/kWh")
    expect(price_card).to_contain_text("Published")
    expect(page.locator("#plan-evidence-panel")).to_contain_text(
        "Alternatives have not been evaluated"
    )
    expect(page.locator("#evidence-period")).to_contain_text(
        "All totals below cover this whole period"
    )
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans?limit=20", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    archived_plan = page.request.get(
        url + f"/v1/plans/{plan_id}/comparison", headers=headers
    ).json()["plan"]
    seeded_period_start = datetime.fromisoformat(archived_plan["generated_at"])
    formatted_timestamps = page.evaluate(
        "values => values.map(value => new Date(value).toLocaleString('en-US', {year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC'}))",
        [
            (seeded_period_start + timedelta(minutes=minute_offset)).isoformat()
            for minute_offset in (0, 15, 30)
        ],
    )
    expect(page.locator("#evidence-period")).to_contain_text(
        f"From {formatted_timestamps[0]} to {formatted_timestamps[2]}"
    )
    expect(page.locator("#evidence-time")).to_contain_text(
        f"From {formatted_timestamps[0]} to {formatted_timestamps[1]}"
    )
    expect(page.locator("#evidence-time")).to_contain_text("15 minutes")
    expect(page.locator("#evidence-time")).to_contain_text(
        "Energy quantities apply to this interval"
    )
    expect(page.locator("#forecast-chart-legend")).to_contain_text("Household demand forecast")
    expect(page.locator("#forecast-chart-legend")).to_contain_text("Available solar forecast")
    page.get_by_text("Inputs and assumptions for this plan", exact=True).click()
    expect(page.locator("#plan-evidence")).to_contain_text("Initial stored energy")
    expect(page.locator("#plan-evidence")).to_contain_text(
        "Estimated from a repeated trailing price window."
    )
    page.locator("#inspect-time").fill("1")
    page.locator("#inspect-time").dispatch_event("input")
    expect(panel).to_contain_text("Stored energy ends at the configured reserve")
    expect(panel).to_contain_text("0 %")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_evidence_loading_failure_and_retry_do_not_show_stale_values(app_page):
    """Desktop/mobile preserve loading status on forced slider input, show a real failure, and recover on retry."""
    page, url = app_page
    held = []
    page.route("**/v1/plans/*/comparison", lambda route: held.append(route))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_text("Understand this plan", exact=True).click()
    page.get_by_text("Selected interval details", exact=True).click()
    expect(page.locator("#evidence-issue")).to_contain_text("Loading")
    expect(page.locator("#smart-benefit-value")).to_have_text("-")
    expect(page.locator("#inspect-time")).to_be_disabled()
    page.locator("#inspect-time").dispatch_event("input")
    expect(page.locator("#evidence-issue")).to_contain_text("Loading")
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(100)
    assert held
    held[0].fulfill(status=503, body="Temporary archive read failure")
    expect(page.locator("#evidence-issue")).to_contain_text("could not be loaded")
    expect(page.locator("#plan-evidence-status")).to_contain_text("could not be loaded")
    expect(page.locator("#smart-benefit-value")).to_have_text("-")
    expect(page.locator("#inspect-time")).to_be_disabled()
    page.unroute("**/v1/plans/*/comparison")
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#inspect-time")).to_be_enabled()
    expect(page.locator("#interval-evidence")).to_contain_text("Forecast and original prices")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_same_named_batteries_keep_independent_graph_series(app_page):
    """Duplicate names retain separate 50→25→0 and constant-75 percent SoC curves and fixed/following modes."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    identity = page.request.get(url + "/v1/plans?limit=20", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    body = page.request.get(url + f"/v1/plans/{identity}/comparison", headers=headers).json()
    plan = body["plan"]
    plan["comparison_names"] = {"battery": "Shared battery", "spare": "Shared battery"}
    second = deepcopy(plan["input_snapshot"]["problem"]["batteries"][0])
    second["device_id"] = "spare"
    second["battery"]["initial_kwh"] = 3
    second["can_follow_grid"] = True
    plan["input_snapshot"]["problem"]["batteries"].append(second)
    for flow in plan["expected_flows"]:
        flow["batteries"].append(
            {"device_id": "spare", "charge_kwh": 0, "discharge_kwh": 0, "end_kwh": 3}
        )
    for step in plan["steps"]:
        step["devices"]["spare"] = {"mode": "follow_grid_target", "power_w": None}
    page.route("**/v1/plans/*/comparison", lambda route: route.fulfill(json=body))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-chart-legend")).to_contain_text("Shared battery: 1: SoC")
    expect(page.locator("#plan-chart-legend")).to_contain_text("Shared battery: 2: SoC")
    assert page.evaluate(
        "Chart.getChart(document.getElementById('plan-chart')).data.datasets.filter(dataset => dataset.yAxisID === 'soc').map(dataset => dataset.data.map(point => point.y))"
    ) == [[50, 25, 25, 0], [75, 75, 75, 75]]
    expect(page.locator("#inspect-values")).to_contain_text("Shared battery: 2: Zero on the meter")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_plan_detail_failure_survives_initial_load_and_recovers(app_page):
    """An original-plan detail outage remains visible, and a successful refresh restores the actual comparison."""
    page, url = app_page
    page.route(
        "**/v1/plans/*/comparison", lambda route: route.fulfill(status=503, body="Unavailable")
    )
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text("Cannot read this view (503).")
    expect(page.locator("#smart-benefit-value")).to_have_text("-")
    page.unroute("**/v1/plans/*/comparison")
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-rows tr")).to_have_count(2)


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("late", ["plan", "savings"])
def test_stale_initial_plan_success_cannot_hide_newer_selection_failure(app_page, late):
    """A late initial plan or financial response must not overwrite a newer failed plan selection."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    listing = page.request.get(url + "/v1/plans?limit=20", headers=headers).json()
    original = listing["plans"][0]
    details = page.request.get(
        url + "/v1/plans/" + original["plan_id"] + "/comparison", headers=headers
    ).json()
    listing["plans"].append({**original, "plan_id": "second-plan"})
    financial = page.request.get(url + "/v1/savings", headers=headers).json()
    held = []

    def comparison_route(route):
        """Keep the first request pending while a different plan fails through the normal reader."""
        if "/second-plan/" in route.request.url:
            route.fulfill(status=503, body="Unavailable")
        elif late == "plan":
            held.append(route)
        else:
            route.fulfill(json=details)

    page.route("**/v1/plans?**", lambda route: route.fulfill(json=listing))
    page.route("**/v1/plans/*/comparison", comparison_route)
    if late == "savings":
        page.route("**/v1/savings?**", lambda route: held.append(route))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#plan-version option")).to_have_count(2)
    if late == "savings":
        expect(page.locator("#plan-rows tr")).to_have_count(2)
    page.locator("#plan-version").select_option("second-plan")
    expect(page.locator("#overview-status")).to_have_text("Cannot read this view (503).")
    assert len(held) == 1
    with page.expect_response(
        lambda response_item: (
            response_item.url.endswith("/" + original["plan_id"] + "/comparison")
            if late == "plan"
            else "/v1/savings?" in response_item.url
        )
    ) as completed:
        held[0].fulfill(json=details if late == "plan" else financial)
    completed.value.finished()
    expect(page.locator("#overview-status")).to_have_text("Cannot read this view (503).")


@pytest.mark.parametrize("locale", ["en-US", "nl-NL"])
def test_percentage_display_preserves_precise_edits_and_saved_state(app_page, browser, locale):
    """Desktop/mobile show one locale decimal, retain unknowns and preserve exact percentage drafts across focus and blur."""
    original_page, url = app_page
    context = browser.new_context(locale=locale, viewport=original_page.viewport_size)
    try:
        page = context.new_page()
        page.goto(url + "/contract")
        formatted = page.evaluate("""async () => {
            const display = await import('/setup/display.js');
            const {SaveBar} = await import('/setup/save-bar.js');
            const form = document.createElement('form'); form.id = 'percentage-test';
            const input = document.createElement('input'); input.type = 'number';
            input.step = 'any'; input.min = '0'; input.max = '100'; input.id = 'percentage-input';
            form.append(input); document.body.append(form);
            const bar = document.createElement('div'); bar.id = 'percentage-bar';
            for (const id of ['percentage-save', 'percentage-discard', 'percentage-state']) {
                const element = document.createElement(id.endsWith('state') ? 'span' : 'button');
                element.id = id; bar.append(element);
            }
            document.body.append(bar); display.percentageInput(input, 12.3456);
            const state = new SaveBar({form: form.id, bar: bar.id, save: 'percentage-save',
                discard: 'percentage-discard', status: 'percentage-state'});
            state.loaded(); window.percentageTest = {display, state};
            return [0, 50, 12.3456, 12.35, null, NaN, Infinity].map(value => display.percentage(value));
        }""")
        decimal = "," if locale == "nl-NL" else "."
        assert formatted == [
            f"0{decimal}0%",
            f"50{decimal}0%",
            f"12{decimal}3%",
            f"12{decimal}4%",
            "Unavailable",
            "Unavailable",
            "Unavailable",
        ]
        field = page.locator("#percentage-input")
        expect(field).to_have_value("12.3")
        field.focus()
        expect(field).to_have_value("12.3456")
        field.press("Tab")
        expect(field).to_have_value("12.3")
        assert page.evaluate("window.percentageTest.state.dirty") is False
        assert (
            page.evaluate(
                "window.percentageTest.display.inputNumber(document.getElementById('percentage-input'))"
            )
            == 12.3456
        )
        field.fill("8.7654")
        field.press("Tab")
        expect(field).to_have_value("8.8")
        assert page.evaluate("window.percentageTest.state.dirty") is True
        assert (
            page.evaluate(
                "window.percentageTest.display.inputNumber(document.getElementById('percentage-input'))"
            )
            == 8.7654
        )
        field.fill("12.35")
        field.press("Tab")
        expect(field).to_have_value("12.4")
        assert (
            page.evaluate(
                "window.percentageTest.display.inputNumber(document.getElementById('percentage-input'))"
            )
            == 12.35
        )
        field.fill("")
        field.press("Tab")
        expect(field).to_have_value("")
        assert (
            page.evaluate(
                "window.percentageTest.display.inputNumber(document.getElementById('percentage-input'))"
            )
            is None
        )
        field.fill("-0.04")
        field.press("Tab")
        expect(field).to_have_value("-0.04")
        assert field.evaluate("input => input.validity.rangeUnderflow") is True
        field.fill("100.04")
        field.press("Tab")
        expect(field).to_have_value("100.04")
        assert field.evaluate("input => input.validity.rangeOverflow") is True
        assert (
            page.evaluate(
                "window.percentageTest.display.inputNumber(document.getElementById('percentage-input'))"
            )
            is None
        )
    finally:
        context.close()


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_current_price_click_does_not_open_original_plan_evidence(app_page):
    """Click a current tariff inside the second archived plan interval. The original inspector must retain its first selection."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    plan_id = page.request.get(url + "/v1/plans", headers=headers).json()["plans"][0]["plan_id"]
    plan = page.request.get(url + f"/v1/plans/{plan_id}", headers=headers).json()
    plan_intervals = plan["input_snapshot"]["problem"]["intervals"][:2]
    page.clock.set_fixed_time(datetime.fromisoformat(plan_intervals[0]["start"]))
    rows = [
        {
            "start": interval["start"],
            "end": (
                datetime.fromisoformat(interval["start"]) + timedelta(hours=interval["duration_h"])
            ).isoformat(),
            "market_price": 0.10,
            "buy_price": 0.20,
            "sell_price": 0.08,
            "total_export_price": 0.11,
            "bonus_issue": None,
        }
        for interval in plan_intervals
    ]
    page.route(
        "**/v1/prices/inputs",
        lambda route: route.fulfill(
            json={
                "as_of": plan_intervals[0]["start"],
                "currency": "EUR",
                "has_bonus": True,
                "coverage": "complete",
                "intervals": rows,
                "issue": None,
            }
        ),
    )
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-chart-legend")).to_contain_text("Import rate")
    expect(page.locator("#current-price-chart-legend")).to_contain_text("before volume cap")
    inspector = page.locator("#inspect-time")
    before = inspector.input_value()
    point = page.evaluate("""() => {
        const chart=Chart.getChart(document.getElementById('current-price-chart'));
        const point=chart.getDatasetMeta(0).data[2];
        return {x:point.x,y:point.y};
    }""")
    page.locator("#current-price-chart").click(position=point)
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert inspector.input_value() == before
    assert page.locator("#interval-evidence-panel").get_attribute("open") is None


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_financial_reference_issue_keeps_physical_bars_visible(app_page):
    """Desktop/mobile retain reference bars and SoC while the missing bonus baseline hides costs and benefit."""
    page, url = app_page

    def unknown_budget(route):
        """Keep the API's original physical evidence while withholding financial comparison values."""
        body = route.fetch().json()
        reference = body["scenarios"]["self_consumption"]
        reference["issue"] = "Continuous reference bonus state is missing"
        reference["comparison"]["reference_costs"] = None
        reference["comparison"]["expected_savings"] = None
        reference.pop("expected_savings_vs_without_battery", None)
        route.fulfill(json=body)

    page.route("**/v1/plans/*/comparison", unknown_budget)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#self-consumption-chart")).to_be_visible()
    expect(page.locator("#self-consumption-chart-legend")).to_contain_text("SoC")
    expect(page.locator("#self-consumption-costs")).to_contain_text("Expected costs unavailable")
    expect(page.locator("#self-consumption-issue")).to_have_text(
        "Continuous reference bonus state is missing"
    )
    assert page.evaluate("""() => {
        const graph = Chart.getChart(document.getElementById('self-consumption-chart'));
        return graph.data.datasets.some(series => series.type === 'bar' && series.data.some(point => point.y !== null));
    }""")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
