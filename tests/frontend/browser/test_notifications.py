"""Verify shared actionable notices, authentication, recovery and stale-data handling."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from playwright.sync_api import expect

from tests.app.notifications.support import notification_document
from tests.frontend.browser.helpers import TOKEN, discover, open_primary_navigation


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("named", [False, True])
def test_sensor_identity_and_remedy_are_visible_in_both_notification_views(app_page, named):
    """Desktop/mobile ingress shows both real source failures without a technical-details disclosure or overflow."""
    from open_energy_planner.home_assistant.notifications import explain_measurement_problem
    from open_energy_planner.runtime.diagnostics.planning_notifications import (
        PlanningStatus,
        planning_notice,
    )
    from tests.app.notifications.support import measurement_evidence

    page, url = app_page
    now = datetime(2026, 11, 1, 9, tzinfo=UTC)
    config, report = measurement_evidence(now)
    names = {"sensor.soc": "Yixiang MB56 32kWh"} if named else None
    text = explain_measurement_problem(
        "required_measurement_unavailable", config, report, now, names
    )
    notice = planning_notice(
        PlanningStatus("not_ready", "required_measurement_unavailable", now), text
    )
    result = {"notifications": [notice.document("planning")], "unavailable_sources": []}
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/setup")
    banner = page.get_by_role("region", name="Notifications")
    expect(banner).to_contain_text("sensor.grid: The configured sensor does not exist")
    expect(banner).to_contain_text(
        "Yixiang MB56 32kWh (sensor.soc): Home Assistant reports unknown or unavailable"
        if named
        else "sensor.soc: Home Assistant reports unknown or unavailable"
    )
    expect(banner.get_by_text("Technical details", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Notifications: 1 active", exact=True).click()
    popup = page.locator("#notification-center-panel")
    expect(popup).to_be_visible()
    expect(popup).to_contain_text("sensor.grid")
    expect(popup).to_contain_text("sensor.soc")
    if named:
        expect(popup).to_contain_text("Yixiang MB56 32kWh")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=f"results/ci/actionable-sensors-{page.viewport_size['width']}.png", full_page=True
    )


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("after_confirmation", ["calculating", "not_ready"])
@pytest.mark.parametrize("refresh_case", ["ordinary", "overlap", "failed_read"])
def test_opening_confirmation_immediately_shows_actual_planning_status(
    app_page, after_confirmation, refresh_case
):
    """An acknowledged prefix refreshes the real notice reader without waiting for polling or inventing readiness."""
    page, url = app_page
    discover(page, url)
    page.clock.install()
    phase = ["prefix"]
    pending_notice = []
    since = datetime(2026, 10, 30, tzinfo=UTC)
    budget = {
        "rule_id": "solar",
        "active": True,
        "period_start": "2026-01-01T00:00:00+00:00",
        "cap_kwh": 7500,
        "used_kwh": None,
        "remaining_kwh": None,
        "issue": "bonus_statistics_prefix_required",
        "confirmation": {"reference_version": "fixed-reference", "config_revision": "config"},
        "statistics": {"reference_at": "2026-09-27T01:00:00+00:00", "series": []},
    }

    def notifications(route):
        """Return authoritative blocked, calculating and completed projections independently of budget reads."""
        status, reason = {
            "prefix": ("not_ready", "bonus_statistics_prefix_required"),
            "calculating": ("calculating", None),
            "not_ready": ("not_ready", "tariff_coverage_gap"),
            "ready": ("ready", "validated"),
        }[phase[0]]
        document = notification_document(
            status,
            reason,
            None if phase[0] == "ready" else since,
            block_reason="bonus_statistics_prefix_required",
        )
        if refresh_case != "ordinary" and phase[0] != "prefix" and not pending_notice:
            pending_notice.append((route, document))
            page.evaluate("() => { window.notificationReadHeld = true; }")
            return
        route.fulfill(json=document)

    def budgets(route):
        """Acknowledge exactly one explicit prefix and expose the separately controlled planning result."""
        if route.request.method == "POST":
            assert route.request.post_data_json["used_kwh"] == 25
            phase[0] = after_confirmation
            budget.update(issue=None, confirmation=None, used_kwh=25, remaining_kwh=7475)
        route.fulfill(json={"contract_version": "contract", "budgets": [budget]})

    page.route("**/v1/notifications", notifications)
    page.route("**/v1/contracts/budgets", budgets)
    page.goto(url + "/contract")
    banner = page.get_by_role("region", name="Notifications")
    expect(banner).to_contain_text("Confirm earlier export")
    if refresh_case != "ordinary":
        page.wait_for_function(
            "document.getElementById('integration-version-status').hasAttribute('title')"
        )
        page.clock.fast_forward(14000)
    page.get_by_label("Export before reference (kWh)", exact=True).fill("25")
    page.get_by_role("button", name="Confirm opening volume", exact=True).click()
    if refresh_case != "ordinary":
        expect(page.get_by_label("Export before reference (kWh)", exact=True)).to_have_count(0)
        page.wait_for_function("window.notificationReadHeld === true")
        assert len(pending_notice) == 1
        page.locator("#integration-version-status").evaluate(
            "element => element.removeAttribute('title')"
        )
        with page.expect_response("**/setup/session"):
            page.clock.fast_forward(1100)
        page.wait_for_function(
            "document.getElementById('integration-version-status').hasAttribute('title')"
        )
        expect(
            banner.get_by_role("heading", name="Confirm earlier export", exact=True)
        ).to_be_visible()
        expect(banner).not_to_be_focused()
        if refresh_case == "failed_read":
            pending_notice[0][0].fulfill(status=503)
            expect(banner).to_contain_text("last known notifications")
            page.clock.fast_forward(16000)
        else:
            pending_notice[0][0].fulfill(json=pending_notice[0][1])
    expected_title = (
        "Calculating a new energy plan"
        if after_confirmation == "calculating"
        else "Prices or contract need attention"
    )
    expect(banner.get_by_role("heading", name=expected_title, exact=True)).to_be_visible()
    if refresh_case == "failed_read":
        expect(banner).not_to_be_focused()
    else:
        expect(banner).to_be_focused()
    expect(banner).not_to_contain_text("Confirm earlier export")
    expect(banner.get_by_text("Technical details", exact=True)).to_have_count(0)
    if after_confirmation == "calculating":
        expect(
            banner.get_by_text(
                "The inputs are usable again. OEP is calculating and checking a new energy plan. "
                "This can take a few minutes. Planning has not yet completed successfully.",
                exact=True,
            )
        ).to_be_visible()
        page.screenshot(
            path=f"results/ci/notification-calculating-{page.viewport_size['width']}.png"
        )
        phase[0] = "ready"
        page.clock.fast_forward(16000)
        expect(banner).to_be_hidden()
    else:
        expect(banner).not_to_contain_text("Calculating a new energy plan")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_negative_export_advisory_is_readable_and_clears_without_confirmation(app_page):
    """Render the real advisory on desktop/mobile and remove it after a successful fresh read."""
    page, url = app_page
    page.clock.install()
    result = notification_document(
        "ready", "validated", None, negative_export_meters=("sensor.export",)
    )
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/contract")
    banner = page.get_by_role("region", name="Notifications")
    expect(banner).to_contain_text("Export statistics are below the period reference")
    expect(banner).to_contain_text("intentional statistics correction or incorrect meter data")
    expect(banner).to_contain_text("Further export can make the difference positive again")
    expect(banner.get_by_text("Technical details", exact=True)).to_have_count(0)
    expect(banner).to_contain_text("sensor.export")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    result["notifications"] = []
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(banner).to_be_hidden()


def blocked():
    """Declare an independent annual-history problem and its existing recovery page."""
    return {
        "notifications": [
            {
                "code": "bonus_counter_baseline_required",
                "level": "warning",
                "title": "Export bonus needs attention",
                "message": "Export history is incomplete. Check Recorder and export meters. No new plan can be published.",
                "action_label": "Review contract",
                "action_path": "/contract",
                "since": "2026-10-30T12:45:00+00:00",
                "detail": "incomplete_or_invalid_history",
            }
        ]
    }


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize(
    "path",
    [
        "/setup",
        "/overview",
        "/contract",
        "/quality",
        "/ev",
        "/thermal",
        "/appliances",
        "/appliance-settings",
        "/appliance-settings/ev",
        "/appliance-settings/thermal",
        "/data-storage",
    ],
)
def test_center_is_available_on_every_page_and_details_only_on_the_recovery_page(app_page, path):
    """Desktop/mobile and direct/ingress share active notices and clear both views without writes."""
    page, url = app_page
    if path == "/contract":
        discover(page, url)
    page.clock.install()
    result = blocked()
    requests = []
    writes = []

    def respond(route):
        """Check the real request authority while returning controlled changing server status."""
        if "/api/hassio_ingress/" not in url:
            assert route.request.headers.get("authorization") == "Bearer " + TOKEN
        requests.append(route.request.url)
        route.fulfill(json=result)

    page.route("**/v1/notifications", respond)
    page.on(
        "request",
        lambda request: (
            writes.append(request.url)
            if request.method in {"POST", "PUT", "DELETE"}
            and not request.url.endswith("/v1/contracts/preview")
            else None
        ),
    )
    page.goto(url + path)
    banner = page.get_by_role("region", name="Notifications")
    bell = page.get_by_role("button", name="Notifications: 1 active", exact=True)
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    if "/api/hassio_ingress/" not in url:
        expect(banner).to_be_hidden()
        expect(bell).to_be_hidden()
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(bell).to_be_visible()
    if path == "/contract":
        expect(banner.get_by_role("heading", name="Export bonus needs attention")).to_be_visible()
        expect(banner).to_contain_text(result["notifications"][0]["message"])
        expect(banner).to_contain_text("Blocked since:")
        expect(banner.get_by_text("Technical details", exact=True)).to_have_count(0)
        expect(banner).not_to_contain_text("bonus_counter_baseline_required")
        expect(banner).not_to_contain_text("incomplete_or_invalid_history")
        assert banner.get_by_role("article").count() == 1
        access = "ingress" if "/api/hassio_ingress/" in url else "direct"
        page.screenshot(
            path=f"results/ci/notification-detail-{page.viewport_size['width']}-{access}.png"
        )
    else:
        expect(banner).to_be_hidden()
    bell.click()
    expect(popup).to_be_visible()
    assert (
        popup.get_by_role("link", name="Export bonus needs attention").get_attribute("href")
        == url + "/contract#planning-notifications"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if path == "/overview":
        page.evaluate("document.fonts.ready")
        access = "ingress" if "/api/hassio_ingress/" in url else "direct"
        page.screenshot(
            path=f"results/ci/notification-center-{page.viewport_size['width']}-{access}.png"
        )
    initial = len(requests)
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(popup.get_by_role("link")).to_have_count(1)
    expect(popup).to_be_visible()
    result["notifications"] = []
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(banner).to_be_hidden()
    expect(bell).to_be_hidden()
    expect(popup).to_be_hidden()
    assert len(requests) > initial
    assert writes == []


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_status_outage_preserves_last_known_blocker_and_recovery_link_prefix(app_page):
    """Lost reads never imply recovery and a later successful empty response clears stale evidence."""
    page, url = app_page
    page.clock.install()
    result = blocked()
    mode = ["ok"]

    def respond(route):
        """Exercise transport failure, renewed success and revoked authorization without source writes."""
        if mode[0] == "offline":
            route.abort()
        elif mode[0] == "unauthorized":
            route.fulfill(status=401)
        else:
            route.fulfill(json=result)

    page.route("**/v1/notifications", respond)
    page.goto(url + "/contract")
    banner = page.get_by_role("region", name="Notifications")
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(banner).to_contain_text("Export bonus needs attention")
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    mode[0] = "offline"
    page.clock.fast_forward(16000)
    expect(banner).to_contain_text("last known notifications")
    expect(banner).to_contain_text("Export bonus needs attention")
    mode[0] = "ok"
    result["notifications"] = []
    page.clock.fast_forward(16000)
    expect(banner).to_be_hidden()
    result.update(blocked())
    page.clock.fast_forward(16000)
    expect(banner).to_contain_text("Export bonus needs attention")
    mode[0] = "unauthorized"
    page.clock.fast_forward(16000)
    expect(banner).to_be_hidden()
    page.get_by_role("button", name="Notifications: Status unavailable", exact=True).click()
    expect(popup).not_to_contain_text("Export bonus needs attention")
    expect(popup).to_contain_text("Sign in again")
    page.clock.fast_forward(16000)
    expect(popup).to_contain_text("Sign in again")
    expect(popup).not_to_contain_text("Export bonus needs attention")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_notification_text_is_inert_and_embedded_recovery_keeps_the_workspace(app_page):
    """Provider text cannot become HTML and embedded recovery links keep the same application shell."""
    page, url = app_page
    result = deepcopy(blocked())
    result["notifications"][0]["title"] = '<img src=x onerror="alert(1)">'
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/workspace?page=/overview")
    frame = page.frame_locator("#workspace-content")
    banner = frame.get_by_role("region", name="Notifications")
    expect(banner).to_be_hidden()
    page.get_by_role("button", name="Notifications: 1 active", exact=True).click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(popup).to_contain_text('<img src=x onerror="alert(1)">')
    assert popup.locator("img").count() == 0
    link = popup.get_by_role("link", name='<img src=x onerror="alert(1)">')
    assert link.get_attribute("href") == url + "/contract?embedded=1#planning-notifications"
    link.click()
    expect(frame.get_by_role("heading", name="Contract settings", exact=True)).to_be_visible()
    expect(banner).to_contain_text('<img src=x onerror="alert(1)">')
    expect(banner).to_be_focused()
    assert frame.get_by_role("button", name="Notifications: 1 active", exact=True).count() == 0
    assert page.locator("#workspace-content").count() == 1


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_malformed_status_cannot_clear_last_known_notice_or_follow_external_actions(app_page):
    """Unsafe links stay inert and invalid fresh payloads are shown as stale, never as recovery."""
    page, url = app_page
    page.clock.install()
    result = blocked()
    result["notifications"][0]["action_path"] = "https://example.invalid/contract"
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/overview")
    banner = page.get_by_role("region", name="Notifications")
    expect(banner).to_be_hidden()
    page.get_by_role("button", name="Notifications: 1 active", exact=True).click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(popup).to_contain_text("Export bonus needs attention")
    assert popup.get_by_role("link").count() == 0
    expect(popup.locator(".notification-expiry")).to_have_count(0)
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    result["notifications"][0]["auto_clear_minutes"] = "5"
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(popup).to_contain_text("last known notifications")
    expect(popup.locator(".notification-expiry")).to_have_count(0)
    result["notifications"] = None
    page.clock.fast_forward(16000)
    expect(popup).to_contain_text("last known notifications")
    expect(popup).to_contain_text("Export bonus needs attention")
    assert errors == []


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_notice_time_follows_newly_discovered_home_assistant_timezone(app_page):
    """Before discovery label UTC explicitly, then reformat the same incident using HA's timezone."""
    page, url = app_page
    page.clock.install()
    metadata = {"ingress": True, "timezone": None}
    page.route("**/setup/session", lambda route: route.fulfill(json=metadata))
    page.route("**/v1/notifications", lambda route: route.fulfill(json=blocked()))
    page.goto(url + "/contract")
    banner = page.get_by_role("region", name="Notifications")
    expect(banner).to_contain_text("2026-10-30 12:45 UTC")
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    metadata["timezone"] = "Europe/Amsterdam"
    page.clock.fast_forward(16000)
    expect(banner).to_contain_text("1:45 PM")
    expect(banner).not_to_contain_text("12:45 UTC")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_empty_center_appears_for_a_new_notice_and_both_views_disappear_after_recovery(app_page):
    """A healthy read shows no bell, a later issue appears, and confirmed recovery clears both views."""
    page, url = app_page
    discover(page, url)
    page.clock.install()
    result = {"notifications": []}
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/contract")
    bell = page.locator(".notification-toggle")
    banner = page.get_by_role("region", name="Notifications")
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    expect(bell).to_be_hidden()
    expect(banner).to_be_hidden()
    page.screenshot(path=f"results/ci/notification-empty-{page.viewport_size['width']}.png")
    result.update(blocked())
    page.clock.fast_forward(16000)
    expect(bell).to_have_accessible_name("Notifications: 1 active")
    expect(banner).to_contain_text("Export bonus needs attention")
    bell.click()
    expect(popup).to_be_visible()
    result["notifications"][0].update(
        code="planning_resuming", title="Planning is resuming", level="info"
    )
    page.clock.fast_forward(16000)
    expect(banner).to_contain_text("Planning is resuming")
    expect(popup).to_contain_text("Planning is resuming")
    result["notifications"] = []
    page.clock.fast_forward(16000)
    expect(bell).to_be_hidden()
    expect(popup).to_be_hidden()
    expect(banner).to_be_hidden()


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_center_keyboard_dismissal_layout_and_multiple_recovery_destinations(app_page):
    """The bell stays in desktop/mobile navigation, supports Escape and routes each issue independently."""
    page, url = app_page
    result = blocked()
    second = deepcopy(result["notifications"][0])
    second.update(
        code="required_measurement_unavailable",
        title="Required measurements are unavailable",
        action_path="/setup",
        action_label="Check installation",
    )
    result["notifications"].append(second)
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/contract")
    bell = page.get_by_role("button", name="Notifications: 2 active", exact=True)
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(bell).to_be_visible()
    container = ".settings-mobilebar" if page.viewport_size["width"] <= 640 else ".sidebar"
    assert (
        bell.locator("xpath=..")
        .locator("xpath=..")
        .evaluate("(element, selector) => element.matches(selector)", container)
    )
    colors = bell.locator(".notification-count").evaluate(
        "element => { const style = getComputedStyle(element); return [style.color, style.backgroundColor]; }"
    )

    def luminance(color):
        """Apply the WCAG sRGB transfer function to the actual rendered badge colors."""
        channels = [
            float(channel) / 255 for channel in color.split("(")[1].split(")")[0].split(",")[:3]
        ]
        linear = [
            channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return sum(
            channel * weight
            for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
        )

    light, dark = sorted((luminance(color) for color in colors), reverse=True)
    assert (light + 0.05) / (dark + 0.05) >= 4.5
    bell.focus()
    bell.press("Enter")
    expect(popup).to_be_visible()
    expect(popup.get_by_role("button", name="Close notifications")).to_be_focused()
    assert popup.get_by_role("link").count() == 2
    assert popup.evaluate(
        "element => { const box = element.getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth && box.bottom <= innerHeight; }"
    )
    popup.press("Escape")
    expect(popup).to_be_hidden()
    expect(bell).to_be_focused()
    bell.click()
    popup.get_by_role("link", name="Required measurements are unavailable").click()
    banner = page.get_by_role("region", name="Notifications")
    expect(banner).to_contain_text("Required measurements are unavailable")
    expect(banner).not_to_contain_text("Export bonus needs attention")
    expect(banner).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.get_by_role("button", name="Notifications: 2 active", exact=True)).to_be_visible()


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("failure", ["offline", 401, 403])
def test_first_read_failure_shows_unknown_status_instead_of_a_healthy_empty_center(
    app_page, failure
):
    """An initial network failure has a visible unknown-status indicator without inventing a blocker."""
    page, url = app_page
    page.route(
        "**/v1/notifications",
        lambda route: route.abort() if failure == "offline" else route.fulfill(status=failure),
    )
    page.goto(url + "/overview")
    page.get_by_role("button", name="Notifications: Status unavailable", exact=True).click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(popup).to_contain_text(
        "Notification status is unavailable" if failure == "offline" else "Sign in again"
    )
    assert popup.get_by_role("link").count() == 0
    expect(page.get_by_role("region", name="Notifications")).to_be_hidden()


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("workspace", [False, True])
def test_center_navigation_keeps_unsaved_drafts_when_native_confirmation_is_cancelled(
    app_page, workspace
):
    """Selecting a recovery destination cannot silently discard a direct or embedded configuration draft."""
    page, url = app_page
    discover(page, url)
    page.route("**/v1/notifications", lambda route: route.fulfill(json=blocked()))
    page.goto(url + ("/workspace?page=%2Fsetup" if workspace else "/setup"))
    content = page.frame_locator("#workspace-content") if workspace else page
    expect(content.locator("#installation-shell")).to_be_visible()
    content.get_by_role("button", name="Continue", exact=True).click()
    limit = content.get_by_label("Maximum grid import (kW)", exact=True)
    limit.fill("6.3")
    page.get_by_role("button", name="Notifications: 1 active", exact=True).click()
    dialogs = []

    def keep_draft(dialog):
        """Reject the browser's actual navigation prompt after notification selection."""
        dialogs.append(dialog.type)
        dialog.dismiss()

    page.once("dialog", keep_draft)
    page.get_by_role("dialog", name="Notifications", exact=True).get_by_role(
        "link", name="Export bonus needs attention"
    ).click(no_wait_after=True)
    expect(limit).to_have_value("6.3")
    assert dialogs == ["beforeunload"]


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_shell_drops_private_notices_when_a_recovery_document_cannot_initialize(app_page):
    """A navigation error clears the old document's private center instead of retaining its notices."""
    page, url = app_page
    page.route("**/v1/notifications", lambda route: route.fulfill(json=blocked()))
    page.route("**/contract?embedded=1", lambda route: route.fulfill(status=401, body="Sign in"))
    page.goto(url + "/workspace?page=/overview")
    page.get_by_role("button", name="Notifications: 1 active", exact=True).click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    popup.get_by_role("link", name="Export bonus needs attention").click()
    page.get_by_role("button", name="Notifications: Status unavailable", exact=True).click()
    expect(popup).to_contain_text("Notification status is unavailable")
    expect(popup).not_to_contain_text("Export bonus needs attention")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("previous_active", [False, True])
