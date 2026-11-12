"""First-screen and Optimizer readiness do not depend on hidden panels or optional notices."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, open_primary_navigation


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_home_assistant_frame_loads_before_notices_and_simulations(app_page):
    """A real full-horizon Optimizer stays usable while optional scripts and comparisons are held."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    notices, simulations, savings = [], [], []
    page.route("**/notifications.js", lambda route: notices.append(route))
    page.route("**/comparison?view=simulations", lambda route: simulations.append(route))
    page.on(
        "request",
        lambda request: savings.append(request.url) if "/v1/savings?" in request.url else None,
    )
    page.route(
        url + "/startup-host",
        lambda route: route.fulfill(
            content_type="text/html",
            body=f'<iframe id="ha-app" src="{url}/workspace?page=%2Foverview" style="display:none;width:100vw;height:100vh;border:0" onload="this.style.display=\'block\'"></iframe>',
        ),
    )
    page.goto(url + "/startup-host", wait_until="domcontentloaded")
    shell = page.frame_locator("#ha-app")
    content = shell.frame_locator("#workspace-content")
    expect(content.get_by_role("heading", name="Planning", exact=True)).to_be_visible()
    assert notices
    content.get_by_label("Installation API token", exact=True).fill(TOKEN)
    content.get_by_role("button", name="Connect", exact=True).click()
    expect(content.locator("#plan-chart-legend")).to_contain_text("Grid import")
    assert simulations and not savings
    captures = Path("results/ci/fast-start")
    captures.mkdir(parents=True, exist_ok=True)
    for table in ("plan-rows", "plan-battery-rows", "plan-solar-rows", "expected-cost-rows"):
        expect(content.locator("#" + table + " tr")).to_have_count(0)
    content.get_by_text("Detailed interval energy", exact=True).click()
    expect(content.locator("#plan-rows tr")).to_have_count(120)
    content.get_by_text("Detailed interval energy", exact=True).click()
    for route in simulations:
        route.fulfill(response=route.fetch())
    expect(content.locator("#overview-status")).to_contain_text("Connected")
    with page.expect_request("**/v1/notifications") as notification_read:
        for route in notices:
            route.fulfill(response=route.fetch())
    assert notification_read.value.headers.get("authorization") == "Bearer " + TOKEN
    page.screenshot(
        path=str(captures / f"planning-{page.viewport_size['width']}.png"), full_page=True
    )
    open_primary_navigation(shell)
    shell.get_by_role("link", name="Savings", exact=True).click()
    expect(content.get_by_role("heading", name="Evaluated savings", exact=True)).to_be_visible()
    expect(content.locator("#coverage")).not_to_be_empty()
    assert len(savings) == 1


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_savings_opens_independently_and_failed_reads_retry(app_page):
    """Opening Savings requests no plans and a failed read retries only through an explicit refresh."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    plans = []
    page.on(
        "request", lambda request: plans.append(request.url) if "/v1/plans" in request.url else None
    )
    page.route("**/v1/savings?*", lambda route: route.fulfill(status=503))
    page.goto(url + "/overview#savings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#coverage")).to_contain_text("503")
    assert not plans
    page.unroute("**/v1/savings?*")
    with page.expect_response("**/v1/savings?*") as recovered:
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    assert recovered.value.status == 200
    expect(page.locator("#coverage")).to_contain_text("Quarters evaluated: 0 of 96")
    expect(page.locator("#coverage")).not_to_contain_text("503")
    assert not plans


def test_public_script_cache_is_reused_between_documents(app_page):
    """A second document reuses the same immutable shared module without another transfer."""
    page, url = app_page
    page.goto(url + "/overview")
    expect(
        page.get_by_role("button", name="Menu", exact=True, include_hidden=True)
    ).to_be_attached()
    page.goto(url + "/contract")
    expect(
        page.get_by_role("button", name="Menu", exact=True, include_hidden=True)
    ).to_be_attached()
    entries = page.evaluate(
        "performance.getEntriesByType('resource').filter(entry => entry.name.endsWith('/settings-actions.js')).map(entry => ({name:entry.name,size:entry.transferSize,decoded:entry.decodedBodySize}))"
    )
    assert len(entries) == 1
    assert "/setup/static/" in entries[0]["name"]
    assert entries[0]["size"] == 0 and entries[0]["decoded"] > 0


def test_optional_notification_module_failure_keeps_navigation_and_recovers(app_page):
    """An optional script failure has explicit recovery advice while the ordinary menu remains usable."""
    page, url = app_page
    page.route("**/notifications.js", lambda route: route.fulfill(status=503))
    page.goto(url + "/overview")
    open_primary_navigation(page)
    expect(
        page.get_by_text(
            "Notifications could not be loaded. Reload this page to retry.", exact=True
        )
    ).to_be_visible()
    page.get_by_role("region", name="Settings", exact=True).get_by_role(
        "link", name="Contract", exact=True
    ).click()
    expect(page.get_by_role("heading", name="Contract settings", exact=True)).to_be_visible()
    page.unroute("**/notifications.js")
    page.reload()
    expect(
        page.get_by_text(
            "Notifications could not be loaded. Reload this page to retry.", exact=True
        )
    ).to_have_count(0)
    expect(
        page.get_by_role("button", name=re.compile(r"^Notifications"), include_hidden=True)
    ).to_be_attached()


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_late_shell_notifications_recover_the_content_result(app_page):
    """The optional shell module recovers the child's confirmed status after missing its original event."""
    page, url = app_page
    held = []
    payload = {
        "notifications": [
            {
                "code": "synthetic_notice",
                "level": "warning",
                "title": "Synthetic source needs attention",
                "message": "Review the synthetic source.",
                "action_path": "/overview",
                "action_label": "Review planning",
                "since": "2026-11-04T00:00:00+00:00",
            }
        ]
    }
    page.route(
        "**/v1/notifications",
        lambda route: route.fulfill(json=payload),
    )
    page.route(
        "**/notifications.js",
        lambda route: (
            held.append(route) if route.request.frame.parent_frame is None else route.continue_()
        ),
    )
    page.goto(url + "/workspace?page=%2Foverview", wait_until="domcontentloaded")
    content = page.frame_locator("#workspace-content")
    expect(content.locator("#planning-notifications")).to_contain_text(
        "Synthetic source needs attention"
    )
    assert len(held) == 1
    held[0].fulfill(response=held[0].fetch())
    open_primary_navigation(page)
    expect(page.get_by_role("button", name="Notifications: 1 active", exact=True)).to_be_visible()


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_early_connection_loads_notices_after_the_initial_session_read_fails(app_page):
    """A buffered standalone token immediately reads real authenticated notices after module binding."""
    page, url = app_page
    beginning = datetime.now(UTC)
    page.clock.install(time=beginning)
    page.clock.pause_at(beginning + timedelta(minutes=1))
    ready = False
    held = []

    def session(route):
        """Fail initial discovery but allow Connect's explicit initialization retry."""
        if ready:
            route.continue_()
        else:
            route.fulfill(status=503)

    page.route("**/setup/session", session)
    page.route("**/notifications.js", lambda route: held.append(route))
    page.goto(url + "/overview")
    expect(page.locator("#overview-status")).to_contain_text("Cannot initialize this view")
    ready = True
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#plan-chart-legend")).to_contain_text("Grid import")
    assert len(held) == 1
    assert page.request.get(url + "/v1/notifications").status == 401
    with page.expect_response("**/v1/notifications", timeout=5000) as notification:
        held[0].fulfill(response=held[0].fetch())
    assert notification.value.status == 200
    assert notification.value.request.headers.get("authorization") == "Bearer " + TOKEN
    notices = notification.value.json()["notifications"]
    assert notices
    expect(
        page.get_by_role("button", name=f"Notifications: {len(notices)} active", exact=True)
    ).to_be_visible()
    assert TOKEN not in json.dumps(page.evaluate("document.oepNotificationState"))
