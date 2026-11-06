"""Show original scenario costs and benefits against one no-battery baseline."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import SAVINGS_BROWSER_TIME_ZONE, TOKEN


@pytest.fixture
def financial_page(app_page, request):
    """Use Dutch formatting by default and an explicit alternative locale when requested."""
    original, url = app_page
    with original.context.browser.new_context(
        viewport=original.viewport_size,
        has_touch=original.viewport_size["width"] < 500,
        timezone_id=SAVINGS_BROWSER_TIME_ZONE,
        locale=getattr(request, "param", "nl-NL"),
    ) as context:
        yield context.new_page(), url


def comparison_response(route, baseline_benefit_eur=4.96, *, without_battery=True):
    """Use a 14.21 EUR baseline and 12 EUR self-consumption cost.

    Optimizer cost is baseline cost minus the specified benefit. The separate
    smart-planning comparison is deliberately different from baseline benefit.
    """
    body = route.fetch().json()
    optimizer_cost_eur = 14.21 - baseline_benefit_eur if baseline_benefit_eur is not None else 9.25

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
        expected_savings=baseline_benefit_eur,
    )
    reference.update(
        reference_costs=costs(12),
        ems_costs=costs(optimizer_cost_eur),
        expected_savings=12 - optimizer_cost_eur,
    )
    body["scenarios"]["self_consumption"]["expected_savings_vs_without_battery"] = 2.21
    if not without_battery:
        body["scenarios"].pop("without_battery")
        body["scenarios"]["self_consumption"].pop("expected_savings_vs_without_battery", None)
    route.fulfill(json=body)


def connect(page, url):
    """Open the actual authenticated planning view with the isolated test credential."""
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize(
    "baseline_benefit_eur,state,displayed_benefit",
    [
        (4.96, "positive", "4,96 EUR"),
        (0, "zero", "0,00 EUR"),
        (-1.25, "negative", "-1,25 EUR"),
        (None, "unavailable", "- EUR"),
        (0.001, "zero", "0,00 EUR"),
        (-0.001, "zero", "0,00 EUR"),
    ],
)
def test_scenario_headers_lead_with_cost_then_baseline_benefit(
    financial_page, baseline_benefit_eur, state, displayed_benefit
):
    """Both battery scenarios compare with baseline, preserving losses, zero and unknown."""
    page, url = financial_page
    page.route(
        "**/v1/plans/*/comparison",
        lambda route: comparison_response(route, baseline_benefit_eur),
    )
    connect(page, url)
    expect(page.locator(".scenario-card > figcaption > h3")).to_have_text(
        ["Optimizer", "Battery self-consumption", "Without battery: Baseline"]
    )
    for cost_id in ("ems-costs", "self-consumption-costs"):
        expect(page.locator(f"#{cost_id} > dl dt")).to_have_text(
            ["Expected economic cost", "Expected benefit versus without battery"]
        )
    expect(page.locator("#ems-costs > dl dd").nth(1)).to_have_text(displayed_benefit)
    expect(page.locator("#ems-costs [data-benefit-state]")).to_have_attribute(
        "data-benefit-state", state
    )
    expect(page.locator("#self-consumption-costs > dl dd")).to_have_text(["12,00 EUR", "2,21 EUR"])
    expect(page.locator("#reference-costs > dl dt")).to_have_text("Expected economic cost")
    expect(page.locator("#reference-costs > dl dd")).to_have_text("14,21 EUR")
    expect(
        page.get_by_role("heading", name="Optimizer: Expected result", exact=True)
    ).to_have_count(0)
    expect(
        page.get_by_text("Extra expected savings from smart planning", exact=True)
    ).to_have_count(0)
    periods = page.locator(".scenario-period").all_text_contents()
    assert len(periods) == 3 and len(set(periods)) == 1
    assert "Whole original plan" in periods[0]
    before = page.locator("#ems-costs > dl").text_content()
    for title in ("Next 6 hours", "Next 24 hours", "Full horizon"):
        page.get_by_role("button", name=title, exact=True).click()
        expect(page.locator("#ems-costs > dl")).to_have_text(before)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    positions = page.locator("#ems-costs > dl > div").evaluate_all(
        "fields => fields.map(field => { const r = field.getBoundingClientRect(); return {x:r.x, y:r.y}; })"
    )
    assert positions[0]["y"] <= positions[1]["y"]
    if positions[0]["y"] == positions[1]["y"]:
        assert positions[0]["x"] < positions[1]["x"]
    if baseline_benefit_eur == 4.96:
        expect(page.locator("#ems-costs > dl dd")).to_have_text(["9,25 EUR", "4,96 EUR"])
        for index, caption in enumerate(page.locator(".scenario-card > figcaption").all()):
            caption.screenshot(
                path=f"results/ci/planning-baseline-{page.viewport_size['width']}-{index}.png"
            )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_missing_baseline_does_not_substitute_the_self_consumption_comparison(financial_page):
    """A valid optimizer cost survives missing baseline, but neither benefit is invented."""
    page, url = financial_page
    page.route(
        "**/v1/plans/*/comparison", lambda route: comparison_response(route, without_battery=False)
    )
    connect(page, url)
    expect(page.locator("#ems-costs > dl dd")).to_have_text(["9,25 EUR", "- EUR"])
    expect(page.locator("#self-consumption-costs > dl dd")).to_have_text(["12,00 EUR", "- EUR"])
    expect(page.locator("#reference-costs")).to_contain_text("Expected costs unavailable")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_missing_self_consumption_keeps_optimizer_baseline_benefit(financial_page):
    """The optimizer baseline benefit remains 4.96 EUR without a self-consumption result."""
    page, url = financial_page

    def without_self_consumption(route):
        """Remove only the independently failed reference from the real API response."""
        body = route.fetch().json()
        baseline = body["scenarios"]["without_battery"]["comparison"]
        baseline["expected_savings"] = 4.96
        baseline["ems_costs"]["economic_cost"] = 9.25
        body["scenarios"].pop("self_consumption")
        route.fulfill(json=body)

    page.route("**/v1/plans/*/comparison", lambda route: comparison_response(route))
    connect(page, url)
    page.unroute("**/v1/plans/*/comparison")
    page.route("**/v1/plans/*/comparison", without_self_consumption)
    with page.expect_response("**/v1/plans/*/comparison"):
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#self-consumption-costs > dl dd")).to_have_text(["- EUR", "- EUR"])
    expect(page.locator("#ems-costs > dl dt")).to_have_text(
        ["Expected economic cost", "Expected benefit versus without battery"]
    )
    expect(page.locator("#ems-costs > dl dd")).to_have_text(["9,25 EUR", "4,96 EUR"])


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

    expect(page.locator("#ems-costs details dt")).to_have_text(
        [
            "Import cost",
            "Export income",
            "Bonus income",
            "Net grid cash cost",
            "Wear",
            "Battery value change (not cash)",
        ]
    )
    expect(page.locator("#ems-costs details dd")).to_have_text(
        ["9,25 EUR", "0,00 EUR", "0,00 EUR", "9,25 EUR", "0,00 EUR", "0,00 EUR"]
    )
    expect(page.locator("#ems-costs details")).to_contain_text("Battery value change (not cash)")
    expect(page.locator("#ems-costs details p")).to_have_text(
        [
            "Net grid cash cost = import cost - export income - bonus income. Amounts are rounded for display. Totals use full precision.",
            (
                "Economic cost = net grid cash cost + wear - battery value change. "
                "A positive value change reduces economic cost. "
                "Battery value change is not cash."
            ),
        ]
    )
    with page.expect_response("**/v1/plans/*/comparison"):
        page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#ems-costs > dl dd")).to_have_text(["9,25 EUR", "4,96 EUR"])
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#ems-costs details")).to_have_attribute("open", "")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("financial_page", ["nl-NL", "en-US"], indirect=True)
@pytest.mark.parametrize("direction", [1, -1])
def test_cost_breakdown_shows_each_scenario_income_with_original_sign(financial_page, direction):
    """Distinct scenario inputs expose income deductions and negative income without changing saved plans."""
    page, url = financial_page
    headers = {"Authorization": "Bearer " + TOKEN}
    original = page.request.get(url + "/v1/plans/browser-original-plan", headers=headers).json()
    economics = [12.90, 15.20, 15.00] if direction == 1 else [18.90, 20.20, 25.00]
    expected = (
        [
            ["16,00 EUR", "2,75 EUR", "0,25 EUR", "13,00 EUR", "0,20 EUR", "0,30 EUR"],
            ["18,00 EUR", "2,25 EUR", "0,25 EUR", "15,50 EUR", "0,10 EUR", "0,40 EUR"],
            ["20,00 EUR", "4,50 EUR", "0,50 EUR", "15,00 EUR", "0,00 EUR", "0,00 EUR"],
        ]
        if direction == 1
        else [
            ["16,00 EUR", "-2,75 EUR", "-0,25 EUR", "19,00 EUR", "0,20 EUR", "0,30 EUR"],
            ["18,00 EUR", "-2,25 EUR", "-0,25 EUR", "20,50 EUR", "0,10 EUR", "0,40 EUR"],
            ["20,00 EUR", "-4,50 EUR", "-0,50 EUR", "25,00 EUR", "0,00 EUR", "0,00 EUR"],
        ]
    )

    def respond(route):
        """Supply independent, internally consistent financial inputs through the real presentation route."""
        body = route.fetch().json()
        costs = [
            {
                "purchase": purchase,
                "sale": sale * direction,
                "bonus": bonus * direction,
                "wear": wear,
                "inventory_change": inventory,
                "economic_cost": economic,
            }
            for (purchase, sale, bonus, wear, inventory), economic in zip(
                [
                    (16, 2.75, 0.25, 0.20, 0.30),
                    (18, 2.25, 0.25, 0.10, 0.40),
                    (20, 4.50, 0.50, 0, 0),
                ],
                economics,
                strict=True,
            )
        ]
        baseline = body["scenarios"]["without_battery"]["comparison"]
        baseline.update(ems_costs=costs[0], reference_costs=costs[2])
        body["scenarios"]["self_consumption"]["comparison"]["reference_costs"] = costs[1]
        route.fulfill(json=body)

    page.route("**/v1/plans/*/comparison", respond)
    connect(page, url)
    english = page.evaluate("navigator.language") == "en-US"
    for cost_id, amounts in zip(
        ("ems-costs", "self-consumption-costs", "reference-costs"), expected, strict=True
    ):
        host = page.locator("#" + cost_id)
        host.locator("summary").click()
        expect(host.locator("details dt")).to_have_text(
            [
                "Import cost",
                "Export income",
                "Bonus income",
                "Net grid cash cost",
                "Wear",
                "Battery value change (not cash)",
            ]
        )
        expect(host.locator("details dd")).to_have_text(
            [amount.replace(",", ".") for amount in amounts] if english else amounts
        )
    expect(page.locator("#plan-version")).to_have_value("browser-original-plan")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    assert (
        page.request.get(url + "/v1/plans/browser-original-plan", headers=headers).json()
        == original
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize(
    "costs,amounts,economic",
    [
        (
            {
                "purchase": 20,
                "sale": 0,
                "bonus": None,
                "wear": 0.20,
                "inventory_change": 0.30,
                "economic_cost": None,
            },
            ["20,00 EUR", "0,00 EUR", "- EUR", "- EUR", "0,20 EUR", "0,30 EUR"],
            "- EUR",
        ),
        (
            {
                "purchase": 11.375,
                "sale": 3.064,
                "bonus": 0,
                "wear": 0,
                "inventory_change": 0,
                "economic_cost": 8.311,
            },
            ["11,38 EUR", "3,06 EUR", "0,00 EUR", "8,31 EUR", "0,00 EUR", "0,00 EUR"],
            "8,31 EUR",
        ),
    ],
)
def test_cost_breakdown_preserves_unknown_values_and_full_precision(
    financial_page, costs, amounts, economic
):
    """Missing amounts stay unavailable and 11.375 minus 3.064 retains its 8.311 total before display rounding."""
    page, url = financial_page

    def respond(route):
        """Project original financial inputs without changing the saved plan or their precision."""
        body = route.fetch().json()
        baseline = body["scenarios"]["without_battery"]["comparison"]
        baseline.update(ems_costs=costs, reference_costs=costs)
        body["scenarios"]["self_consumption"]["comparison"]["reference_costs"] = costs
        route.fulfill(json=body)

    page.route("**/v1/plans/*/comparison", respond)
    connect(page, url)
    for cost_id in ("ems-costs", "self-consumption-costs", "reference-costs"):
        host = page.locator("#" + cost_id)
        host.locator("summary").click()
        expect(host.locator("details dd")).to_have_text(amounts)
        expect(host.locator(":scope > dl dd").first).to_have_text(economic)
        expect(host.locator("details")).to_contain_text("Totals use full precision")


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_cost_breakdown_on_real_two_day_plan_remains_readable(app_page):
    """Capture each actual solved scenario over the full 48-hour horizon with no horizontal overflow."""
    from tests.frontend.browser.documentation_fixture import START

    page, url = app_page
    page.clock.install(time=START)
    connect(page, url)
    page.get_by_role("button", name="Full horizon", exact=True).click()
    # A tall mobile capture retains the complete two-day card below the sticky menu.
    if page.viewport_size["width"] < 500:
        page.set_viewport_size({"width": page.viewport_size["width"], "height": 1600})
    directory = Path("results/ci/cost-breakdown")
    directory.mkdir(parents=True, exist_ok=True)
    for cost_id in ("ems-costs", "self-consumption-costs", "reference-costs"):
        host = page.locator("#" + cost_id)
        host.locator("summary").click()
        expect(host.get_by_text("Import cost", exact=True)).to_be_visible()
        expect(host.get_by_text("Export income", exact=True)).to_be_visible()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.locator(f"figure:has(#{cost_id})").screenshot(
            path=str(directory / f"{cost_id}-{page.viewport_size['width']}.png")
        )
