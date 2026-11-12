"""Exercise complete charging-request creation, editing, repetition and cancellation through the real app."""

from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.ev_request_fixture import configure_ev
from tests.frontend.browser.helpers import TOKEN


def test_complete_charging_request_keeps_defaults_and_retains_cancelled_history(app_page):
    """One saved request replaces per-field actions and is never recreated by a refresh."""
    page, url = app_page
    headers, before = configure_ev(page, url)
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    card = page.locator("#vehicle-controls > article").first
    expect(card).to_contain_text("No planned charging request")
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    target = card.get_by_label("Target state of charge (%)", exact=True)
    expect(target).to_have_value("80.0")
    expect(card.get_by_label("Repeat on selected weekdays", exact=True)).not_to_be_checked()
    target.fill("65")
    page.locator("#refresh-evidence").click()
    expect(target).to_have_value("65")
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    expect(card).to_contain_text("Planned charging request: Target: 65.0%")
    saved = page.request.get(url + "/v1/ev", headers=headers).json()
    request = saved["vehicles"][0]["charging_request"]
    assert saved["vehicles"][0]["target_soc"] == 80
    assert saved["vehicles"][0]["departure_time"] == before["vehicles"][0]["departure_time"]
    assert request["repeat_weekdays"] == []
    card.get_by_role("button", name="Edit charging request", exact=True).click()
    target.fill("70")
    card.get_by_role("button", name="Save changes", exact=True).click()
    expect(card).to_contain_text("Target: 70.0%")
    edited = page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]
    assert edited["charging_request"]["id"] == request["id"]
    assert edited["target_soc"] == 80
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=f"results/ci/ev-request-planned-{page.viewport_size['width']}.png", full_page=True
    )
    card.get_by_role("button", name="Cancel charging request", exact=True).click()
    dialog = page.get_by_role("alertdialog", name="Cancel charging request?", exact=True)
    expect(dialog).to_contain_text("Car A")
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(card).to_contain_text("Planned charging request")
    card.get_by_role("button", name="Cancel charging request", exact=True).click()
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Cancel charging request", exact=True
    ).click()
    expect(card).to_contain_text("No planned charging request")
    expect(page.locator("#status")).to_contain_text("Charging request cancelled")
    expect(page.locator("#request-history")).to_contain_text("Cancelled")
    page.locator("#refresh-evidence").click()
    expect(card).to_contain_text("No planned charging request")
    assert (
        page.request.get(url + "/v1/ev/requests", headers=headers).json()["requests"][0]["status"]
        == "cancelled"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_repeat_is_explicit_and_a_rejected_complete_request_retains_all_edits(app_page):
    """A failed atomic write preserves target, time and selected weekdays without changing defaults."""
    page, url = app_page
    headers, _before = configure_ev(page, url)
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    card = page.locator("#vehicle-controls > article").first
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    card.get_by_label("Target state of charge (%)", exact=True).fill("75")
    card.get_by_label("Repeat on selected weekdays", exact=True).check()
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    bounds = card.locator("fieldset").evaluate(
        "field => [field.getBoundingClientRect().bottom, field.nextElementSibling.getBoundingClientRect().top]"
    )
    assert bounds[0] <= bounds[1]
    for weekday in ("Tuesday", "Thursday", "Saturday", "Sunday"):
        card.get_by_label(weekday, exact=True).uncheck()
    submissions = []
    page.on(
        "request",
        lambda request: (
            submissions.append(request.post_data_json)
            if request.url == url + "/v1/ev/requests" and request.method == "POST"
            else None
        ),
    )
    page.route(
        "**/v1/ev/requests",
        lambda route: (
            route.fulfill(status=409, body="Charging request changed. Refresh before saving")
            if route.request.method == "POST"
            else route.fallback()
        ),
    )
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    expect(card.get_by_role("status")).to_contain_text("Refresh before saving")
    expect(card.get_by_label("Target state of charge (%)", exact=True)).to_have_value("75.0")
    expect(card.get_by_label("Monday", exact=True)).to_be_checked()
    expect(card.get_by_label("Tuesday", exact=True)).not_to_be_checked()
    page.unroute("**/v1/ev/requests")
    page.locator("#refresh-evidence").click()
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    expect(card).to_contain_text("Repeats: Monday, Wednesday, Friday")
    request = page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0][
        "charging_request"
    ]
    assert request["repeat_weekdays"] == [0, 2, 4]
    assert len(submissions) == 2
    assert submissions[0]["request_id"] == submissions[1]["request_id"] == request["id"]
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]["target_soc"] == 80
    )
    card.get_by_role("button", name="Edit charging request", exact=True).click()
    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=f"results/ci/ev-request-editing-{page.viewport_size['width']}.png", full_page=True
    )
    card.get_by_role("button", name="Cancel changes", exact=True).click()


