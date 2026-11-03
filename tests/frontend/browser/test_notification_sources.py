"""Exercise arbitrary future notification sources without browser knowledge of their causes."""

from copy import deepcopy

import pytest
from playwright.sync_api import expect


def example_notice(owner="example", resource="one"):
    """Return independently specified wire evidence for an imaginary module and resource."""
    return {
        "key": f"{owner}:{resource}",
        "owner": owner,
        "code": "input_unavailable",
        "title": f"Example resource {resource} needs attention",
        "message": "Check its input source.",
        "level": "warning",
        "state": "active",
        "blocks": ["example_operation"],
        "action_label": "Review resource",
        "action_path": "/data-storage",
        "since": "2026-10-31T10:00:00+00:00",
        "detail": None,
        "stale": False,
    }


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("start_path", ["/quality", "/data-storage"])
def test_recovery_navigation_keeps_the_full_explanation_below_the_mobile_header(
    app_page, start_path
):
    """Selecting any cause exposes its complete heading and message below sticky navigation on arrival."""
    page, url = app_page
    notice = example_notice()
    notice["action_path"] = "/quality"
    page.route(
        "**/v1/notifications",
        lambda route: route.fulfill(json={"notifications": [notice], "unavailable_sources": []}),
    )
    page.goto(url + start_path)
    page.locator(".notification-toggle").click()
    page.get_by_role("dialog", name="Notifications", exact=True).get_by_role(
        "link", name=notice["title"], exact=True
    ).click()
    expect(page).to_have_url(url + "/quality#planning-notifications")
    banner = page.get_by_role("region", name="Notifications", exact=True)
    expect(banner).to_be_focused()
    page.evaluate("document.fonts.ready")
    header = page.locator(".settings-mobilebar").bounding_box()
    minimum_top = header["y"] + header["height"] if header else 0
    for content in (
        banner.get_by_role("heading", name=notice["title"], exact=True),
        banner.get_by_text(notice["message"], exact=True),
    ):
        expect(content).to_be_visible()
        assert content.bounding_box()["y"] >= minimum_top
    page.screenshot(
        path=f"results/ci/notification-recovery-heading-{page.viewport_size['width']}-{start_path.strip('/')}.png"
    )


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("level", ["info", "warning", "error"])
def test_generic_source_unknown_restart_and_confirmed_recovery_share_both_views(app_page, level):
    """Desktop/mobile retain missing-source causes, update other owners and clear only confirmed recovery."""
    page, url = app_page
    page.clock.install()
    first, second = example_notice(), example_notice("another", "two")
    first["level"] = level
    if level == "warning":
        first["blocks"] = []
    result = {"notifications": [first, second], "unavailable_sources": []}
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/data-storage")
    banner = page.get_by_role("region", name="Notifications", exact=True)
    expect(banner).to_contain_text(first["title"])
    expect(banner).to_contain_text(second["title"])
    article = banner.locator("article").filter(has_text=first["title"])
    if level == "error":
        expect(article).to_have_class("planning-notice notification-error")
    elif level == "info":
        expect(article).to_have_class("planning-notice informational")
    else:
        expect(article).to_contain_text("Active since:")
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    bell = page.locator(".notification-toggle")
    bell.click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(popup).to_contain_text(first["title"])
    expect(popup).to_contain_text(second["title"])
    popup.get_by_role("button", name="Close notifications").click()

    # A restarted server can have no cached notice while its source is still unknown.
    result.update(notifications=[], unavailable_sources=["example"])
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(banner).to_contain_text(first["title"])
    expect(banner).not_to_contain_text(second["title"])
    expect(banner).to_contain_text("last known notifications")
    bell.click()
    expect(popup).to_contain_text(first["title"])
    expect(popup).not_to_contain_text(second["title"])
    page.screenshot(
        path=f"results/ci/generic-notification-stale-{level}-{page.viewport_size['width']}.png"
    )
    popup.get_by_role("button", name="Close notifications").click()

    result["unavailable_sources"] = []
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(banner).to_be_hidden()
    expect(bell).to_be_hidden()


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_first_unavailable_source_does_not_claim_retained_notifications(app_page):
    """An unknown first read explains missing status without inventing previous notification evidence."""
    page, url = app_page
    page.clock.install()
    page.route(
        "**/v1/notifications",
        lambda route: route.fulfill(json={"notifications": [], "unavailable_sources": ["example"]}),
    )
    page.goto(url + "/data-storage")
    page.locator(".notification-toggle").click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(popup).to_contain_text("Some notification sources are unavailable.")
    expect(popup).not_to_contain_text("last known notifications")
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    page.route("**/v1/notifications", lambda route: route.abort())
    page.clock.fast_forward(16000)
    expect(popup).to_contain_text("Notification status is unavailable.")
    expect(popup).not_to_contain_text("last known notifications")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_automatic_retry_notice_without_action_has_a_readable_explanation(app_page):
    """A future automatic-retry source needs no invented recovery link to explain itself."""
    page, url = app_page
    notice = example_notice()
    notice.update(action_label=None, action_path=None, message="This source retries automatically.")
    page.route(
        "**/v1/notifications",
        lambda route: route.fulfill(json={"notifications": [notice], "unavailable_sources": []}),
    )
    page.goto(url + "/data-storage")
    bell = page.locator(".notification-toggle")
    expect(bell).to_be_visible()
    bell.click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(popup).to_contain_text(notice["title"])
    expect(popup).to_contain_text("This source retries automatically.")
    expect(popup.get_by_role("link")).to_have_count(0)


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize("invalid", ["source_list", "lifecycle", "operations", "fallback"])
def test_invalid_source_metadata_cannot_clear_last_confirmed_problem(app_page, invalid):
    """Malformed source ownership, lifecycle or blocked operations preserve explicitly stale evidence."""
    page, url = app_page
    page.clock.install()
    notice = example_notice()
    result = {"notifications": [notice], "unavailable_sources": []}
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/data-storage")
    banner = page.get_by_role("region", name="Notifications", exact=True)
    expect(banner).to_contain_text(notice["title"])
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    if invalid == "source_list":
        result.update(notifications=[], unavailable_sources="invalid")
    else:
        broken = deepcopy(notice)
        broken[{"lifecycle": "state", "operations": "blocks", "fallback": "fallback"}[invalid]] = (
            "invalid"
        )
        result["notifications"] = [broken]
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(banner).to_contain_text(notice["title"])
    expect(banner).to_contain_text("last known notifications")


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize(
    "previous_fallback,current_fallback,retain",
    [(False, True, True), (True, True, False), (False, False, False)],
)
def test_unknown_owner_retains_authoritative_advice_over_a_same_key_fallback(
    app_page, previous_fallback, current_fallback, retain
):
    """Restart preserves specific advice against fallbacks while accepting fresh authoritative or peer fallback updates."""
    page, url = app_page
    page.clock.install()
    previous = example_notice()
    previous["fallback"] = previous_fallback
    result = {"notifications": [previous], "unavailable_sources": []}
    page.route("**/v1/notifications", lambda route: route.fulfill(json=result))
    page.goto(url + "/data-storage")
    banner = page.get_by_role("region", name="Notifications", exact=True)
    expect(banner).to_contain_text(previous["title"])
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    current = {
        **previous,
        "owner": "dependent",
        "title": "Current dependent evidence",
        "action_path": "/setup",
        "fallback": current_fallback,
    }
    result.update(notifications=[current], unavailable_sources=["example"])
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    if retain:
        expect(banner).to_contain_text(previous["title"])
        expect(banner).to_contain_text("last known notifications")
    else:
        expect(banner).to_be_hidden()
    bell = page.locator(".notification-toggle")
    bell.click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(popup.get_by_role("link")).to_have_count(1)
    selected = popup.get_by_role("link")
    expect(selected).to_have_text(previous["title"] if retain else current["title"])
    expect(selected).to_have_attribute(
        "href", url + ("/data-storage" if retain else "/setup") + "#planning-notifications"
    )
    popup.get_by_role("button", name="Close notifications").click()
    result["unavailable_sources"] = []
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(banner).to_be_hidden()
    bell.click()
    expect(popup.get_by_role("link")).to_have_text(current["title"])
    popup.get_by_role("button", name="Close notifications").click()
    result["notifications"] = []
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(bell).to_be_hidden()
