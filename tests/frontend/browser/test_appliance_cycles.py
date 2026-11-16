"""Keep daily cycle planning separate from retained read-only appliance insights."""

from copy import deepcopy
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
    """Device identity, pagination, retained models and narrow screens survive the page split."""
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
    expect(page.locator("#detected-cycles > li")).to_have_count(25)
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.get_by_role("button", name="Previous", exact=True)).to_be_focused()
    expect(page.locator("#detected-cycles > li")).to_have_count(2)
    expect(page.get_by_text("Page 2", exact=True)).to_be_visible()
    first = page.locator("#detected-cycles details").first
    first.locator("summary").click()
    expect(first).to_have_attribute("open", "")
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(10000)
    expect(page.get_by_text("Page 2", exact=True)).to_be_visible()
    expect(first).to_have_attribute("open", "")
    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.get_by_role("button", name="Next", exact=True)).to_be_focused()
    expect(page.locator("#detected-cycles > li")).to_have_count(25)
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
    content.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
    expect(content.locator("#profile-device option")).to_have_text(["Example washer"])
    sources["selected"] = []
    content.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
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
    page.clock.install()
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
def test_poll_updates_evidence_without_closing_or_defocusing_cycle(review_evidence):
    """Volatile learning evidence and corrected energy refresh while the same cycle stays open and focused."""
    page, devices, _ = review_evidence
    first = page.locator("#detected-cycles details").first
    first.locator("summary").click()
    devices[0]["observed_run"] = {
        "status": "observed",
        "started_at": "2026-07-20T10:00:00Z",
        "energy_kwh": 0.5,
    }
    devices[0]["recent_cycles"][-1]["energy_kwh"] = 1.23
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(10000)
    expect(page.locator("#profile-history")).to_contain_text("0.50 kWh measured")
    expect(
        page.locator("#profile-history > p").filter(has_text="0.50 kWh measured")
    ).to_be_visible()
    expect(first.locator("summary")).to_contain_text("1.23 kWh")
    expect(first).to_have_attribute("open", "")
    expect(first.locator("summary")).to_be_focused()


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
        page.clock.fast_forward(10000)
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
def test_waiting_profile_keeps_learning_and_recorded_evidence_available(review_evidence):
    """A device without a learned profile still completes rendering and exposes its retained evidence."""
    page, devices, _ = review_evidence
    devices[0]["profile"] = None
    devices[0]["history_import"] = {"phase": "processing"}
    page.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
    expect(page.locator("#profile-kind")).to_have_text("Waiting for learning")
    expect(page.locator("#profile-summary")).to_be_hidden()
    expect(page.locator("#profile-note")).to_contain_text("Use the appliance normally")
    expect(page.locator("#cycle-charts")).to_be_hidden()
    learning = page.get_by_text("Learning and usable history", exact=True)
    expect(learning).to_be_visible()
    learning.click()
    expect(page.locator("#learning-history")).to_contain_text("Processing hourly history")
    expect(page.locator("#status")).to_have_text("Connected")
    expect(page.get_by_text("Recorded cycle measurements", exact=True)).to_be_visible()
    devices[0]["power_entity"] = None
    page.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
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
    page.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
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
    page.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
    expect(page.locator("#profile-kind")).to_have_text("Simulated hourly estimate")
    expect(
        page.get_by_text("Link a power sensor in Home Assistant Energy.", exact=True)
    ).to_be_visible()
    expect(page.locator("#learning-history")).not_to_have_attribute("open", "")


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_keyboard_paging_keeps_direction_until_boundary(review_evidence):
    """Repeated Enter traverses four pages in either direction with usable boundary focus."""
    page, _, _ = review_evidence
    page.get_by_role("button", name="Next", exact=True).focus()
    for number in (2, 3, 4):
        page.keyboard.press("Enter")
        expect(page.get_by_text(f"Page {number}", exact=True)).to_be_visible()
        expect(
            page.get_by_role("button", name="Next" if number < 4 else "Previous", exact=True)
        ).to_be_focused()
    expect(page.locator("#detected-cycles > li")).to_have_count(1)
    for number in (3, 2, 1):
        page.keyboard.press("Enter")
        expect(page.get_by_text(f"Page {number}", exact=True)).to_be_visible()
        expect(
            page.get_by_role("button", name="Previous" if number > 1 else "Next", exact=True)
        ).to_be_focused()


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_raw_measurements_refresh_timezone_with_identical_winter_offset(review_evidence):
    """Unchanged July measurements shift one hour in London, including already rendered chart ticks."""
    page, _, session = review_evidence
    page.get_by_text("Recorded cycle measurements", exact=True).click()
    expect(page.locator("#recorded-cycle-status")).to_contain_text("10:00 AM")
    original = page.locator("#recorded-power-chart").evaluate(
        "canvas => Chart.getChart(canvas).scales.x.ticks.map(tick => tick.label)"
    )
    session["timezone"] = "Europe/London"
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(10000)
    expect(page.locator("#recorded-cycle-status")).to_contain_text("11:00 AM")
    current = page.locator("#recorded-power-chart").evaluate(
        "canvas => Chart.getChart(canvas).scales.x.ticks.map(tick => tick.label)"
    )
    assert current != original
    assert any("10:00 AM" in label for label in current)


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
def test_old_raw_failure_cannot_replace_newer_success_and_current_failure_recovers(review_evidence):
    """Release a stale rejection after a newer success, then prove current failures and retry remain visible."""
    page, devices, _ = review_evidence
    raw = deepcopy(devices[0]["raw_history"])
    state = {"revision": "r1", "devices": devices}
    pending = []
    page.route("**/v1/appliances", lambda route: route.fulfill(json=state))
    page.evaluate("""() => {
        window.rawFailuresHandled = [];
        const original = Promise.prototype.catch;
        Promise.prototype.catch = function(handler) {
            return original.call(this, error => {
                try { return handler(error); }
                finally {
                    if (['stale-raw-failure', 'current-raw-failure'].includes(error?.message)) window.rawFailuresHandled.push(error.message);
                }
            });
        };
    }""")

    def hold_raw(route):
        """Acknowledge each actual request before the test controls its terminal response."""
        pending.append(route)
        page.evaluate("count => window.rawHeldCount = count", len(pending))

    def use_summary(revision):
        """Advance the evidence identity without including the separately requested original curves."""
        state["revision"] = revision
        devices[0]["raw_history"] = {
            **{
                key: value
                for key, value in raw.items()
                if key not in ("power_curve", "energy_curve")
            },
            "curve_revision": revision,
        }

    def succeed(request_index, revision, peak):
        """Return independently specified raw evidence for the selected revision."""
        detail = deepcopy(devices[0])
        detail["raw_history"] = {**deepcopy(raw), "curve_revision": revision}
        detail["raw_history"]["power_curve"][0]["power_w"] = peak
        pending[request_index].fulfill(json={"revision": revision, "devices": [detail]})

    page.route("**/v1/appliances?raw_entity=*", hold_raw)
    page.get_by_text("Recorded cycle measurements", exact=True).click()
    use_summary("r1")
    page.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
    page.wait_for_function("() => window.rawHeldCount === 1")
    use_summary("r2")
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(10000)
    page.wait_for_function("() => window.rawHeldCount === 2")
    succeed(1, "r2", 2000)
    expect(page.locator("#recorded-cycle-status")).to_contain_text("Power and energy agree")
    before = page.locator("#recorded-cycle-status").inner_text()
    curves = page.evaluate(
        "['recorded-power-chart','recorded-energy-chart'].map(id => Chart.getChart(id).data.datasets[0].data)"
    )
    assert curves[0][0]["y"] == 2000
    with page.expect_response(
        lambda response: "raw_entity=" in response.url and response.status == 503
    ):
        pending[0].fulfill(status=503, body="stale-raw-failure")
    page.wait_for_function("() => window.rawFailuresHandled.includes('stale-raw-failure')")
    expect(page.locator("#recorded-cycle-status")).to_have_text(before)
    assert (
        page.evaluate(
            "['recorded-power-chart','recorded-energy-chart'].map(id => Chart.getChart(id).data.datasets[0].data)"
        )
        == curves
    )
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(10000)
    expect(page.locator("#recorded-cycle-status")).to_have_text(before)
    assert len(pending) == 2
    use_summary("r3")
    page.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
    page.wait_for_function("() => window.rawHeldCount === 3")
    pending[2].fulfill(status=503, body="current-raw-failure")
    page.wait_for_function("() => window.rawFailuresHandled.includes('current-raw-failure')")
    expect(page.locator("#recorded-cycle-status")).to_contain_text(
        "Recorded state details unavailable"
    )
    page.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
    page.wait_for_function("() => window.rawHeldCount === 4")
    succeed(3, "r3", 3000)
    expect(page.locator("#recorded-cycle-status")).to_contain_text("Power and energy agree")
    assert (
        page.evaluate("Chart.getChart('recorded-power-chart').data.datasets[0].data[0].y") == 3000
    )
