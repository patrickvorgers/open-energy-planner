"""Make the original incremental planning benefit visible without changing financial contracts."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import SAVINGS_BROWSER_TIME_ZONE, TOKEN


@pytest.fixture
def financial_page(app_page):
    """Use explicit Dutch number formatting on both CI and Windows for the same actual app."""
    original, url = app_page
    with original.context.browser.new_context(
        viewport=original.viewport_size,
        has_touch=original.viewport_size["width"] < 500,
        timezone_id=SAVINGS_BROWSER_TIME_ZONE,
        locale="nl-NL",
    ) as context:
        yield context.new_page(), url


def comparison_response(route, extra=2.75, *, without_battery=True):
    """Supply independent costs: 14.21 without storage, 12.00 self-consumption, 9.25 optimized."""
    body = route.fetch().json()
    ems_cost = 12 - extra if extra is not None else 9.25

    def costs(amount):
        """Keep cash and economic cost equal with explicitly zero wear and inventory correction."""
        return {
            "purchase": amount,
            "sale": 0,
            "bonus": 0,
            "wear": 0,
            "inventory_change": 0,
            "economic_cost": amount,
        }

    baseline = body["scenarios"]["without_battery"]["comparison"]
    reference = body["scenarios"]["self_consumption"]["comparison"]
    baseline.update(
        reference_costs=costs(14.21), ems_costs=costs(ems_cost), expected_savings=14.21 - ems_cost
    )
    reference.update(reference_costs=costs(12), ems_costs=costs(ems_cost), expected_savings=extra)
    body["scenarios"]["self_consumption"]["expected_savings_vs_without_battery"] = 2.21
    if extra is None:
        reference["reference_costs"] = None
    if not without_battery:
        body["scenarios"].pop("without_battery")
    route.fulfill(json=body)


def connect(page, url):
    """Open the actual authenticated planning view with the isolated test credential."""
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize(
    "extra,state,amount,label",
    [
        (2.75, "positive", "2,75", "Extra expected savings from smart planning"),
        (0, "zero", "0,00", "No extra expected savings from smart planning"),
        (-1.25, "negative", "1,25", "Extra expected cost from smart planning"),
        (None, "unavailable", "-", "Extra expected savings from smart planning"),
        (0.001, "zero", "0,00", "No extra expected savings from smart planning"),
    ],
)
def test_incremental_benefit_is_prominent_and_keeps_loss_zero_and_unknown_distinct(
    financial_page, extra, state, amount, label
):
    """Use the self-consumption comparison directly and never paint zero or missing benefit green."""
    page, url = financial_page
    page.route("**/v1/plans/*/comparison", lambda route: comparison_response(route, extra))
    connect(page, url)
    expect(page.locator("#smart-benefit-value")).to_have_text(amount)
    expect(page.locator("#smart-benefit-label")).to_have_text(label)
    expect(page.locator(".planning-benefit")).to_have_attribute("data-state", state)
    expect(page.locator(".planning-benefit")).to_contain_text(
        "Compared with battery self-consumption"
    )
    expect(page.locator("#smart-benefit-period")).to_contain_text("Whole original plan")
    if extra == 2.75:
        expect(page.locator("#reference-costs > dl dd")).to_have_text("14,21 EUR")
        expect(page.locator("#self-consumption-costs > dl dd")).to_have_text(
            ["12,00 EUR", "2,21 EUR"]
        )
        expect(page.locator("#ems-costs > dl dd")).to_have_text(["9,25 EUR", "4,96 EUR"])
    assert (
        page.locator("#smart-benefit-value").evaluate(
            "el => parseFloat(getComputedStyle(el).fontSize)"
        )
        >= 32
    )
    assert (
        page.locator(".planning-benefit").bounding_box()["y"]
        < page.locator("#reference-chart").bounding_box()["y"]
    )
    before = page.locator(".planning-benefit").text_content()
    for title in ("Next 6 hours", "Next 24 hours", "Full horizon"):
        page.get_by_role("button", name=title, exact=True).click()
        expect(page.locator(".planning-benefit")).to_have_text(before)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_smart_benefit_does_not_require_the_without_battery_reference(financial_page):
    """A valid 2.75 self-consumption comparison remains usable when the no-battery reference is absent."""
    page, url = financial_page
    page.route(
        "**/v1/plans/*/comparison", lambda route: comparison_response(route, without_battery=False)
    )
    connect(page, url)
    expect(page.locator("#smart-benefit-value")).to_have_text("2,75")
    expect(page.locator("#reference-costs")).to_contain_text("Expected costs unavailable")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_cost_breakdown_is_keyboard_accessible_and_survives_refresh(financial_page):
    """Keep original cash, wear and non-cash inventory available without the dense text strip."""
    page, url = financial_page
    page.route("**/v1/plans/*/comparison", lambda route: comparison_response(route))
    connect(page, url)
    disclosure = page.locator("#ems-costs summary")
    disclosure.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#ems-costs details")).to_have_attribute("open", "")
    expect(page.locator("#ems-costs details dd")).to_have_text(["9,25 EUR", "0,00 EUR", "0,00 EUR"])
    expect(page.locator("#ems-costs details")).to_contain_text("Battery value change (not cash)")
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#ems-costs details")).to_have_attribute("open", "")
