"""Large daily calculation lists stay compact without losing chart evidence or detail identity."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.fixture
def daily_history(app_page):
    """Serve 96 independent synthetic quarter-hour summaries through the existing daily wire contract."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = page.request.get(url + "/v1/planning-performance", headers=headers)
    assert response.ok
    evidence = response.json()
    template = evidence["calculations"][0]
    detail = page.request.get(
        url + "/v1/planning-performance/" + template["id"], headers=headers
    ).json()
    start = datetime.now(ZoneInfo("Europe/Amsterdam")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    rows = []
    for index in range(96):
        duration = 7 + (index % 9) * 1.5 + max(0, 24 - abs(index - 32) * 3)
        rows.append(
            {
                **template,
                "id": f"synthetic-calculation-{index:03}",
                "started_at": (start + timedelta(minutes=15 * index)).astimezone(UTC).isoformat(),
                "duration_seconds": duration,
                "first_candidate_seconds": None if index % 13 == 0 else 4,
                "reason": "candidate_rejected" if index == 11 else "published",
            }
        )
    evidence = {**evidence, "calculations": rows}
    detail_reads, daily_reads = [], []

    def respond(route):
        """Return literal dated evidence and record exact requested identities without changing stored history."""
        address = urlsplit(route.request.url)
        identity = address.path.rsplit("/", 1)[-1]
        if identity != "planning-performance":
            detail_reads.append(identity)
            selected = next(row for row in rows if row["id"] == identity)
            route.fulfill(json={**detail, **selected})
            return
        selected_day = parse_qs(address.query).get("day", [evidence["day"]])[0]
        daily_reads.append(selected_day)
        previous = (start - timedelta(days=1)).date().isoformat()
        selected_rows = rows if selected_day == evidence["day"] else []
        if selected_day == previous:
            selected_rows = [
                {**rows[0], "started_at": (start - timedelta(days=1)).astimezone(UTC).isoformat()}
            ]
        route.fulfill(json={**evidence, "day": selected_day, "calculations": selected_rows})

    page.route("**/v1/planning-performance**", respond)
    page.goto(url + "/planning-performance")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#performance-summary")).to_contain_text("96 completed calculations")
    return page, evidence, detail_reads, daily_reads


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_compact_calculation_pages_preserve_full_day_and_exact_details(daily_history):
    """Four pages expose every identity once while keyboard details and the full-day chart remain intact."""
    page, evidence, detail_reads, daily_reads = daily_history
    rows = page.locator(".performance-row")
    expect(rows).to_have_count(25)
    expect(page.locator("#performance-page")).to_have_text("Page 1 of 4")
    previous = page.get_by_role("button", name="Previous", exact=True)
    following = page.get_by_role("button", name="Next", exact=True)
    expect(previous).to_be_disabled()
    page.evaluate("document.fonts.ready")
    assert rows.first.bounding_box()["height"] <= (56 if page.viewport_size["width"] < 500 else 48)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    captures = Path("results/ci/calculation-history-list")
    captures.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    page.screenshot(path=str(captures / f"full-day-{width}.png"), full_page=False)
    seen = rows.evaluate_all("items => items.map(item => item.dataset.calculationId)")
    following.click()
    expect(page.locator("#performance-page")).to_have_text("Page 2 of 4")
    action = rows.first.get_by_role("button")
    action.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    assert detail_reads == ["synthetic-calculation-025"]
    page.keyboard.press("Escape")
    expect(action).to_be_focused()
    expect(page.locator("#performance-page")).to_have_text("Page 2 of 4")
    page.get_by_label("Show additional calculations between quarter hours").check()
    expect(page.locator("#performance-page")).to_have_text("Page 2 of 4")
    for page_number, expected_count in ((2, 25), (3, 25), (4, 21)):
        expect(page.locator("#performance-page")).to_have_text(f"Page {page_number} of 4")
        expect(rows).to_have_count(expected_count)
        seen.extend(rows.evaluate_all("items => items.map(item => item.dataset.calculationId)"))
        assert (
            page.evaluate(
                "Chart.getChart(document.getElementById('performance-chart')).data.datasets[0].data.length"
            )
            == 96
        )
        if page_number != 4:
            following.click()
    expect(following).to_be_disabled()
    expect(previous).to_be_focused()
    assert seen == [row["id"] for row in evidence["calculations"]]
    assert daily_reads == [evidence["day"]]
    previous.click()
    expect(page.locator("#performance-page")).to_have_text("Page 3 of 4")
    page.locator("#calculations-heading").evaluate(
        "heading => heading.scrollIntoView({block: 'start'})"
    )
    page.screenshot(path=str(captures / f"paged-list-{width}.png"), full_page=False)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#performance-page")).to_have_text("Page 1 of 4")
    following.click()
    previous_day = (datetime.fromisoformat(evidence["day"]) - timedelta(days=1)).date().isoformat()
    page.get_by_label("Day", exact=True).fill(previous_day)
    expect(rows).to_have_count(1)
    expect(page.locator("#performance-page")).to_have_text("Page 1 of 1")
    expect(previous).to_be_disabled()
    expect(following).to_be_disabled()
    page.get_by_role("button", name="Today", exact=True).click()
    expect(rows).to_have_count(25)
    expect(page.locator("#performance-page")).to_have_text("Page 1 of 4")


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_calculation_paging_waits_for_reads_and_recovers_from_failure(daily_history):
    """Pending navigation locks and stale-response guards prevent another day's rows from leaking into a page."""
    page, evidence, _, _ = daily_history
    pending = []
    page.route("**/v1/planning-performance?day=*", lambda route: pending.append(route))
    following = page.get_by_role("button", name="Next", exact=True)
    previous = page.get_by_role("button", name="Previous", exact=True)
    following.click()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(following).to_be_disabled()
    expect(previous).to_be_disabled()
    expect(page.locator("#performance-status")).to_contain_text("Loading")
    assert len(pending) == 1
    pending.pop().fulfill(status=503, body="private provider response")
    expect(page.locator("#performance-status")).to_contain_text("could not be loaded")
    expect(page.locator(".performance-row")).to_have_count(0)
    expect(page.locator("#performance-page")).to_have_text("Page 1 of 1")
    expect(page.locator("body")).not_to_contain_text("private provider response")
    page.get_by_role("button", name="Refresh", exact=True).click()
    assert len(pending) == 1
    pending.pop().fulfill(json=evidence)
    expect(page.locator(".performance-row")).to_have_count(25)
    expect(following).to_be_enabled()
    page.get_by_label("Day", exact=True).fill("2020-01-01")
    page.get_by_role("button", name="Today", exact=True).click()
    assert len(pending) == 2
    old_read, current_read = pending
    current_read.fulfill(json=evidence)
    expect(page.locator("#performance-summary")).to_contain_text("96 completed calculations")
    old_read.fulfill(json={**evidence, "day": "2020-01-01", "calculations": []})
    expect(page.get_by_label("Day", exact=True)).to_have_value(evidence["day"])
    expect(page.locator(".performance-row")).to_have_count(25)
    page.route(
        "**/v1/planning-performance?day=2020-01-02",
        lambda route: route.fulfill(json={**evidence, "day": "2020-01-02", "calculations": []}),
    )
    page.get_by_label("Day", exact=True).fill("2020-01-02")
    expect(page.locator(".performance-row")).to_have_count(0)
    expect(page.locator("#performance-list")).to_contain_text("No completed calculations")
