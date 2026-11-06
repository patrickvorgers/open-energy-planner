"""Activity is readable and live on desktop and mobile without changing energy configuration."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_module_status_hierarchy_preserves_sources_markets_and_live_states(app_page):
    """Prices and Weather retain independent child and overview states through live recovery and removal."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    prices = page.get_by_role("region", name="Prices", exact=True)
    expect(prices).to_be_visible()
    expect(prices.locator("details")).to_have_count(5)
    for name in ("Nord Pool", "EpexPredictor", "Jeroen.nl"):
        expect(prices.get_by_role("region", name=name, exact=True)).to_be_visible()
    nord_pool = prices.get_by_role("region", name="Nord Pool", exact=True)
    expect(nord_pool.get_by_text("Overview: Unknown", exact=True)).to_be_visible()
    expect(nord_pool.get_by_text("NL: Available", exact=True)).to_be_visible()
    predictor = prices.get_by_role("region", name="EpexPredictor", exact=True)
    expect(predictor.get_by_text("BE: Available", exact=True)).to_be_visible()
    weather = page.get_by_role("region", name="Weather", exact=True)
    for name in ("Overview: Available", "Solar: Degraded", "Temperature: Available"):
        expect(weather.get_by_text(name, exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Demand", exact=True)).to_have_count(0)
    source = predictor.get_by_text("NL: Unavailable", exact=True)
    source.focus()
    source.press("Enter")
    expect(
        predictor.get_by_text(
            "NL forecast unavailable. Published prices remain usable.", exact=True
        )
    ).to_be_visible()
    assert page.request.post(
        url + "/_test/activity-health",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={
            "owner": "prices:epex_predictor:NL",
            "state": "available",
            "detail": "NL forecast recovered.",
        },
    ).ok
    expect(predictor.get_by_text("NL: Available", exact=True)).to_be_visible()
    predictor.get_by_text("NL: Available", exact=True).click()
    expect(predictor.get_by_text("NL forecast recovered.", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_label("Activity type").select_option("training")
    expect(page.locator("#activity-list .activity-row")).to_have_count(1)
    expect(page.locator("#activity-status")).to_have_text("")
    predictor.get_by_text("NL: Available", exact=True).click()
    expect(predictor.get_by_text("NL forecast recovered.", exact=True)).to_be_visible()
    capture = Path("results/ci/activity-module-groups")
    capture.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=str(capture / f"hierarchy-{page.viewport_size['width']}.png"), full_page=True
    )
    for owner in (
        "prices:nord_pool",
        "prices:nord_pool:NL",
        "prices:epex_predictor:NL",
        "prices:epex_predictor:BE",
        "prices:jeroen_nl:NL",
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