@pytest.mark.parametrize("read_fails", [False, True])
def test_workspace_navigation_does_not_turn_notification_loading_into_a_warning(
    app_page, previous_active, read_fails
):
    """Keep the center stable while a new page reads status, then show recovery or a genuine failure."""
    page, url = app_page
    held_reads = []
    reading_next_page = False

    def notifications(route):
        """Hold the next document's real read independently of its load event."""
        if reading_next_page:
            held_reads.append(route)
            page.evaluate("window.notificationReadPending = true")
        else:
            route.fulfill(json=blocked() if previous_active else {"notifications": []})

    page.route("**/v1/notifications", notifications)
    with page.expect_response("**/v1/notifications"):
        page.goto(url + "/workspace?page=/overview")
    center = page.locator(".notification-center")
    content = page.frame_locator("#workspace-content")
    expect(content.locator("#planning-notifications")).to_be_attached()
    if previous_active:
        expect(
            page.get_by_role("button", name="Notifications: 1 active", exact=True)
        ).to_be_visible()
    else:
        expect(center).to_be_hidden()
    reading_next_page = True
    open_primary_navigation(page)
    page.get_by_role("navigation", name="Primary navigation").get_by_role(
        "link", name="Contract", exact=True
    ).click()
    page.wait_for_function("window.notificationReadPending === true")
    page.wait_for_function(
        """() => {
            const content = document.querySelector('#workspace-content').contentDocument;
            return new URL(content.URL).pathname.endsWith('/contract') && content.readyState === 'complete';
        }"""
    )
    if previous_active:
        expect(
            page.get_by_role("button", name="Notifications: 1 active", exact=True)
        ).to_be_visible()
    else:
        expect(center).to_be_hidden()
        if not read_fails:
            page.screenshot(
                path=f"results/ci/notification-navigation-{page.viewport_size['width']}.png"
            )
    for route in held_reads:
        if read_fails:
            route.abort()
        else:
            route.fulfill(json={"notifications": []})
    if read_fails:
        page.get_by_role("button", name="Notifications: Status unavailable", exact=True).click()
        expect(page.get_by_role("dialog", name="Notifications", exact=True)).to_contain_text(
            "Notification status is unavailable"
        )
    else:
        expect(center).to_be_hidden()