@pytest.mark.parametrize("failed_read", ["state", "history"])
def test_confirmed_request_stays_confirmed_when_followup_read_fails(app_page, failed_read):
    """Durable POST success remains visible after either subsequent GET fails, without a retryable draft."""
    page, url = app_page
    headers, _before = configure_ev(page, url)
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    card = page.locator("#vehicle-controls > article").first
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    card.get_by_label("Target state of charge (%)", exact=True).fill("65")
    pattern = "**/v1/ev" if failed_read == "state" else "**/v1/ev/requests"
    page.route(
        pattern,
        lambda route: (
            route.fulfill(status=503, body="Synthetic read unavailable")
            if route.request.method == "GET"
            else route.fallback()
        ),
    )
    card.get_by_role("button", name="Plan charging request", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Charging request saved")
    expect(page.locator("#status")).to_contain_text("Refresh")
    expect(card.get_by_label("Target state of charge (%)", exact=True)).to_have_count(0)
    page.unroute(pattern)
    stored = page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]
    assert stored["charging_request"]["target_soc"] == 65
    assert len(page.request.get(url + "/v1/ev/requests", headers=headers).json()["requests"]) == 1
    page.locator("#refresh-evidence").click()
    expect(card).to_contain_text("Target: 65.0%")
    page.route(
        pattern,
        lambda route: (
            route.fulfill(status=503, body="Synthetic read unavailable")
            if route.request.method == "GET"
            else route.fallback()
        ),
    )
    card.get_by_role("button", name="Cancel charging request", exact=True).click()
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Cancel charging request", exact=True
    ).click()
    expect(page.locator("#status")).to_contain_text("Charging request cancelled")
    expect(page.locator("#status")).to_contain_text("Refresh")
    expect(card).to_contain_text("No planned charging request")
    page.unroute(pattern)
    assert (
        page.request.get(url + "/v1/ev/requests", headers=headers).json()["requests"][0]["status"]
        == "cancelled"
    )


def test_delayed_history_cannot_restore_a_request_after_cancel(app_page):
    """An older in-flight history page cannot overwrite the freshly confirmed cancelled outcome."""
    page, url = app_page
    headers, before = configure_ev(page, url)
    vehicle_id = before["vehicles"][0]["id"]
    assert page.request.post(
        url + "/v1/ev/requests",
        headers=headers,
        data={"request_id": str(uuid4()), "vehicle_id": vehicle_id},
    ).ok
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    history = page.locator("#request-history")
    expect(history).to_contain_text("Planned")
    old_history = page.request.get(url + "/v1/ev/requests", headers=headers).json()
    held = []

    def delay_first_history(route):
        """Hold just the first old GET while allowing the new post-cancel history to finish."""
        if route.request.method == "GET" and not held:
            held.append(route)
        else:
            route.fallback()

    page.route("**/v1/ev/requests", delay_first_history)
    with page.expect_request(
        lambda request: request.url == url + "/v1/ev/requests" and request.method == "GET"
    ):
        page.locator("#refresh-evidence").click()
    page.locator("#vehicle-controls > article").first.get_by_role(
        "button", name="Cancel charging request", exact=True
    ).click()
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Cancel charging request", exact=True
    ).click()
    expect(history).to_contain_text("Cancelled")
    held[0].fulfill(json=old_history)
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(history).to_contain_text("Cancelled")
    expect(history).not_to_contain_text("Planned")


def test_same_history_cursor_is_requested_and_appended_once(app_page):
    """Double activation of Load more cannot fetch or append one history page twice."""
    page, url = app_page
    configure_ev(page, url)
    rows = [
        {
            "id": str(uuid4()),
            "vehicle_name": f"History car {index}",
            "status": "cancelled",
            "target_soc": 65,
            "departure": "2026-12-01T06:00:00+00:00",
            "repeat_weekdays": [],
        }
        for index in range(26)
    ]
    held = []

    def history(route):
        """Serve an independent 25+1 page boundary and hold every duplicate next-page call."""
        if "cursor=" in route.request.url:
            held.append(route)
        else:
            route.fulfill(json={"requests": rows[:25], "next_cursor": "next-page"})

    page.route("**/v1/ev/requests{,?*}", history)
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#request-history > p")).to_have_count(25)
    with page.expect_request("**/v1/ev/requests?cursor=next-page"):
        page.locator("#more-history").evaluate("button => { button.click(); button.click(); }")
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    for route in held:
        route.fulfill(json={"requests": rows[25:], "next_cursor": None})
    expect(page.locator("#request-history > p")).to_have_count(26)
    assert len(held) == 1
