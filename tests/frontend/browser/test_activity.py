"""Activity is readable and live on desktop and mobile without changing energy configuration."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover, wait_for_product_reads


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
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    if page.viewport_size["width"] < 500:
        day = page.get_by_label("Day", exact=True).bounding_box()
        kind = page.get_by_label("Activity type").bounding_box()
        assert abs(day["y"] - kind["y"]) < 4
    expect(page.locator("#activity-list")).to_contain_text("Recovered")
    expect(page.locator("#activity-list")).to_contain_text("Running")
    history = page.locator("#activity-list .activity-row")
    capture = Path("results/ci/activity")
    capture.mkdir(parents=True, exist_ok=True)
    first_page = history.evaluate_all("rows => rows.map(row => row.dataset.activityId)")
    identities = set(first_page)
    for page_number, count in ((2, 25), (3, 25), (4, 25), (5, 7)):
        page.get_by_role("button", name="Next", exact=True).click()
        expect(page.locator("#activity-page")).to_have_text(f"Page {page_number}")
        expect(history).to_have_count(count)
        current = history.evaluate_all("rows => rows.map(row => row.dataset.activityId)")
        assert identities.isdisjoint(current)
        identities.update(current)
        if page_number in (2, 5):
            page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            page.screenshot(
                path=str(capture / f"page-{page_number}-{page.viewport_size['width']}.png")
            )
    assert len(identities) == 107
    expect(page.get_by_role("button", name="Next", exact=True)).to_be_disabled()
    for page_number in (4, 3, 2, 1):
        page.get_by_role("button", name="Previous", exact=True).click()
        expect(page.locator("#activity-page")).to_have_text(f"Page {page_number}")
    assert history.evaluate_all("rows => rows.map(row => row.dataset.activityId)") == first_page
    expect(page.get_by_role("button", name="Previous", exact=True)).to_be_disabled()
    row_span = page.locator("#activity-list summary").evaluate_all(
        "rows => {const first = rows.slice(0,8).map(row => row.getBoundingClientRect()); return first.at(-1).bottom - first[0].top;}"
    )
    assert row_span <= (576 if page.viewport_size["width"] < 500 else 400)
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
def test_day_changes_keep_type_filter_and_open_newest_page(app_page):
    """Day changes start at page one of the same type, with the newest records first."""
    from datetime import date, timedelta

    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_label("Activity type").select_option("prices")
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    day = page.get_by_label("Day", exact=True)
    today = day.input_value()
    day.fill((date.fromisoformat(today) - timedelta(days=1)).isoformat())
    expect(page.locator("#activity-page")).to_have_text("Page 1")
    expect(page.get_by_label("Activity type")).to_have_value("prices")
    expect(page.locator("#activity-list strong").first).to_have_text("Earlier-day prices 31")
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    expect(page.locator("#activity-list .activity-row")).to_have_count(7)
    page.get_by_role("button", name="Today", exact=True).click()
    expect(day).to_have_value(today)
    expect(page.get_by_label("Activity type")).to_have_value("prices")
    expect(page.locator("#activity-page")).to_have_text("Page 1")
    expect(page.locator("#activity-list strong").first).to_have_text("Fetch published prices")


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_older_page_receives_its_live_outcome_without_changing_position(app_page):
    """An older execution finishes in place while its open details and focus survive."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_label("Activity type").select_option("prices")
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    document = page.request.get(
        url + "/v1/activities",
        params={"day": page.get_by_label("Day", exact=True).input_value(), "kind": "prices"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    identity = next(row["id"] for row in document["activities"] if row["state"] == "running")
    for page_number in (2, 3):
        page.get_by_role("button", name="Next", exact=True).click()
        expect(page.locator("#activity-page")).to_have_text(f"Page {page_number}")
    details = page.locator(f'#activity-list [data-activity-id="{identity}"]')
    details.locator("summary").focus()
    details.locator("summary").press("Enter")
    expect(details.locator(".activity-state")).to_have_text("Running")
    assert page.request.post(
        url + "/_test/activity-finish",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={"activity_id": identity},
    ).ok
    expect(details.locator(".activity-state")).to_have_text("Completed")
    expect(details.get_by_text("Older activity completed successfully.")).to_be_visible()
    expect(details.locator("summary")).to_be_focused()
    expect(page.locator("#activity-page")).to_have_text("Page 3")


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_failed_navigation_and_late_old_day_response_preserve_selection(app_page):
    """A failed Next retains its page, and an obsolete reply cannot replace another day's page."""
    from datetime import date, timedelta

    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_label("Activity type").select_option("prices")
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    original = page.locator("#activity-list").inner_text()
    page.route("**/v1/activities?**", lambda route: route.fulfill(status=503))
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#activity-status")).to_contain_text("could not be loaded")
    expect(page.locator("#activity-page")).to_have_text("Page 1")
    assert page.locator("#activity-list").inner_text() == original
    page.unroute("**/v1/activities?**")
    held = []

    def hold_older_page(route):
        """Delay only cursor navigation, allowing a new day to load normally."""
        if "before=" in route.request.url:
            held.append(route)
            page.evaluate("window.olderActivityPageHeld = true")
        else:
            route.continue_()

    page.route("**/v1/activities?**", hold_older_page)
    page.get_by_role("button", name="Next", exact=True).click()
    page.wait_for_function("window.olderActivityPageHeld === true")
    expect(page.get_by_role("button", name="Next", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Previous", exact=True)).to_be_disabled()
    response = page.request.get(
        held[0].request.url, headers={"Authorization": f"Bearer {TOKEN}"}
    ).json()
    day = page.get_by_label("Day", exact=True)
    day.fill((date.fromisoformat(day.input_value()) - timedelta(days=1)).isoformat())
    expect(page.locator("#activity-list strong").first).to_have_text("Earlier-day prices 31")
    held[0].fulfill(json=response)
    wait_for_product_reads(page)
    expect(page.locator("#activity-list strong").first).to_have_text("Earlier-day prices 31")
    expect(page.locator("#activity-page")).to_have_text("Page 1")
    expect(page.get_by_label("Activity type")).to_have_value("prices")


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_activity_status_recovers_without_hiding_history_load_errors(app_page):
    """Live and storage recovery clear stale warnings but preserve a failed explicit history read."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
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
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
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
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    older_ids = page.locator("#activity-list .activity-row").evaluate_all(
        "rows => rows.map(row => row.dataset.activityId)"
    )
    assert page.request.post(
        url + "/_test/activity", headers={"Authorization": f"Bearer {TOKEN}"}
    ).ok
    assert page.request.post(
        url + "/_test/activity-health",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={"owner": "weather", "state": "available", "detail": "Paging live update received."},
    ).ok
    expect(page.locator("#activity-health")).to_contain_text("Paging live update received.")
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    assert (
        page.locator("#activity-list .activity-row").evaluate_all(
            "rows => rows.map(row => row.dataset.activityId)"
        )
        == older_ids
    )
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
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
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
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
