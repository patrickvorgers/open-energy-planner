"""Users compare actual stored attempts by HA day and inspect progress without activating plans."""

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_daily_calculation_chart_dialog_and_day_navigation(app_page):
    """Default to HA today, preserve exact durations, show unknown first times and allow keyboard details."""
    page, url = app_page
    discover(page, url)
    assert page.request.get(url + "/v1/planning-performance").status == 401
    page.goto(url + "/planning-performance")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    expect(page.get_by_label("Day", exact=True)).to_have_value(today.isoformat())
    expect(page.locator(".performance-row")).to_have_count(3)
    expect(page.locator("#performance-summary")).to_contain_text("3 completed calculations")
    action = page.get_by_role("button", name="View calculation at").first
    action.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#calculation-summary")).to_contain_text("4 s")
    expect(dialog.locator("#calculation-cost-note")).to_contain_text(re.compile(r"0[.,]06 EUR"))
    expect(dialog.locator("#calculation-proof")).to_contain_text(re.compile(r"2[.,]0%"))
    box = dialog.bounding_box()
    if page.viewport_size["width"] < 500:
        assert box["y"] == 0 and box["height"] == page.viewport_size["height"]
    capture = Path("results/ci/planning-performance")
    capture.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    page.screenshot(path=str(capture / f"detail-{width}.png"), full_page=False)
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(action).to_be_focused()
    page.screenshot(path=str(capture / f"overview-{width}.png"), full_page=True)
    page.get_by_role("button", name="View calculation at").last.click()
    expect(dialog.locator("#calculation-summary")).to_contain_text("not recorded")
    page.get_by_role("button", name="Close", exact=True).click()
    page.get_by_label("Day", exact=True).fill((today - timedelta(days=1)).isoformat())
    expect(page.locator(".performance-row")).to_have_count(1)
    page.get_by_label("Day", exact=True).fill("2020-01-01")
    expect(page.locator("#performance-summary")).to_contain_text("No completed calculations")
    page.get_by_role("button", name="Today", exact=True).click()
    expect(page.locator(".performance-row")).to_have_count(3)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    response = page.request.get(
        url + "/v1/planning-performance", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    summaries = response.json()["calculations"]
    assert all("progress" not in row for row in summaries)
    identity = summaries[0]["id"]
    assert page.request.get(url + "/v1/planning-performance/" + identity).status == 401
    page.goto(
        url
        + f"/planning-performance?day={today.isoformat()}&calculation={identity}&keep=1#selection"
    )
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(dialog).to_be_visible()
    expect(dialog.locator("#calculation-summary")).to_contain_text("4 s")
    assert (
        "calculation=" not in page.url and "keep=1" in page.url and page.url.endswith("#selection")
    )
    page.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#performance-status")).to_have_text("")
    expect(dialog).not_to_be_visible()
    page.route(
        "**/v1/planning-performance/" + identity,
        lambda route: route.fulfill(status=503, body="private details"),
    )
    page.get_by_role("button", name="View calculation at").first.click()
    expect(dialog.locator("#calculation-summary")).to_contain_text("could not be loaded")
    expect(dialog).not_to_contain_text("private details")


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_rejected_candidates_do_not_report_timeout_as_the_publication_cause(app_page):
    """Rejected candidates retain their rejection outcome, while genuine no-candidate timeouts stay visible."""
    page, url = app_page
    discover(page, url)
    response = page.request.get(
        url + "/v1/planning-performance", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status == 200
    evidence = response.json()
    reasons = ("candidate_rejected", "consumption_objective_rejected", "time_limit")
    for row, reason in zip(evidence["calculations"], reasons, strict=True):
        row.update(reason=reason, stop_reason="time_limit", plan_id=None)
    details = {}
    for row in evidence["calculations"]:
        detail = page.request.get(
            url + "/v1/planning-performance/" + row["id"],
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert detail.status == 200
        details[row["id"]] = {**detail.json(), **row}

    def respond(route):
        """Supply explicit presentation evidence through the same daily and detail wire contracts."""
        identity = route.request.url.rsplit("/", 1)[-1].split("?", 1)[0]
        route.fulfill(json=details.get(identity, evidence))

    page.route("**/v1/planning-performance**", respond)
    page.goto(url + "/planning-performance")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    rows = page.locator(".performance-row")
    expect(rows).to_have_count(3)
    dialog = page.get_by_role("dialog")
    for index in range(3):
        row = rows.nth(index)
        expect(row).to_contain_text("No plan published")
        if index < 2:
            expect(row).not_to_contain_text("Time budget reached")
        else:
            expect(row).to_contain_text("Time budget reached")
        row.get_by_role("button", name="View calculation at").click()
        summary = dialog.locator("#calculation-summary")
        expect(summary).to_contain_text("No plan published")
        if index < 2:
            expect(summary).not_to_contain_text("Time budget reached")
        else:
            expect(summary).to_contain_text("Time budget reached")
        if index == 0:
            captures = Path("results/ci/planning-performance")
            captures.mkdir(parents=True, exist_ok=True)
            width = page.viewport_size["width"]
            page.screenshot(path=str(captures / f"rejected-detail-{width}.png"), full_page=False)
        page.get_by_role("button", name="Close", exact=True).click()
        if index == 0:
            page.evaluate("window.scrollTo({top: 0, left: 0, behavior: 'instant'})")
            page.screenshot(path=str(captures / f"rejected-overview-{width}.png"), full_page=True)
