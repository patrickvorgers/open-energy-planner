"""Activity is readable and live on desktop and mobile without changing energy configuration."""

from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover, wait_for_product_reads


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_activity_titles_show_source_context_without_wrapping(app_page):
    """Recognize price sources on one line and read the complete title through keyboard disclosure."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list summary strong").filter(has_text="Jeroen.nl")).to_have_text(
        "Fetch price forecast: Jeroen.nl"
    )
    expect(
        page.locator("#activity-list summary strong").filter(has_text="Nord Pool").first
    ).to_have_text("Fetch published prices: Nord Pool")
    full_title = "Fetch price forecast for the next planning horizon and retain previously accepted prices: EpexPredictor"
    long_title = page.locator("#activity-list summary strong").filter(has_text="EpexPredictor")
    expect(long_title).to_have_text(full_title)
    expect(long_title).to_have_attribute("title", full_title)
    assert long_title.bounding_box()["height"] < 30
    assert long_title.evaluate("element => element.scrollWidth > element.clientWidth")
    expect(
        page.locator("#activity-list summary strong").filter(has_text="Fetch weather").first
    ).to_have_text("Fetch weather")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    directory = Path("results/ci/activity-context")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / f"history-{page.viewport_size['width']}.png"))
    details = long_title.locator("xpath=../..")
    details.locator("summary").focus()
    page.keyboard.press("Enter")
    expect(details.locator("p").first).to_have_text(full_title)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_activity_subjects_and_failed_plan_periods_remain_readable(app_page):
    """Render captured subjects inertly and distinguish financial failures in HA time across DST."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(100)
    assert page.request.post(
        url + "/_test/activity-contexts", headers={"Authorization": f"Bearer {TOKEN}"}
    ).ok
    for title in (
        "Update consumption forecast: Household consumption",
        "Update solar forecast: East roof, West roof",
        "Learn appliance cycles: Laundry <img src=x>",
        "Compare forecast models: East roof",
        "Archive daily forecast: 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM",
        "Calculate energy plan: 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM",
        "Evaluate measured savings: 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM",
    ):
        label = page.locator("#activity-list summary strong").get_by_text(title, exact=True)
        expect(label).to_have_attribute("title", title)
        assert label.bounding_box()["height"] < 30
    expect(page.locator("#activity-list img")).to_have_count(0)
    expect(page.locator("#activity-upcoming")).to_contain_text(
        "Update solar forecast: East roof, West roof"
    )
    page.get_by_label("Activity type").select_option("settlement")
    expect(page.locator("#activity-list .activity-row")).to_have_count(3)
    failed = page.locator("#activity-list details").filter(has_text="Recalculate plan value")
    expect(failed).to_have_count(2)
    expect(failed.locator("[data-state=failed]")).to_have_count(2)
    assert len(set(failed.locator("summary strong").all_text_contents())) == 2
    assert set(failed.locator("summary strong").all_text_contents()) == {
        "Recalculate plan value: Plan 10/24/2026, 12:00 PM: Minimize cost, 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM",
        "Recalculate plan value: Plan 10/24/2026, 12:15 PM: Minimize cost, 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM",
    }
    assert len(set(failed.evaluate_all("items => items.map(item => item.dataset.activityId)"))) == 2
    first = failed.filter(has_text="10/24/2026, 12:15 PM")
    first.locator("summary").focus()
    page.keyboard.press("Enter")
    expect(first).to_have_attribute("open", "")
    expect(first).to_contain_text("does not change the active energy plan or its device commands")
    expect(first).to_contain_text("under Planning")
    expect(first).to_contain_text("review Contract settings")
    expect(first).to_contain_text("report this activity for investigation")
    expect(first.get_by_role("link", name="View this plan")).to_have_attribute(
        "href", f"{url}/overview?plan=example-plan-1#planning"
    )
    directory = Path("results/ci/activity-context")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / f"failures-{page.viewport_size['width']}.png"))
    first.locator("summary").click()
    waiting = page.locator("#activity-list details").filter(has_text="Evaluate measured savings")
    waiting.locator("summary").click()
    expect(waiting).to_contain_text("processed 1 of 2 measured intervals")
    expect(waiting).to_contain_text("Published electricity prices are missing")
    expect(waiting).to_contain_text(
        "Processing stopped at: 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM"
    )
    expect(waiting.get_by_role("link", name="View savings")).to_have_attribute(
        "href", f"{url}/overview#savings"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(directory / f"savings-waiting-{page.viewport_size['width']}.png"))
    waiting.screenshot(
        path=str(directory / f"savings-waiting-detail-{page.viewport_size['width']}.png")
    )
    page.get_by_label("Activity type").select_option("")
    expect(page.locator("#activity-list .activity-row")).to_have_count(100)
    page.screenshot(path=str(directory / f"modules-{page.viewport_size['width']}.png"))


