"""Activity is readable and live on desktop and mobile without changing energy configuration."""

from datetime import datetime
from itertools import pairwise
from math import isfinite
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.activity_fixture import ACTIVITY_SCENARIO_TIME
from tests.frontend.browser.helpers import TOKEN, discover, wait_for_product_reads


@pytest.fixture
def app_page(app_page):
    """Keep browser dates aligned with the Activity publisher and its seeded local day."""
    page, url = app_page
    page.clock.install(time=ACTIVITY_SCENARIO_TIME)
    return page, url


def assert_serial_requests(requests):
    """Check completed browser intervals without relying on callback delivery order."""
    intervals = []
    for request in requests:
        response = request.response()
        assert response is not None and response.finished() is None
        timing = request.timing
        start, duration = timing["startTime"], timing["responseEnd"]
        assert isfinite(start) and start > 0
        assert isfinite(duration) and duration >= 0
        intervals.append((start, start + duration))
    intervals.sort()
    for previous, following in pairwise(intervals):
        assert previous[1] <= following[0], "Browser history requests overlapped"


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_serial_request_oracle_rejects_real_overlap(app_page):
    """Two deliberately overlapping browser reads must fail the transport assertion."""
    page, url = app_page
    page.goto(url + "/activity")
    held = []

    def hold(route):
        """Keep both reads pending until their real browser intervals overlap."""
        held.append(route)
        page.evaluate("count => window.overlapReads = count", len(held))

    page.route("**/_test/transport-oracle?*", hold)
    for sequence in (1, 2):
        with page.expect_request(f"**/_test/transport-oracle?read={sequence}"):
            page.evaluate(
                "sequence => { fetch(`_test/transport-oracle?read=${sequence}`); }", sequence
            )
        # The first response remains held while the second request starts.
        page.wait_for_function("count => window.overlapReads === count", arg=sequence)
        assert len(held) == sequence
    requests = [route.request for route in held]
    for route in reversed(held):
        route.fulfill(json={"ok": True})
    with pytest.raises(AssertionError, match="Browser history requests overlapped"):
        assert_serial_requests(requests)


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_calculated_plan_title_shows_one_period_before_strategy(app_page):
    """Completed calculations show the horizon once, while running attempts retain their period."""
    page, url = app_page
    discover(page, url)

    def history(route):
        """Use independent captured plan contexts without altering stored plans or live publishers."""
        body = route.fetch().json()
        original = body["activities"][0]
        period = {"start": "2026-10-25T00:00:17+00:00", "end": "2026-10-25T02:00:17+00:00"}
        body["activities"] = [
            {
                **original,
                "id": f"{index:032x}",
                "owner": "planning",
                "kind": "planning",
                "title": "Calculate energy plan",
                "state": "running" if strategy is None else "completed",
                "finished_at": None if strategy is None else original["started_at"],
                "detail": "Synthetic energy planning example.",
                "context": period
                if strategy is None
                else {
                    **period,
                    "plan_generated_at": "2026-10-24T23:45:59+00:00",
                    "plan_strategy": strategy,
                    "plan_id": f"synthetic-plan-{index}",
                },
            }
            for index, strategy in enumerate(("minimize cost", "minimize consumption", None), 1)
        ]
        body["next"] = None
        route.fulfill(json=body)

    page.route("**/v1/activities?**", history)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expected = [
        "Calculate energy plan: Plan 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM: Minimize cost",
        "Calculate energy plan: Plan 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM: Minimize consumption",
        "Calculate energy plan: 10/25/2026, 2:00 AM to 10/25/2026, 3:00 AM",
    ]
    for index, title in enumerate(expected, 1):
        heading = page.locator(f'#activity-list [data-activity-id="{index:032x}"] summary strong')
        expect(heading).to_have_text(title)
        expect(heading).to_have_attribute("title", title)
    completed = page.locator('#activity-list [data-activity-id="00000000000000000000000000000001"]')
    completed.locator("summary").click()
    expect(completed.get_by_role("link", name="View this plan")).to_have_attribute(
        "href", f"{url}/overview?plan=synthetic-plan-1#planning"
    )
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    directory = Path("results/ci/activity-plan-title")
    directory.mkdir(parents=True, exist_ok=True)
    page.locator("#activity-list").screenshot(
        path=str(directory / f"title-{page.viewport_size['width']}.png")
    )


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
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
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
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
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
@pytest.mark.parametrize(
    "app_page",
    [{"width": 1440, "height": 900}, {"width": 768, "height": 1024}, {"width": 390, "height": 844}],
    indirect=True,
)
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
    for group in (prices, weather):
        summary = group.locator(":scope > details > summary")
        heading_box = summary.locator("h3").bounding_box()
        summary_box = summary.bounding_box()
        child_box = group.locator(":scope > details > ul summary").first.bounding_box()
        assert heading_box["x"] - summary_box["x"] >= 12
        assert abs(heading_box["x"] - child_box["x"]) <= 1
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
    expect(page.locator("#activity-list strong").first).to_have_text(
        "Earlier-day prices 31: Nord Pool"
    )
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    expect(page.locator("#activity-list .activity-row")).to_have_count(7)
    page.get_by_role("button", name="Today", exact=True).click()
    expect(day).to_have_value(today)
    expect(page.get_by_label("Activity type")).to_have_value("prices")
    expect(page.locator("#activity-page")).to_have_text("Page 1")
    expect(page.locator("#activity-list strong").first).to_have_text(
        "Fetch price forecast: Jeroen.nl"
    )


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
    expect(page.locator("#activity-list strong").first).to_have_text(
        "Earlier-day prices 31: Nord Pool"
    )
    held[0].fulfill(json=response)
    wait_for_product_reads(page)
    expect(page.locator("#activity-list strong").first).to_have_text(
        "Earlier-day prices 31: Nord Pool"
    )
    expect(page.locator("#activity-page")).to_have_text("Page 1")
    expect(page.get_by_label("Activity type")).to_have_value("prices")


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_pagination_restores_keyboard_focus_after_held_and_failed_reads(app_page):
    """Keyboard navigation retains an enabled control at both boundaries and after failure."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    expect(page.locator("#activity-health")).to_contain_text("Jeroen.nl")
    wait_for_product_reads(page)
    held = []

    def hold_page(route):
        """Capture only real page requests so completion and failure are controlled."""
        held.append(route)
        page.evaluate("count => window.paginationFocusRequestCount = count", len(held))

    page.route("**/v1/activities?**", hold_page)

    def navigate(name, number, focus, *, failed=False):
        """Activate by keyboard, reject duplicate activation and complete the held request."""
        button = page.get_by_role("button", name=name, exact=True)
        count = len(held) + 1
        button.focus()
        button.press("Enter")
        page.wait_for_function("count => window.paginationFocusRequestCount === count", arg=count)
        expect(button).to_be_disabled()
        button.dispatch_event("click")
        assert len(held) == count
        if failed:
            held[-1].fulfill(status=503)
            expect(page.locator("#activity-status")).to_contain_text("could not be loaded")
        else:
            response = page.request.get(
                held[-1].request.url, headers={"Authorization": f"Bearer {TOKEN}"}
            ).json()
            held[-1].fulfill(json=response)
        expect(page.locator("#activity-page")).to_have_text(f"Page {number}")
        wait_for_product_reads(page)
        expect(page.get_by_role("button", name=focus, exact=True)).to_be_focused()
        assert len(held) == count

    for number in (2, 3, 4):
        navigate("Next", number, "Next")
    navigate("Next", 5, "Previous")
    for number in (4, 3, 2):
        navigate("Previous", number, "Previous")
    navigate("Previous", 1, "Next")
    navigate("Next", 1, "Next", failed=True)
    page.get_by_role("button", name="Next", exact=True).focus()
    page.get_by_role("button", name="Next", exact=True).press("Enter")
    page.wait_for_function("() => window.paginationFocusRequestCount === 10")
    day = page.get_by_label("Day", exact=True)
    day.focus()
    response = page.request.get(
        held[-1].request.url, headers={"Authorization": f"Bearer {TOKEN}"}
    ).json()
    held[-1].fulfill(json=response)
    wait_for_product_reads(page)
    expect(day).to_be_focused()


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
@pytest.mark.parametrize(
    ("first_failure", "move_focus"), [(False, False), (True, False), (False, True)]
)
def test_live_resets_queue_one_read_without_releasing_pagination_focus(
    app_page, first_failure, move_focus
):
    """Real replay resets coalesce behind a held keyboard read, including failure recovery."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    expect(page.locator("#activity-health")).to_contain_text("Jeroen.nl")
    wait_for_product_reads(page)
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    wait_for_product_reads(page)
    held, requests = [], []

    def started(request):
        """Measure only browser history requests, excluding the independent fixture API client."""
        if "/v1/activities?" in request.url:
            requests.append(request)

    def hold(route):
        """Keep each actual history response pending until the test explicitly completes it."""
        held.append(route)
        page.evaluate("count => window.resetHistoryReads = count", len(held))

    page.on("request", started)
    page.route("**/v1/activities?**", hold)
    next_page = page.get_by_role("button", name="Next", exact=True)
    next_page.focus()
    next_page.press("Enter")
    page.wait_for_function("() => window.resetHistoryReads === 1")
    for reset_number in (1, 2):
        marker = f"Synthetic subscription reset {reset_number}."
        assert page.request.post(
            url + "/_test/activity-reset",
            headers={"Authorization": f"Bearer {TOKEN}"},
            data={"detail": marker},
        ).ok
        expect(page.locator("#activity-health")).to_contain_text(marker)
    assert len(held) == 1 and len(requests) == 1
    expect(next_page).to_be_disabled()
    next_page.dispatch_event("click")
    assert len(held) == 1
    if move_focus:
        page.get_by_label("Day", exact=True).focus()
    if first_failure:
        held[0].fulfill(status=503)
    else:
        response = page.request.get(
            held[0].request.url, headers={"Authorization": f"Bearer {TOKEN}"}
        ).json()
        held[0].fulfill(json=response)
    page.wait_for_function("() => window.resetHistoryReads === 2")
    expect(next_page).to_be_disabled()
    expect(page.locator("#activity-list")).to_have_attribute("aria-busy", "true")
    next_page.dispatch_event("click")
    response = page.request.get(
        held[1].request.url, headers={"Authorization": f"Bearer {TOKEN}"}
    ).json()
    identity = next(row["id"] for row in response["activities"] if row["state"] == "running")
    assert page.request.post(
        url + "/_test/activity-finish",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={"activity_id": identity},
    ).ok
    assert page.request.post(
        url + "/_test/activity-health",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={"owner": "weather", "state": "available", "detail": "Post-reset outcome received."},
    ).ok
    expect(page.locator("#activity-health")).to_contain_text("Post-reset outcome received.")
    held[1].fulfill(json=response)
    wait_for_product_reads(page)
    expect(page.locator("#activity-page")).to_have_text("Page 3")
    expect(page.locator(f'[data-activity-id="{identity}"] .activity-state')).to_have_text(
        "Completed"
    )
    expect(page.locator("#activity-status")).to_have_text("")
    expect(page.locator("#activity-list")).to_have_attribute("aria-busy", "false")
    expect(next_page).to_be_enabled()
    expect(page.get_by_label("Day", exact=True) if move_focus else next_page).to_be_focused()
    assert len(held) == 2 and len(requests) == 2
    assert_serial_requests(requests)


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_history_processing_errors_use_safe_public_message(app_page):
    """Valid JSON with malformed history cannot display technical errors or private payload values."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    expect(page.locator("#activity-health")).to_contain_text("Jeroen.nl")
    wait_for_product_reads(page)
    original = page.locator("#activity-list").inner_text()
    private_value = "PRIVATE_ACTIVITY_PROCESSING_VALUE"
    page.route(
        "**/v1/activities?**",
        lambda route: route.fulfill(
            json={"timezone": "Europe/Amsterdam", "activities": {"private": private_value}}
        ),
    )
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#activity-status")).to_have_text(
        "Activity history could not be loaded. Check the connection and try Refresh."
    )
    assert private_value not in page.locator("body").inner_text()
    assert page.locator("#activity-list").inner_text() == original
    expect(page.locator("#activity-page")).to_have_text("Page 1")
    page.unroute("**/v1/activities?**")
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    expect(page.locator("#activity-status")).to_have_text("")


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
@pytest.mark.parametrize(
    ("rejected_zone", "malformed_rows", "recovery_zone"),
    [("America/New_York", True, None), ("PRIVATE_INVALID_IANA_ZONE", False, "America/New_York")],
)
def test_history_zone_is_committed_only_with_valid_rows(
    app_page, rejected_zone, malformed_rows, recovery_zone
):
    """Reject invalid successful replies atomically, retaining clocks, details and a usable retry."""
    from zoneinfo import ZoneInfo

    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    expect(page.locator("#activity-health")).to_contain_text("Jeroen.nl")
    wait_for_product_reads(page)
    details = page.locator("#activity-list details").first
    details.locator("summary").click()
    original = page.locator("#activity-list").inner_text()
    errors, reject = [], [True]
    page.on("pageerror", lambda error: errors.append(str(error)))

    def history_reply(route):
        """Vary only the synthetic history reply while retaining real row data and cursors."""
        body = page.request.get(
            route.request.url, headers={"Authorization": f"Bearer {TOKEN}"}
        ).json()
        body["timezone"] = rejected_zone if reject[0] else recovery_zone
        if reject[0] and malformed_rows:
            body["activities"] = {"private": "PRIVATE_ACTIVITY_ZONE_PAYLOAD"}
        route.fulfill(json=body)

    page.route("**/v1/activities?**", history_reply)
    next_page = page.get_by_role("button", name="Next", exact=True)
    next_page.focus()
    next_page.press("Enter")
    expect(page.locator("#activity-status")).to_have_text(
        "Activity history could not be loaded. Check the connection and try Refresh."
    )
    wait_for_product_reads(page)
    assert page.locator("#activity-list").inner_text() == original
    expect(details).to_have_attribute("open", "")
    expect(page.locator("#activity-page")).to_have_text("Page 1")
    expect(next_page).to_be_enabled()
    expect(next_page).to_be_focused()
    assert errors == []
    assert "PRIVATE_" not in page.locator("body").inner_text()
    reject[0] = False
    next_page.press("Enter")
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    expect(page.locator("#activity-status")).to_have_text("")
    first_time = page.locator("#activity-list time").first
    expected = (
        datetime.fromisoformat(first_time.get_attribute("datetime"))
        .astimezone(ZoneInfo(recovery_zone or "UTC"))
        .strftime("%I:%M %p")
    )
    expect(first_time).to_have_text(expected)
    assert errors == []


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_completed_history_wins_over_pre_reset_running_update(app_page):
    """A completion missed in a replay gap wins over a cached running event from the prior read."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    expect(page.locator("#activity-health")).to_contain_text("Jeroen.nl")
    wait_for_product_reads(page)
    next_page = page.get_by_role("button", name="Next", exact=True)
    next_page.click()
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    wait_for_product_reads(page)
    held = []

    def hold(route):
        """Hold both actual attempts while preserving the real event subscription."""
        held.append(route)
        page.evaluate("count => window.attemptHistoryReads = count", len(held))

    page.route("**/v1/activities?**", hold)
    next_page.focus()
    next_page.press("Enter")
    page.wait_for_function("() => window.attemptHistoryReads === 1")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    old_response = page.request.get(held[0].request.url, headers=headers).json()
    identity = next(row["id"] for row in old_response["activities"] if row["state"] == "running")
    assert page.request.post(
        url + "/_test/activity-progress", headers=headers, data={"activity_id": identity}
    ).ok
    expect(page.locator("#activity-health")).to_contain_text("Pre-reset progress received.")
    with page.expect_response(
        lambda response: (
            "/v1/events" in response.url
            and response.status == 200
            and response.json().get("reset") is True
        )
    ) as reset_reply:
        assert page.request.post(
            url + "/_test/activity-finish",
            headers=headers,
            data={"activity_id": identity, "reset": True},
        ).ok
    snapshot = reset_reply.value.json()
    assert snapshot["events"] == []
    assert all(row["id"] != identity for row in snapshot["snapshot"]["running"])
    expect(page.locator("#activity-health")).to_contain_text("Missed completion recovered.")
    held[0].fulfill(json=old_response)
    page.wait_for_function("() => window.attemptHistoryReads === 2")
    fresh_response = page.request.get(held[1].request.url, headers=headers).json()
    assert next(row["state"] for row in fresh_response["activities"] if row["id"] == identity) == (
        "completed"
    )
    held[1].fulfill(json=fresh_response)
    wait_for_product_reads(page)
    expect(page.locator(f'[data-activity-id="{identity}"] .activity-state')).to_have_text(
        "Completed"
    )
    expect(page.locator("#activity-page")).to_have_text("Page 3")
    expect(next_page).to_be_focused()
    assert len(held) == 2


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_error_stream_cancellation_settles_before_queued_history_read(app_page):
    """A real streaming 503 is explicitly cancelled before reset recovery releases navigation."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#activity-list .activity-row")).to_have_count(25)
    expect(page.locator("#activity-health")).to_contain_text("Jeroen.nl")
    wait_for_product_reads(page)
    next_page = page.get_by_role("button", name="Next", exact=True)
    next_page.click()
    expect(page.locator("#activity-page")).to_have_text("Page 2")
    wait_for_product_reads(page)
    page.evaluate("""() => {
        const original = window.fetch;
        let sequence = 0;
        window.historyBodyLifecycle = [];
        const cancellationGate = new Promise(resolve => { window.releaseHistoryCancellation = resolve; });
        window.fetch = async function(input, options) {
            const url = input instanceof Request ? input.url : String(input);
            if (!url.includes('v1/activities?')) return original.call(this, input, options);
            const request = ++sequence;
            const record = event => window.historyBodyLifecycle.push({request, event});
            record('start');
            const response = await original.call(this, input, options);
            if (!response.ok && response.body) {
                const cancel = response.body.cancel.bind(response.body);
                response.body.cancel = async (...args) => {
                    record('cancel-start');
                    const result = await cancel(...args);
                    await cancellationGate;
                    record('cancel-complete');
                    return result;
                };
            }
            return response;
        };
    }""")
    held = []

    def history(route):
        """Hold the original read and let the queued recovery use the real history endpoint."""
        if not held:
            held.append(route)
            page.evaluate("window.streamingHistoryHeld = true")
        else:
            route.continue_()

    page.route("**/v1/activities?**", history)
    next_page.focus()
    next_page.press("Enter")
    page.wait_for_function("() => window.streamingHistoryHeld === true")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert page.request.post(
        url + "/_test/activity-reset",
        headers=headers,
        data={"detail": "Streaming error recovery queued."},
    ).ok
    expect(page.locator("#activity-health")).to_contain_text("Streaming error recovery queued.")
    try:
        held[0].continue_(url=url + "/_test/activity-streamed-error")
        page.wait_for_function(
            "() => window.historyBodyLifecycle.some(item => item.event === 'cancel-start')"
        )
        lifecycle = page.evaluate("window.historyBodyLifecycle")
        assert not any(item["event"] == "cancel-complete" for item in lifecycle)
        expect(next_page).to_be_disabled()
        expect(page.locator("#activity-list")).to_have_attribute("aria-busy", "true")
        assert sum(item["event"] == "start" for item in lifecycle) == 1
    finally:
        page.evaluate("window.releaseHistoryCancellation()")
        assert page.request.post(url + "/_test/activity-release-error-body", headers=headers).ok
    wait_for_product_reads(page)
    lifecycle = page.evaluate("window.historyBodyLifecycle")
    cancelled = lifecycle.index({"request": 1, "event": "cancel-complete"})
    following = lifecycle.index({"request": 2, "event": "start"})
    assert cancelled < following
    assert sum(item["event"] == "start" for item in lifecycle) == 2
    expect(page.locator("#activity-page")).to_have_text("Page 3")
    expect(page.locator("#activity-status")).to_have_text("")
    expect(next_page).to_be_enabled()
    expect(next_page).to_be_focused()
    assert "PRIVATE_ACTIVITY_ERROR_BODY" not in page.locator("body").inner_text()


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


@pytest.mark.parametrize("app_site", ["activity"], indirect=True)
def test_historical_bonus_wait_does_not_request_contract_reconfiguration(app_page):
    """Desktop and mobile identify historical checkpoint evidence without asking users to alter a valid contract."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/activity")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    assert page.request.post(
        url + "/_test/activity-bonus-wait", headers={"Authorization": f"Bearer {TOKEN}"}
    ).ok
    page.get_by_label("Activity type").select_option("settlement")
    waiting = page.locator("#activity-list details").filter(has_text="Evaluate measured savings")
    waiting.locator("summary").click()
    expect(waiting).to_contain_text("processed 0 of 96 measured intervals")
    expect(waiting).to_contain_text("Do not change a valid contract start date")
    expect(waiting).to_contain_text("Previous published financial results remain available")
    expect(waiting).not_to_contain_text("Check the bonus budget under Contract")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    directory = Path("results/ci/historical-bonus")
    directory.mkdir(parents=True, exist_ok=True)
    waiting.screenshot(path=str(directory / f"waiting-{page.viewport_size['width']}.png"))
