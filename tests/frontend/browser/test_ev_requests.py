"""Exercise complete charging-request creation, editing, repetition and cancellation through the real app."""

from pathlib import Path

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
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]["target_soc"] == 80
    )
    card.get_by_role("button", name="Edit charging request", exact=True).click()
    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=f"results/ci/ev-request-editing-{page.viewport_size['width']}.png", full_page=True
    )
    card.get_by_role("button", name="Cancel changes", exact=True).click()
