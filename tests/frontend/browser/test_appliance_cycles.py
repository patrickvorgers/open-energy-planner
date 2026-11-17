"""Keep daily cycle planning separate from retained read-only appliance insights."""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_planning_links_select_device_and_insights_retain_unshiftable_history(app_page):
    """Device identity, eight recent cycles, retained models and narrow screens survive the page split."""
    page, url = app_page
    page.clock.install()
    start = datetime(2026, 11, 1, tzinfo=UTC)
    cycles = [
        {
            "started_at": (start + timedelta(hours=index * 4)).isoformat(),
            "completed_at": (start + timedelta(hours=index * 4 + 2)).isoformat(),
            "energy_kwh": 0.83,
            "synthetic": True,
        }
        for index in range(27)
    ]
    profile = {
        "power_w": [1800, 80, 80, 0, 80, 1200, 80, 0],
        "samples": 27,
        "evidence": "synthetic",
    }
    devices = [
        {
            "entity_id": entity,
            "name": name,
            "shiftable": shiftable,
            "profile": profile,
            "learned_profile": profile,
            "recent_cycles": cycles if shiftable else [],
            "task": None,
            "power_entity": entity + "_power",
            "history_import": {"phase": "complete"},
        }
        for entity, name, shiftable in (
            ("sensor.washer", "Example washer", False),
            ("sensor.dishwasher", "Example dishwasher", True),
        )
    ]
    sources = {
        "charging_meters": [],
        "thermal_meters": [],
        "selected": [{"entity_id": device["entity_id"]} for device in devices],
        "options": [
            {"entity_id": device["entity_id"], "name": device["name"]} for device in devices
        ],
    }
    page.route("**/v1/appliances", lambda route: route.fulfill(json={"devices": devices}))
    page.route("**/v1/consumers?view=settings", lambda route: route.fulfill(json=sources))
    page.goto(url + "/appliances")
    authenticate(page, url)
    expect(page.get_by_role("heading", name="Appliance planning", exact=True)).to_be_visible()
    expect(page.locator("#appliance-list > section")).to_have_count(1)
    expect(page.locator("#appliance-profiles")).to_have_count(0)
    expect(page.locator("#appliance-list")).to_contain_text("0.83 kWh, 120 minutes")
    page.screenshot(
        path=f"results/ci/appliance-planning-{page.viewport_size['width']}.png", full_page=True
    )
    page.get_by_role("link", name="View cycle insights", exact=True).click()
    authenticate(page, url)
    expect(page.locator("#profile-device")).to_have_value("sensor.dishwasher")
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_have_count(0)
    expect(page.locator("#detected-cycles > li")).to_have_count(8)
    expect(page.get_by_role("button", name="Next", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Previous", exact=True)).to_have_count(0)
    expect(page.locator("#detected-cycles details")).to_have_count(0)
    first = page.locator("#detected-cycles > li").first
    expect(first).to_contain_text("Started:")
    expect(first).to_contain_text("Completed:")
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(300000)
    expect(page.locator("#detected-cycles > li")).to_have_count(8)
    page.locator("#profile-device").select_option("sensor.washer")
    expect(page.locator("#profile-history")).to_contain_text("The learned profile is retained")
    expect(page.locator("#profile-history")).to_contain_text("Fine cycles in model")
    expect(page.locator("#cycle-charts")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(
        "document.querySelectorAll('canvas').forEach(canvas => { const chart = Chart.getChart(canvas); chart?.stop(); chart?.update('none'); })"
    )
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/appliance-cycles-{page.viewport_size['width']}.png", full_page=True
    )
    page.go_back()
    expect(page.locator("#profile-device")).to_have_value("sensor.dishwasher")
    page.go_forward()
    expect(page.locator("#profile-device")).to_have_value("sensor.washer")
    page.reload()
    authenticate(page, url)
    expect(page.locator("#profile-device")).to_have_value("sensor.washer")
    page.goto(url + "/?page=" + quote("/appliance-cycles#entity=sensor.dishwasher", safe=""))
    content = page.frame_locator("#workspace-content")
    authenticate(content, url)
    expect(content.locator("#profile-device")).to_have_value("sensor.dishwasher")
    expect(page.locator('.sidebar a[aria-current="page"]')).to_contain_text("Appliance cycles")
    sources["charging_meters"] = ["sensor.dishwasher"]
    page.clock.fast_forward(300000)
    expect(content.locator("#profile-device option")).to_have_text(["Example washer"])
    sources["selected"] = []
    page.clock.fast_forward(300000)
    expect(content.locator("#appliance-profiles")).to_be_hidden()
    expect(content.get_by_role("heading", name="No followed cycle appliances")).to_be_visible()
    assert content.locator("#cycle-chart").evaluate("canvas => !Chart.getChart(canvas)")


def authenticate(page, url):
    """Standalone pages accept a token while trusted ingress connects automatically."""
    if "/api/hassio_ingress/" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Connected")


@pytest.fixture
def review_evidence(app_page):
    """Serve stable cycle identities with independently mutable evidence and HA timezone."""
    page, url = app_page
    clock_start = datetime(2026, 7, 20, tzinfo=UTC)
    page.clock.install(time=clock_start)
    page.clock.pause_at(clock_start)
    start = datetime(2026, 7, 1, 9, tzinfo=UTC)
    profile = {"power_w": [1000, 0, 200], "samples": 76, "evidence": "synthetic"}
    devices = [
        {
            "entity_id": entity,
            "name": name,
            "shiftable": False,
            "power_entity": entity + "_power",
            "profile": profile,
            "learned_profile": profile,
            "recent_cycles": [
                {
                    "started_at": (start + timedelta(hours=index * 4)).isoformat(),
                    "completed_at": (start + timedelta(hours=index * 4 + 1)).isoformat(),
                    "energy_kwh": 0.3,
                }
                for index in range(76)
            ],
            "raw_history": {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "status": "complete",
                "power_curve": [
                    {"at": start.isoformat(), "power_w": 1000},
                    {"at": (start + timedelta(hours=1)).isoformat(), "power_w": 0},
                ],
                "energy_curve": [
                    {"at": start.isoformat(), "kwh": 1},
                    {"at": (start + timedelta(hours=1)).isoformat(), "kwh": 2},
                ],
            },
        }
        for entity, name in [("sensor.washer", "Washer"), ("sensor.dryer", "Dryer")]
    ]
    state = {"devices": devices, "revision": "review-evidence"}
    session = {"ingress": False, "timezone": "UTC"}
    sources = {
        "charging_meters": [],
        "thermal_meters": [],
        "selected": [{"entity_id": device["entity_id"]} for device in devices],
        "options": devices,
    }
    page.route("**/setup/session", lambda route: route.fulfill(json=session))
    page.route("**/v1/appliances", lambda route: route.fulfill(json=state))
    page.route("**/v1/consumers?view=settings", lambda route: route.fulfill(json=sources))
    page.goto(url + "/appliance-cycles")
    authenticate(page, url)
    return page, devices, session


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_poll_updates_active_and_completed_cycles_without_replacing_profile(review_evidence):
    """Running and uncertain activity stays above eight completed runs while unchanged charts retain identity."""
    page, devices, _ = review_evidence
    first = page.locator("#detected-cycles > li").first
    chart_identity = page.locator("#cycle-chart").evaluate("canvas => Chart.getChart(canvas).id")
    devices[0]["observed_run"] = {
        "status": "observed",
        "started_at": "2026-07-20T10:00:00Z",
        "energy_kwh": 0.5,
    }
    devices[0]["recent_cycles"][-1]["energy_kwh"] = 1.23
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(300000)
    expect(page.locator("#profile-history")).to_contain_text("0.50 kWh measured")
    active = page.locator("#cycle-in-progress")
    expect(active).to_be_visible()
    expect(active).to_contain_text("Cycle in progress")
    expect(active).not_to_contain_text("cannot confirm whether this cycle")
    expect(active).to_contain_text("Started:")
    expect(first).to_contain_text("1.23 kWh")
    expect(page.locator("#detected-cycles > li")).to_have_count(8)
    assert active.bounding_box()["y"] < first.bounding_box()["y"]
    assert (
        page.locator("#cycle-chart").evaluate("canvas => Chart.getChart(canvas).id")
        == chart_identity
    )
    devices[0]["observed_run"]["status"] = "uncertain"
    page.clock.fast_forward(300000)
    expect(active).to_contain_text("Cycle detected")
    expect(active).to_contain_text("Current state unconfirmed")
    expect(active).not_to_contain_text("Execution uncertain")
    expect(active).to_contain_text(
        "Recent power and energy measurements cannot confirm whether this cycle is still running or has finished."
    )
    expect(active).to_contain_text("Pending starts are blocked.")
    devices[0]["observed_run"]["status"] = "complete"
    page.clock.fast_forward(300000)
    expect(active).to_have_count(0)


@pytest.mark.parametrize(
    "app_page", [{"width": 1440, "height": 1024}, {"width": 390, "height": 844}], indirect=True
)
def test_page_selection_controls_profile_and_right_hand_history(review_evidence):
    """Page selection stays above stacked charts and responsive history preserves open learning on refresh."""
    page, devices, _ = review_evidence
    expect(page.locator("#profile-title")).to_have_text("Washer")
    expect(page.locator("#profile-summary")).to_have_text("0.30 kWh per cycle: 45 minutes")
    selector = page.get_by_label("Appliance", exact=True).bounding_box()
    profile = page.get_by_role("heading", name="Average cycle profile", exact=True).bounding_box()
    history = page.get_by_role("heading", name="Detected cycles", exact=True).bounding_box()
    power = page.locator("#cycle-chart").bounding_box()
    energy = page.locator("#cycle-energy-chart").bounding_box()
    assert selector["y"] + selector["height"] <= profile["y"]
    assert energy["y"] >= power["y"] + power["height"]
    expected_height = 330 if page.viewport_size["width"] > 640 else 210
    assert power["height"] == pytest.approx(expected_height, abs=1)
    assert energy["height"] == pytest.approx(expected_height, abs=1)
    if page.viewport_size["width"] > 1100:
        assert history["x"] > power["x"] + power["width"]
        assert abs(history["y"] - profile["y"]) < 2
    else:
        assert history["y"] > energy["y"] + energy["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    learning = page.locator("#learning-history")
    expect(learning).not_to_have_attribute("open", "")
    learning.locator("summary").click()
    chart_identity = page.locator("#cycle-chart").evaluate("canvas => Chart.getChart(canvas).id")
    devices[0]["learning_weight"] = 12.5
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(300000)
    expect(learning).to_have_attribute("open", "")
    expect(learning.locator("summary")).to_be_focused()
    expect(learning).to_contain_text("12.50 at latest cycle")
    assert (
        page.locator("#cycle-chart").evaluate("canvas => Chart.getChart(canvas).id")
        == chart_identity
    )
    page.get_by_label("Appliance", exact=True).select_option("sensor.dryer")
    expect(page.locator("#profile-title")).to_have_text("Dryer")
    expect(learning).not_to_have_attribute("open", "")
    expect(learning).not_to_contain_text("12.50 at latest cycle")


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_waiting_profile_keeps_learning_available_without_raw_detail_requests(review_evidence):
    """A device without a learned profile still completes rendering and exposes its retained evidence."""
    page, devices, _ = review_evidence
    devices[0]["profile"] = None
    devices[0]["history_import"] = {"phase": "processing"}
    page.clock.fast_forward(300000)
    expect(page.locator("#profile-kind")).to_have_text("Waiting for learning")
    expect(page.locator("#profile-summary")).to_be_hidden()
    expect(page.locator("#profile-note")).to_contain_text("Use the appliance normally")
    expect(page.locator("#cycle-charts")).to_be_hidden()
    learning = page.get_by_text("Learning and usable history", exact=True)
    expect(learning).to_be_visible()
    learning.click()
    expect(page.locator("#learning-history")).to_contain_text("Processing hourly history")
    expect(page.locator("#status")).to_have_text("Connected")
    expect(page.get_by_text("Recorded cycle measurements", exact=True)).to_have_count(0)
    devices[0]["power_entity"] = None
    page.clock.fast_forward(300000)
    expect(page.locator("#profile-note")).to_have_text(
        "Link a power sensor in Home Assistant Energy to start learning."
    )


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
@pytest.mark.parametrize(
    "evidence,basis,label,note",
    [
        (
            "measured",
            "statistics",
            "Historical estimate",
            "Historical data cannot confirm continuous reporting.",
        ),
        ("measured", "observed", "Followed profile", ""),
        (
            "hourly_estimate",
            "statistics",
            "Historical estimate",
            "Cycle timing is approximate. Finer measurements will refine it.",
        ),
        ("manual", None, "Manual estimate", "Energy is spread evenly over the entered duration."),
        ("synthetic", None, "Simulated profile", ""),
    ],
)
def test_cycle_summary_keeps_decisions_visible_and_technical_evidence_collapsed(
    review_evidence, evidence, basis, label, note
):
    """Profile provenance and limitations stay readable while model counts and explanations require expansion."""
    page, devices, _ = review_evidence
    devices[0]["profile"] = {**devices[0]["profile"], "evidence": evidence}
    devices[0]["profile_basis"] = basis
    devices[0]["learning_half_life_days"] = 90
    page.clock.fast_forward(300000)
    expect(page.locator("#profile-kind")).to_have_text(label)
    expect(page.locator("#profile-note")).to_have_text(note)
    if note:
        expect(page.locator("#profile-note")).to_be_visible()
    else:
        expect(page.locator("#profile-note")).to_be_hidden()
    expect(page.get_by_text("Fine cycles in model", exact=True)).to_be_hidden()
    expect(page.get_by_text("Model evidence", exact=True)).to_be_hidden()
    expect(page.locator("#profile-history")).not_to_contain_text("completed measured cycles")
    page.get_by_text("Learning and usable history", exact=True).click()
    expect(page.get_by_text("Fine cycles in model", exact=True)).to_be_visible()
    expect(page.locator("#learning-history")).to_contain_text("90-day half-life")
    expect(page.locator("#profile-summary")).to_contain_text("0.30 kWh per cycle")


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_simulated_hourly_profile_and_planning_warning_stay_explicit(review_evidence):
    """Collapsing technical evidence never conceals simulated input or an actionable planning limitation."""
    page, devices, _ = review_evidence
    devices[0]["profile"] = {**devices[0]["profile"], "evidence": "hourly_estimate"}
    devices[0]["hourly_synthetic"] = True
    devices[0]["planning_issue"] = "Link a power sensor in Home Assistant Energy."
    page.clock.fast_forward(300000)
    expect(page.locator("#profile-kind")).to_have_text("Simulated hourly estimate")
    expect(
        page.get_by_text("Link a power sensor in Home Assistant Energy.", exact=True)
    ).to_be_visible()
    expect(page.locator("#learning-history")).not_to_have_attribute("open", "")


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_back_to_empty_entity_fragment_restores_initial_device(review_evidence):
    """An initial implicit device remains the target of browser Back after choosing another device."""
    page, _, _ = review_evidence
    expect(page.locator("#profile-device")).to_have_value("sensor.washer")
    page.locator("#profile-device").select_option("sensor.dryer")
    page.go_back()
    expect(page.locator("#profile-device")).to_have_value("sensor.washer")
    page.go_forward()
    expect(page.locator("#profile-device")).to_have_value("sensor.dryer")


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_cycle_history_uses_five_minute_poll_and_latest_eight_by_completion(review_evidence):
    """No reads happen before five minutes and unordered evidence still shows the eight newest completions."""
    page, devices, _ = review_evidence
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    expect(page.get_by_role("button", name="Refresh cycle evidence", exact=True)).to_have_count(0)
    expect(page.get_by_text("Recorded cycle measurements", exact=True)).to_have_count(0)
    cycles = devices[0]["recent_cycles"]
    devices[0]["recent_cycles"] = cycles[1::2] + cycles[::2]
    page.clock.fast_forward(299999)
    assert not any("/v1/appliances" in address for address in requests)
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(1)
    rows = page.locator("#detected-cycles > li")
    expect(rows).to_have_count(8)
    expect(rows.first).to_contain_text("7/13/2026, 9:00 PM")
    expect(rows.first).to_contain_text("7/13/2026, 10:00 PM")
    expect(rows.last).to_contain_text("7/12/2026, 5:00 PM")
    expect(page.locator("#detected-cycles details")).to_have_count(0)
    assert not any("raw_entity=" in address for address in requests)


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_cycle_poll_failure_keeps_evidence_then_recovers_automatically(review_evidence):
    """A failed periodic read retains the selected model and automatically recovers at the next interval."""
    page, devices, _ = review_evidence
    page.get_by_label("Appliance", exact=True).select_option("sensor.dryer")
    page.route("**/v1/appliances", lambda route: route.fulfill(status=503))
    page.clock.fast_forward(300000)
    expect(page.locator("#status")).to_contain_text("Retained evidence is shown")
    expect(page.locator("#profile-title")).to_have_text("Dryer")
    expect(page.locator("#cycle-charts")).to_be_visible()
    page.unroute("**/v1/appliances")
    page.route("**/v1/appliances", lambda route: route.fulfill(json={"devices": devices}))
    page.clock.fast_forward(300000)
    expect(page.locator("#status")).to_have_text("Connected")
    expect(page.locator("#profile-device")).to_have_value("sensor.dryer")


def test_initial_cycle_read_failure_retries_without_manual_refresh(review_evidence):
    """Polling waits for connection and recovers a failed first authenticated load automatically."""
    page, devices, _ = review_evidence
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.reload()
    page.clock.fast_forward(300000)
    assert not any("/v1/appliances" in address for address in requests)
    page.route("**/v1/appliances", lambda route: route.fulfill(status=503))
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("reload this page")
    page.unroute("**/v1/appliances")
    page.route("**/v1/appliances", lambda route: route.fulfill(json={"devices": devices}))
    page.clock.fast_forward(300000)
    expect(page.locator("#status")).to_have_text("Connected")
    expect(page.locator("#cycle-charts")).to_be_visible()


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_cycle_rows_follow_installation_timezone_and_capture_compact_history(review_evidence):
    """A representative complete profile, eight inline runs and active status stay readable in the HA timezone."""
    page, devices, session = review_evidence
    devices[0]["profile"] = {
        "power_w": [
            300,
            1800,
            1200,
            80,
            80,
            3000,
            1800,
            80,
            0,
            0,
            0,
            120,
            600,
            80,
            80,
            1800,
            200,
            50,
        ],
        "phase_seconds": 300,
        "samples": 76,
        "evidence": "synthetic",
    }
    devices[0]["observed_run"] = {
        "status": "observed",
        "started_at": "2026-07-20T10:00:00Z",
        "energy_kwh": 0.5,
    }
    session["timezone"] = "Europe/London"
    page.clock.fast_forward(300000)
    first = page.locator("#detected-cycles > li").first
    expect(first).to_contain_text("7/13/2026, 10:00 PM")
    expect(first).to_contain_text("7/13/2026, 11:00 PM")
    for timestamp in page.locator("#detected-cycles > li > span").all():
        assert timestamp.evaluate("""element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            return range.getClientRects().length === 1 && element.scrollWidth <= element.clientWidth;
        }""")
    expect(page.locator("#cycle-in-progress")).to_contain_text("11:00 AM")
    expect(page.locator("#detected-cycles > li")).to_have_count(8)
    expect(page.locator("#learning-history")).not_to_have_attribute("open", "")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("""() => {
        for (const id of ['cycle-chart', 'cycle-energy-chart']) {
            const chart = Chart.getChart(id); chart.stop(); chart.update('none');
        }
    }""")
    page.screenshot(
        path=f"results/ci/cycle-compact-{page.viewport_size['width']}.png", full_page=True
    )