@pytest.mark.parametrize("app_site", ["activity_plans", "activity_ingress"], indirect=True)
@pytest.mark.parametrize("selection", ["listed", "older", "missing"])
def test_activity_plan_link_reads_exact_archive_without_mutation_or_fallback(app_page, selection):
    """Open the exact saved plan through native or ingress navigation, including missing and older archives."""
    page, url = app_page
    discover(page, url, devices=True)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    original = page.request.get(url + "/v1/plans/browser-original-plan", headers=headers).json()
    plan = original.get("plan", original)
    identity = "missing-plan" if selection == "missing" else "browser-original-plan"
    assert page.request.post(
        url + "/_test/activity-plan",
        headers=headers,
        data={
            "plan_id": identity,
            "plan_generated_at": plan["generated_at"],
            "plan_strategy": plan["strategy"],
        },
    ).ok
    page.clock.install()
    page.goto(url + "/activity?embedded=1")
    if "/api/hassio_ingress/" not in url:
        expect(page.locator("#connect")).to_be_visible()
        page.get_by_label("Installation API token").fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    row = page.locator("#activity-list details").filter(has_text="Recalculate plan value")
    expect(row).to_have_count(1)
    row.locator("summary").click()
    writes, comparisons = [], []
    page.on(
        "request",
        lambda request: (
            writes.append(request.url) if request.method not in ("GET", "HEAD") else None
        ),
    )
    page.on(
        "request",
        lambda request: comparisons.append(request.url) if "/comparison" in request.url else None,
    )
    if selection == "older":
        page.route(
            "**/v1/plans?limit=20",
            lambda route: route.fulfill(json={"plans": [], "next_before": None}),
        )
    row.get_by_role("link", name="View this plan").click()
    assert page.url == f"{url}/overview?plan={identity}&embedded=1#planning"
    if "/api/hassio_ingress/" not in url:
        expect(page.locator("#overview-connect")).to_be_visible()
        expect(page.locator("#overview-status")).to_have_text(
            "Connect to view your plans and savings."
        )
        page.get_by_label("Installation API token").fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#plan-version")).to_have_value(identity)
    if selection == "missing":
        expect(page.locator("#overview-status")).to_contain_text(
            "The linked plan could not be loaded"
        )
        expect(page.locator("#plan-description")).to_have_text("Selected plan could not be loaded.")
    else:
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        expect(page.locator("#plan-description")).to_contain_text("Generated:")
        expect(page.locator("#plan-version option:checked")).not_to_have_text(
            "Selected archived plan"
        )
    page.clock.run_for(15001)
    wait_for_product_reads(page)
    expect(page.locator("#plan-version")).to_have_value(identity)
    if selection != "missing":
        assert (
            page.evaluate(
                "Chart.getChart(document.getElementById('plan-chart')).options.scales.x.min"
            )
            == datetime.fromisoformat(plan["steps"][0]["start"]).timestamp() * 1000
        )
    assert comparisons and all(f"/{identity}/comparison" in request for request in comparisons)
    assert writes == []
    assert (
        page.request.get(url + "/v1/plans/browser-original-plan", headers=headers).json()
        == original
    )


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
@pytest.mark.parametrize(
    "title,action,destination,heading",
    [
        ("Compare forecast models", "View forecast quality", "quality", "Forecast quality"),
        ("Learn appliance cycles", "View appliance profiles", "appliances", "Appliances"),
        ("Evaluate measured savings", "View savings", "overview#savings", "Savings"),
        (
            "Fetch price forecast: Jeroen.nl",
            "View planning and prices",
            "overview#planning",
            "Planning",
        ),
    ],
)
def test_activity_data_links_open_the_named_read_only_page(
    app_page, title, action, destination, heading
):
    """Related activities navigate to the named real data screen without issuing a domain mutation."""
    page, url = app_page
    discover(page, url)
    assert page.request.post(
        url + "/_test/activity-contexts", headers={"Authorization": f"Bearer {TOKEN}"}
    ).ok
    page.goto(url + "/activity")
    expect(page.locator("#connect")).to_be_visible()
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    row = (
        page.locator("#activity-list details")
        .filter(has=page.locator("summary strong").filter(has_text=title))
        .first
    )
    row.locator("summary").click()
    writes = []
    page.on(
        "request",
        lambda request: (
            writes.append(request.url) if request.method not in ("GET", "HEAD") else None
        ),
    )
    row.get_by_role("link", name=action).click()
    assert page.url == f"{url}/{destination}"
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    assert writes == []


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_module_status_hierarchy_preserves_collapsed_groups_and_live_states(app_page):
    """Prices hide the single market, and keyboard groups retain collapse and focus through live recovery."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    prices = page.get_by_role("region", name="Prices", exact=True)
    expect(prices).to_be_visible()
    expect(prices.locator("details[data-owner]")).to_have_count(3)
    for name in ("Nord Pool: Available", "EpexPredictor: Unavailable", "Jeroen.nl: Available"):
        expect(prices.get_by_text(name, exact=True)).to_be_visible()
    for summary in prices.locator("summary").all_text_contents():
        assert ": NL" not in summary and ": BE" not in summary
    weather = page.get_by_role("region", name="Weather", exact=True)
    for name in ("Overview: Available", "Solar: Degraded", "Temperature: Available"):
        expect(weather.get_by_text(name, exact=True)).to_be_visible()
    weather_heading = weather.locator(":scope > details[data-module-group] > summary")
    weather_heading.click()
    expect(weather.get_by_text("Solar: Degraded", exact=True)).not_to_be_visible()
    weather_heading.click()
    expect(page.get_by_role("heading", name="Demand", exact=True)).to_have_count(0)
    source = prices.get_by_text("EpexPredictor: Unavailable", exact=True)
    source.focus()
    source.press("Enter")
    expect(
        prices.get_by_text("NL forecast unavailable. Published prices remain usable.", exact=True)
    ).to_be_visible()
    price_group = prices.locator(":scope > details[data-module-group]")
    price_heading = price_group.locator(":scope > summary")
    price_heading.focus()
    price_heading.press("Enter")
    expect(source).not_to_be_visible()
    assert page.request.post(
        url + "/_test/activity-health",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={
            "owner": "prices:epex_predictor:NL",
            "state": "available",
            "detail": "NL forecast recovered.",
        },
    ).ok
    expect(prices.get_by_text("EpexPredictor: Available", exact=True)).to_have_count(1)
    expect(price_group).not_to_have_attribute("open", "")
    expect(price_heading).to_be_focused()
    price_heading.press("Enter")
    prices.get_by_text("EpexPredictor: Available", exact=True).click()
    expect(prices.get_by_text("NL forecast recovered.", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_label("Activity type").select_option("training")
    expect(page.locator("#activity-list .activity-row")).to_have_count(1)
    expect(page.locator("#activity-status")).to_have_text("")
    prices.get_by_text("EpexPredictor: Available", exact=True).click()
    expect(prices.get_by_text("NL forecast recovered.", exact=True)).to_be_visible()
    # Switch the one configured market only after withdrawing all previous-area producers.
    for adapter in ("nord_pool", "epex_predictor", "jeroen_nl"):
        assert page.request.post(
            url + "/_test/activity-health",
            headers={"Authorization": f"Bearer {TOKEN}"},
            data={"owner": f"prices:{adapter}:NL"},
        ).ok
    for adapter in ("nord_pool", "epex_predictor", "jeroen_nl"):
        assert page.request.post(
            url + "/_test/activity-health",
            headers={"Authorization": f"Bearer {TOKEN}"},
            data={
                "owner": f"prices:{adapter}:BE",
                "state": "available",
                "detail": "Current market prices are usable.",
            },
        ).ok
    expect(prices.locator("details[data-owner]")).to_have_count(3)
    for name in ("Nord Pool: Available", "EpexPredictor: Available", "Jeroen.nl: Available"):
        expect(prices.get_by_text(name, exact=True)).to_be_visible()
    expect(prices).not_to_contain_text("NL forecast recovered.")
    for summary in prices.locator("summary").all_text_contents():
        assert ": NL" not in summary and ": BE" not in summary
    capture = Path("results/ci/activity-module-groups")
    capture.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=str(capture / f"hierarchy-{page.viewport_size['width']}.png"), full_page=True
    )
    price_heading.focus()
    price_heading.press("Enter")
    expect(prices.get_by_text("Jeroen.nl: Available", exact=True)).not_to_be_visible()
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=str(capture / f"collapsed-{page.viewport_size['width']}.png"), full_page=True
    )
    price_heading.press("Enter")
    for owner in (
        "prices:nord_pool",
        "prices:nord_pool:NL",
        "prices:nord_pool:BE",
        "prices:epex_predictor:NL",
        "prices:epex_predictor:BE",
        "prices:jeroen_nl:NL",
        "prices:jeroen_nl:BE",
    ):
        assert page.request.post(
            url + "/_test/activity-health",
            headers={"Authorization": f"Bearer {TOKEN}"},
            data={"owner": owner},
        ).ok
    expect(prices).to_have_count(0)
    expect(weather.get_by_text("Solar: Degraded", exact=True)).to_be_visible()
    assert page.request.post(
        url + "/_test/activity-health",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={"owner": "solar-weather"},
    ).ok
    assert page.request.post(
        url + "/_test/activity-health",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={"owner": "temperature-weather"},
    ).ok
    expect(weather).to_have_count(0)
    expect(page.locator("#activity-health")).to_contain_text("Weather: Available")


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_activity_filter_pagination_live_updates_and_keyboard_details(app_page):
    """Exercise real authenticated history, filters, cursor updates and accessible disclosure."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(100)
    if page.viewport_size["width"] < 500:
        day = page.get_by_label("Day", exact=True).bounding_box()
        kind = page.get_by_label("Activity type").bounding_box()
        assert abs(day["y"] - kind["y"]) < 4
    expect(page.locator("#activity-list")).to_contain_text("Recovered")
    expect(page.locator("#activity-list")).to_contain_text("Running")
    page.get_by_role("button", name="Load earlier activities").click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(107)
    expect(
        page.get_by_role("list", name="Recorded activities").get_by_role("listitem")
    ).to_have_count(107)
    row_span = page.locator("#activity-list summary").evaluate_all(
        "rows => {const first = rows.slice(0,8).map(row => row.getBoundingClientRect()); return first.at(-1).bottom - first[0].top;}"
    )
    assert row_span <= (576 if page.viewport_size["width"] < 500 else 400)
    capture = Path("results/ci/activity")
    capture.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(path=str(capture / f"dense-{page.viewport_size['width']}.png"), full_page=False)
    page.get_by_label("Activity type").select_option("weather")
    expect(page.locator("#activity-list .activity-row")).to_have_count(2)
    details = page.locator("#activity-list details").filter(has_text="Recovered")
    details.locator("summary").focus()
    page.keyboard.press("Enter")
    expect(details.locator("p").first).to_be_visible()
    expect(page.locator("#activity-upcoming")).to_contain_text("Fetch weather")
    response = page.request.post(
        url + "/_test/activity", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.ok
    expect(page.locator("#activity-list")).to_contain_text("Live weather refresh")
    expect(page.locator("#activity-list .activity-row")).to_have_count(3)
    expect(details.locator("p").first).to_be_visible()
    expect(details.locator("summary")).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    expect(page.get_by_role("heading", name="Activity", exact=True)).to_be_in_viewport()
    page.screenshot(
        path=str(capture / f"weather-{page.viewport_size['width']}.png"), full_page=True
    )
    page.get_by_label("Activity type").select_option("training")
    expect(page.locator("#activity-list .activity-row")).to_have_count(1)
    page.get_by_label("Day", exact=True).fill("2020-01-01")
    expect(page.locator("#activity-list")).to_contain_text("No activities recorded")
    page.get_by_role("button", name="Today", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(1)


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_activity_status_recovers_without_hiding_history_load_errors(app_page):
    """Live and storage recovery clear stale warnings but preserve a failed explicit history read."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(100)
    status = page.locator("#activity-status")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = page.request.post(
        url + "/_test/activity-history", headers=headers, data={"available": False}
    )
    assert response.ok
    expect(status).to_contain_text("cannot currently be saved")
    response = page.request.post(
        url + "/_test/activity-history", headers=headers, data={"available": True}
    )
    assert response.ok
    expect(status).to_have_text("")

    disconnected = [True]

    def connection(route):
        """Interrupt the transport while retaining the real server and replay protocol on recovery."""
        if disconnected[0]:
            route.abort()
        else:
            route.continue_()

    page.route("**/v1/events**", connection)
    # Wake the existing long poll so the next request encounters the transport outage.
    assert page.request.post(url + "/_test/activity", headers=headers).ok
    expect(status).to_contain_text("Live updates unavailable", timeout=10000)
    disconnected[0] = False
    expect(status).to_have_text("", timeout=10000)
    page.route("**/v1/activities?**", lambda route: route.abort())
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(status).to_contain_text("Activity history could not be loaded")
    assert page.request.post(url + "/_test/activity", headers=headers).ok
    expect(page.locator("#activity-list")).to_contain_text("Live weather refresh")
    expect(status).to_contain_text("Activity history could not be loaded")
    page.unroute("**/v1/activities?**")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(status).to_have_text("")


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_activity_keeps_live_history_bounded_and_later_schedules_visible(app_page):
    """Future deadlines enter the six-hour window, and trimmed live records remain pageable."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(100)
    expect(page.locator("#activity-upcoming")).not_to_contain_text("Later model training")
    page.clock.install()
    page.clock.fast_forward(2 * 60 * 60 * 1000)
    expect(page.locator("#activity-upcoming")).to_contain_text("Later model training")
    page.get_by_label("Activity type").select_option("weather")
    expect(page.locator("#activity-list .activity-row")).to_have_count(2)
    assert page.request.post(
        url + "/_test/activity-burst", headers={"Authorization": f"Bearer {TOKEN}"}
    ).ok
    expect(page.locator("#activity-list")).to_contain_text("Weather burst 149")
    expect(page.locator("#activity-list .activity-row")).to_have_count(100)
    page.get_by_role("button", name="Load earlier activities").click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(152)
    page.get_by_label("Activity type").select_option("training")
    expect(page.locator("#activity-list .activity-row")).to_have_count(1)
    assert page.request.post(
        url + "/_test/activity-burst", headers={"Authorization": f"Bearer {TOKEN}"}
    ).ok
    expect(page.locator("#activity-list .activity-row")).to_have_count(1)


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_activity_refresh_is_single_and_parser_errors_are_safe(app_page):
    """Repeated Refresh cannot overlap, and invalid private JSON never becomes visible error text."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(100)
    requests = []

    def intercept(route):
        """Signal only after the actual paused request has been captured by this handler."""
        requests.append(route)
        page.evaluate("window.activityHistoryRequestCaptured = true")

    page.route("**/v1/activities?**", intercept)
    refresh = page.get_by_role("button", name="Refresh", exact=True)
    refresh.click()
    expect(refresh).to_be_disabled()
    page.wait_for_function("window.activityHistoryRequestCaptured === true")
    page.evaluate("document.getElementById('activity-refresh').click()")
    assert len(requests) == 1
    requests[0].fulfill(
        status=200, content_type="application/json", body="PRIVATE_INVALID_JSON_RESPONSE"
    )
    expect(page.locator("#activity-status")).to_contain_text("Activity history could not be loaded")
    assert "PRIVATE_INVALID_JSON_RESPONSE" not in page.locator("body").inner_text()
    expect(refresh).to_be_enabled()
    page.unroute("**/v1/activities?**")
    refresh.click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(100)
