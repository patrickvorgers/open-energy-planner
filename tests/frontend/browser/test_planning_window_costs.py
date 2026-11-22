"""Responsive period costs from the real financial module, with stale-response and coverage guards."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.helpers import TOKEN, wait_for_product_reads

HEADERS = {"Authorization": "Bearer " + TOKEN}


def open_plan(page, url, *, now=START, linked=False):
    """Open the isolated 48-hour solved plan at an explicit live or historical clock."""
    page.clock.set_fixed_time(now)
    identity = page.request.get(url + "/v1/plans?limit=1", headers=HEADERS).json()["plans"][0][
        "plan_id"
    ]
    page.goto(url + "/overview" + (f"?plan={identity}" if linked else ""))
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    return identity


def period_response(page, url, identity, start, end):
    """Read server-owned amounts independently of the UI, never reconstructing its accounting in JavaScript."""
    response = page.request.get(
        url + f"/v1/plans/{identity}/comparison",
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers=HEADERS,
    )
    assert response.status == 200
    return response.json()


def assert_amounts(page, result):
    """Compare all rendered amounts to exact server results rounded only at the display boundary."""
    for name, host in (
        ("optimizer", "ems-costs"),
        ("self_consumption", "self-consumption-costs"),
        ("without_battery", "reference-costs"),
    ):
        entry = result["scenarios"][name]
        assert "issue" not in entry, entry
        costs = entry["costs"]
        expected = [f"{costs['economic_cost']:,.2f} EUR"]
        if name != "without_battery":
            expected.append(f"{entry['benefit_vs_without_battery']:,.2f} EUR")
        expect(page.locator(f"#{host} > dl dd")).to_have_text(expected)
        expect(page.locator(f"#{host} details dd")).to_have_text(
            [
                f"{costs[key]:,.2f} EUR"
                for key in (
                    "purchase",
                    "sale",
                    "bonus",
                    "electricity_cost",
                    "wear",
                    "inventory_change",
                )
            ]
        )


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_six_day_and_full_period_use_financial_module_and_preserve_original(app_page):
    """Real 6/24/48-hour summaries and breakdowns match server amounts while original advice remains byte-equivalent."""
    page, url = app_page
    identity = open_plan(page, url)
    original = page.request.get(url + f"/v1/plans/{identity}", headers=HEADERS).json()
    expect(page.locator('[data-hours="0"]')).to_have_text("Full period")
    expect(page.get_by_role("heading", name="Original plan", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"hour planning window"))).to_have_count(0)
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.get_by_text(re.compile(r"hour planning window"))).to_have_count(0)
    directory = Path("results/ci/planning-heading")
    directory.mkdir(parents=True, exist_ok=True)
    viewport = page.viewport_size
    if viewport["width"] < 500:
        page.set_viewport_size({**viewport, "height": 1800})
    page.locator("#plan-heading").evaluate(
        "element => window.scrollTo(0, element.getBoundingClientRect().top + scrollY - 100)"
    )
    page.screenshot(path=str(directory / f"planning-{viewport['width']}.png"))
    page.set_viewport_size(viewport)
    totals = []
    for hours, end_label in (
        (6, "11/4/2026, 6:00 AM"),
        (24, "11/5/2026, 12:00 AM"),
        (48, "11/6/2026, 12:00 AM"),
    ):
        expected = period_response(page, url, identity, START, START + timedelta(hours=hours))
        page.locator(f'[data-hours="{0 if hours == 48 else hours}"]').click()
        assert_amounts(page, expected)
        totals.append(expected["scenarios"]["optimizer"]["costs"]["economic_cost"])
        for caption in page.locator(".scenario-period").all():
            expect(caption).to_have_text(f"From 11/4/2026, 12:00 AM to {end_label}.")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for host in ("ems-costs", "self-consumption-costs", "reference-costs"):
            page.locator(f"#{host} details").evaluate("element => element.open = true")
        if hours == 6:
            directory = Path("results/ci/window-costs")
            directory.mkdir(parents=True, exist_ok=True)
            viewport = page.viewport_size
            if viewport["width"] < 500:
                page.set_viewport_size({**viewport, "height": 1600})
            page.locator(".optimizer-scenario > figcaption").screenshot(
                path=str(directory / f"six-hours-{page.viewport_size['width']}.png")
            )
            page.set_viewport_size(viewport)
    assert len(set(totals)) == 3
    assert page.request.get(url + f"/v1/plans/{identity}", headers=HEADERS).json() == original


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_late_six_hour_response_cannot_replace_newer_day_selection(app_page):
    """A delayed six-hour response carrying a sentinel cost cannot overwrite the already selected 24-hour result."""
    page, url = app_page
    identity = open_plan(page, url)
    pending = []

    def defer_six_hours(route):
        """Hold only the first window response while the real second request proceeds."""
        if not pending:
            pending.append(route)
        else:
            route.continue_()

    page.route(re.compile(r".*/v1/plans/[^/]+/comparison\?.*"), defer_six_hours)
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    expect(page.locator(".scenario-period").first).to_contain_text("Loading expected costs")
    expect(page.locator("#ems-costs > dl dd")).to_have_text(["- EUR", "- EUR"])
    page.get_by_role("button", name="Next 24 hours", exact=True).click()
    expected = period_response(page, url, identity, START, START + timedelta(hours=24))
    assert_amounts(page, expected)
    assert len(pending) == 1
    pending[0].fulfill(
        json={"period": None, "scenarios": {"optimizer": {"costs": {"economic_cost": 999}}}}
    )
    wait_for_product_reads(page)
    assert_amounts(page, expected)
    expect(page.get_by_role("button", name="Next 24 hours", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("elapsed_minutes", [17, 60])
def test_live_remaining_period_and_linked_original_period_are_distinct(
    app_page, linked, elapsed_minutes
):
    """Live fractional-hour and historical costs retain their actual periods behind one stable range label."""
    page, url = app_page
    now = START + timedelta(minutes=elapsed_minutes)
    remaining_start = START + timedelta(minutes=elapsed_minutes // 15 * 15)
    identity = open_plan(page, url, now=now, linked=linked)
    expected = period_response(
        page, url, identity, START if linked else remaining_start, START + timedelta(hours=48)
    )
    assert_amounts(page, expected)
    expect(page.locator('[data-hours="0"]')).to_have_text("Full period")


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_failed_slice_and_expired_plan_never_show_previous_totals(app_page):
    """A failed period read clears stale totals, full-period recovery works and an expired live plan has no invented zero cost."""
    page, url = app_page
    identity = open_plan(page, url)
    page.route(re.compile(r".*/v1/plans/[^/]+/comparison\?.*"), lambda route: route.abort())
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    expect(page.locator(".scenario-period").first).to_contain_text("Use Refresh overview to retry")
    expect(page.locator("#ems-costs > dl dd")).to_have_text(["- EUR", "- EUR"])
    page.locator('[data-hours="0"]').click()
    assert_amounts(page, period_response(page, url, identity, START, START + timedelta(hours=48)))
    page.unroute(re.compile(r".*/v1/plans/[^/]+/comparison\?.*"))
    page.clock.set_fixed_time(START + timedelta(hours=49))
    page.locator('[data-hours="0"]').click()
    expect(page.locator(".scenario-period").first).to_contain_text("No planned intervals")
    expect(page.locator("#ems-costs > dl dd")).to_have_text(["- EUR", "- EUR"])
    expect(page.locator('[data-hours="0"]')).to_have_text("Full period")
