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


def comparison_response(route, incremental_benefit_eur=2.75, *, without_battery=True):
    """Supply independent scenario costs and a signed incremental benefit in EUR.

    Baseline and self-consumption costs are 14.21 and 12.00 EUR. Numeric
    benefits set optimizer cost to 12 EUR minus that benefit. A missing benefit
    keeps optimizer cost at 9.25 EUR and makes self-consumption costs and its
    benefit versus the baseline unavailable.
    """
    body = route.fetch().json()
    optimizer_cost_eur = (
        12 - incremental_benefit_eur if incremental_benefit_eur is not None else 9.25
    )

    def costs(cost_eur):
        """Keep cash and economic cost equal with explicitly zero wear and inventory correction."""
        return {
            "purchase": cost_eur,
            "sale": 0,
            "bonus": 0,
            "wear": 0,
            "inventory_change": 0,
            "economic_cost": cost_eur,
        }

    baseline = body["scenarios"]["without_battery"]["comparison"]
    reference = body["scenarios"]["self_consumption"]["comparison"]
    baseline.update(
        reference_costs=costs(14.21),
        ems_costs=costs(optimizer_cost_eur),
        expected_savings=14.21 - optimizer_cost_eur,
    )
    reference.update(
        reference_costs=costs(12),
        ems_costs=costs(optimizer_cost_eur),
        expected_savings=incremental_benefit_eur,
    )
    body["scenarios"]["self_consumption"]["expected_savings_vs_without_battery"] = 2.21
    if incremental_benefit_eur is None:
        reference["reference_costs"] = None
        body["scenarios"]["self_consumption"].pop("expected_savings_vs_without_battery", None)
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
    "incremental_benefit_eur,state,displayed_incremental_benefit_eur,label",
    [
        (2.75, "positive", "2,75", "Extra expected savings from smart planning"),
        (0, "zero", "0,00", "No extra expected savings from smart planning"),
        (-1.25, "negative", "1,25", "Extra expected cost from smart planning"),
        (None, "unavailable", "-", "Extra expected savings from smart planning"),
        (0.001, "zero", "0,00", "No extra expected savings from smart planning"),
    ],
)
def test_incremental_benefit_is_prominent_and_keeps_loss_zero_and_unknown_distinct(
    financial_page, incremental_benefit_eur, state, displayed_incremental_benefit_eur, label
):
    """Use the self-consumption comparison directly and never paint zero or missing benefit green."""
    page, url = financial_page
    page.route(
        "**/v1/plans/*/comparison",
        lambda route: comparison_response(route, incremental_benefit_eur),
    )
    connect(page, url)
    expect(page.locator("#smart-benefit-value")).to_have_text(displayed_incremental_benefit_eur)
    expect(page.locator("#smart-benefit-label")).to_have_text(label)
    expect(page.locator("#optimizer-total-value")).to_have_text(displayed_incremental_benefit_eur)
    expect(page.locator("#optimizer-total-label")).to_have_text(label)
    expect(page.locator(".optimizer-scenario .planning-benefit")).to_have_attribute(
        "data-state", state
    )
    expect(page.locator(".optimizer-scenario .planning-benefit")).to_contain_text(
        "Compared with battery self-consumption"
    )
    expect(page.locator("#smart-benefit-period")).to_contain_text("Whole original plan")
    expect(page.locator(".scenario-card > figcaption > h3")).to_have_text(
        ["Optimizer", "Battery self-consumption", "Without battery: Baseline"]
    )
    expect(page.locator("#plan-chart").locator("xpath=ancestor::figure")).to_contain_text(
        "Compared with battery self-consumption"
    )
    expect(page.locator("#self-consumption-heading").locator("xpath=..")).to_contain_text(
        "Battery and converter limits can still leave grid import or export."
    )
    periods = page.locator(".scenario-period").all_text_contents()
    assert len(periods) == 4 and len(set(periods)) == 1
    if incremental_benefit_eur is None:
        expect(page.locator("#self-consumption-costs > dl dd")).to_have_text(["- EUR", "- EUR"])
    if incremental_benefit_eur == 2.75:
        expect(page.locator("#reference-costs > dl dd")).to_have_text("14,21 EUR")
        expect(page.locator("#self-consumption-costs > dl dd")).to_have_text(
            ["12,00 EUR", "2,21 EUR"]
        )
        for cost_id in ("ems-costs", "optimizer-total-costs"):
            expect(page.locator(f"#{cost_id} > dl dd")).to_have_text(["9,25 EUR", "4,96 EUR"])
    assert (
        page.locator("#smart-benefit-value").evaluate(
            "el => parseFloat(getComputedStyle(el).fontSize)"
        )
        >= 32
    )
    assert (
        page.locator(".optimizer-scenario .planning-benefit").bounding_box()["y"]
        < page.locator("#reference-chart").bounding_box()["y"]
    )
    summary_top = page.locator(".optimizer-total").bounding_box()["y"]
    assert page.locator("#reference-chart").bounding_box()["y"] < summary_top
    assert (
        summary_top
        < page.locator("#current-price-chart").locator("xpath=ancestor::figure").bounding_box()["y"]
    )
    before = page.locator(".optimizer-scenario .planning-benefit").text_content()
    for title in ("Next 6 hours", "Next 24 hours", "Full horizon"):
        page.get_by_role("button", name=title, exact=True).click()
        expect(page.locator(".optimizer-scenario .planning-benefit")).to_have_text(before)
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
    chart_help = page.get_by_text("Read this chart", exact=True)
    chart_help.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".scenario-chart-help")).to_have_attribute("open", "")
    expect(page.locator(".scenario-chart-help")).to_contain_text(
        "All series together balance to zero"
    )
    disclosure = page.locator("#ems-costs summary")
    disclosure.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#ems-costs details")).to_have_attribute("open", "")
    expect(page.locator("#ems-costs details dd")).to_have_text(["9,25 EUR", "0,00 EUR", "0,00 EUR"])
    expect(page.locator("#ems-costs details")).to_contain_text("Battery value change (not cash)")
    expect(page.locator("#ems-costs details p")).to_have_text(
        "Economic cost = net grid cash cost + wear - battery value change. "
        "A positive value change reduces economic cost. "
        "Battery value change is not cash."
    )
    with page.expect_response("**/v1/plans/*/comparison"):
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#optimizer-total-costs > dl dd")).to_have_text(["9,25 EUR", "4,96 EUR"])
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#ems-costs details")).to_have_attribute("open", "")
