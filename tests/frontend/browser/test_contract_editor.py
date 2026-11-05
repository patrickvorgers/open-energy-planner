"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from open_energy_planner.contracts.calculation.formula import Formula
from tests.frontend.browser.helpers import (
    TOKEN,
    add_price_rule,
    assert_floating_save_bar,
    discover,
    edit_price_rule,
)


@pytest.mark.parametrize("app_site", ["savings_corrected"], indirect=True)
def test_overview_retains_original_expectations_beside_successful_contract_correction(app_page):
    """Desktop/mobile identify corrected expected costs separately from original advice and measured evaluation."""
    page, url = app_page
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#expected-benefit")).to_contain_text("Contract updated")
    expect(page.locator("#expected-benefit")).to_contain_text("without re-optimization")
    page.get_by_text("Original and repriced expected costs", exact=True).click()
    expect(page.locator("#expected-cost-rows tr")).to_have_count(6)
    expect(page.get_by_role("columnheader", name="Original EMS", exact=True)).to_be_visible()
    expect(page.get_by_role("columnheader", name="Repriced EMS", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_contract_editor_previews_saves_and_preserves_conflicting_edits(app_page):
    """Desktop/mobile and ingress edit real dated formulas, preview exact rates and retain unsaved edits on conflict."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    if "hassio_ingress" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_label("Contract name", exact=True)).to_have_count(0)
    expect(page.get_by_label("Contract starts", exact=True)).to_be_visible()
    assert_floating_save_bar(page)
    expect(page.get_by_label("Bidding area", exact=True)).to_have_count(0)
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-01-01")
    page.get_by_label("Price rule starts", exact=True).fill("2026-01-01")
    page.get_by_label("Import formula", exact=True).fill("(market + 0.10) * 1.21")
    page.get_by_label("Export formula", exact=True).fill("market - 0.02")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_disabled()
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    page.get_by_label("Extra export rate", exact=True).fill("0.10 * max(market, 0)")
    page.get_by_label("Annual export cap (kWh)", exact=True).fill("7500")
    page.get_by_label("Paid window", exact=True).select_option("daylight")
    page.get_by_role("button", name="Preview rates", exact=True).click()
    expect(page.locator("#contract-preview .price-preview:visible")).to_contain_text("0.24")
    expect(page.locator("#contract-preview .price-preview:visible")).to_contain_text("-0.12")
    expect(page.locator("#contract-preview .price-preview:visible")).to_contain_text("Extra bonus")
    expect(page.locator("#contract-preview .price-preview:visible")).to_contain_text("0.01")
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    expect(page.locator("#bonus-budgets")).to_contain_text("Budget unavailable")
    page.get_by_role("button", name="Edit price rule", exact=True).click()
    edit_price_rule(page)
    page.get_by_label("Import formula", exact=True).fill("market + 0.15")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Preview rates", exact=True).click()
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.route(
        "**/v1/contracts",
        lambda route: (
            route.fulfill(status=409, body="Contract changed. Reload before saving")
            if route.request.method == "PUT"
            else route.continue_()
        ),
    )
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Your edits are retained")
    expect(page.get_by_label("Import formula", exact=True)).to_have_value("market + 0.15")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    edit_price_rule(page)
    page.get_by_label("Import formula", exact=True).fill("__import__('os')")
    page.get_by_role("button", name="Preview rates", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Invalid or unsupported")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_disabled()


def test_contract_delayed_preview_cannot_validate_newer_edits(app_page):
    """A response for an older formula cannot enable saving or show stale rates after the user edits again."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_label("Bidding area", exact=True)).to_have_count(0)
    pending = []
    page.route("**/v1/contracts/preview", lambda route: pending.append(route))
    with page.expect_request("**/v1/contracts/preview"):
        page.get_by_role("button", name="Preview rates", exact=True).click()
    edit_price_rule(page)
    page.get_by_label("Import formula", exact=True).fill("market + 0.20")
    assert len(pending) == 1
    pending[0].fulfill(json={"preview": [[{"market": 0, "buy_price": 999, "sell_price": 0}]]})
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_disabled()
    expect(page.locator("#contract-preview")).to_be_empty()


def test_contract_calendar_controls_have_readable_choices_and_no_timezone_setting(app_page):
    """New price/bonus dates use HA-local midnight. Calendar/anniversary choices replace editable reset parts and timezone."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    start = page.get_by_label("Price rule starts", exact=True)
    expect(start).to_have_attribute("type", "date")
    start.fill("2026-05-12")
    edit_price_rule(page)
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    expect(page.get_by_label("Bonus starts", exact=True)).to_have_count(0)
    expect(page.get_by_label("Bonus rule ID", exact=True)).to_have_count(0)
    expect(page.get_by_label("Bonus timezone", exact=True)).to_have_count(0)
    expect(page.get_by_label("Annual reset month", exact=True)).to_have_count(0)
    expect(page.get_by_label("Annual reset", exact=True).locator("option")).to_have_text(
        ["Calendar year (1 January)", "Contract anniversary"]
    )
    expect(page.get_by_label("Paid window", exact=True).locator("option")).to_have_text(
        ["Always", "Clock", "Daylight", "Monthly schedule"]
    )


def test_contract_anniversary_is_derived_from_contract_start_not_bonus_start(app_page):
    """A May contract with an August complete price rule resets its bonus in May, independently of the replacement date."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_label("Bidding area", exact=True)).to_have_count(0)
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-05-12")
    page.get_by_label("Price rule starts", exact=True).fill("2026-08-20")
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    page.get_by_label("Annual export cap (kWh)", exact=True).fill("1000")
    page.get_by_label("Annual reset", exact=True).select_option("contract_anniversary")
    page.get_by_role("button", name="Preview rates", exact=True).click()
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    result = page.request.get(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    assert result["periods"][0]["contract_start"] == "2026-05-11T22:00:00+00:00"
    assert result["periods"][0]["effective_from"] == "2026-08-19T22:00:00+00:00"
    assert result["bonuses"][0]["effective_from"] == "2026-08-19T22:00:00+00:00"
    assert (result["bonuses"][0]["reset_month"], result["bonuses"][0]["reset_day"]) == (5, 12)


def test_optional_price_comparison_previews_saves_reloads_and_disables(app_page):
    """Desktop/mobile edit an explicit gate using native controls. Save/reload preserves it and disabling removes it."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_label("Bidding area", exact=True)).to_have_count(0)
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-01-01")
    page.get_by_label("Price rule starts", exact=True).fill("2026-01-01")
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    page.get_by_label("Extra export rate", exact=True).fill("0.03")
    page.get_by_label("Annual export cap (kWh)", exact=True).fill("1000")
    expect(page.locator("#contract-save-state")).to_contain_text("Unsaved changes")
    before_save = page.request.get(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    assert before_save["status"] == "not_configured"
    save_bounds = page.get_by_role("button", name="Save contract", exact=True).bounding_box()
    assert save_bounds and 0 <= save_bounds["y"] < page.viewport_size["height"]
    expression = page.get_by_label("Price expression", exact=True)
    expect(expression).to_be_hidden()
    toggle = page.get_by_role("checkbox", name="Enable price condition", exact=True)
    toggle.focus()
    page.keyboard.press("Space")
    expect(expression).to_have_value("market")
    condition = page.get_by_role("group", name="Price condition", exact=True)
    bounds = condition.bounding_box()
    labels = condition.locator("label").all()
    field_bounds = [label.bounding_box() for label in labels]
    assert bounds and len(field_bounds) == 3
    for field_box in field_bounds:
        assert field_box and field_box["x"] >= bounds["x"] + 12
        assert field_box["x"] + field_box["width"] <= bounds["x"] + bounds["width"] - 12
    previous_bottom = max(box["y"] + box["height"] for box in field_bounds[:2])
    assert 12 <= field_bounds[2]["y"] - previous_bottom <= 24
    preview_bounds = condition.get_by_role("status").bounding_box()
    assert preview_bounds
    assert preview_bounds["y"] + preview_bounds["height"] <= bounds["y"] + bounds["height"] - 12
    condition.screenshot(
        path=f"results/ci/price-condition-{page.viewport_size['width']}.png",
        style=".settings-toolbar { visibility:hidden; }",
    )
    expression.fill("market + 0.02")
    comparison = page.get_by_label("Comparison", exact=True)
    assert comparison.locator("option").all_text_contents() == [
        "Greater than",
        "Greater than or equal to",
        "Less than",
        "Less than or equal to",
        "Equal to",
        "Not equal to",
    ]
    comparison.select_option("less_than_or_equal")
    page.get_by_label("Threshold (€/kWh)", exact=True).fill("0.0200000000000000000000000001")
    page.get_by_role("button", name="Preview rates", exact=True).click()
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    page.reload()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_price_rule(page)
    expect(toggle).to_be_checked()
    expect(expression).to_have_value("market + 0.02")
    expect(comparison).to_have_value("less_than_or_equal")
    expect(page.get_by_label("Threshold (€/kWh)", exact=True)).to_have_value(
        "0.0200000000000000000000000001"
    )
    edit_price_rule(page)
    toggle.uncheck()
    expect(expression).to_be_hidden()
    page.get_by_role("button", name="Preview rates", exact=True).click()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    result = page.request.get(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    assert result["bonuses"][0]["price_condition"] is None
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_contract_editor_waits_for_home_assistant_timezone(app_page):
    """Before HA discovery, date-only contract editing stays unavailable instead of assuming UTC."""
    page, url = app_page
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("installation timezone is required")
    expect(page.get_by_label("Price rule starts", exact=True)).to_have_count(0)


def test_price_rules_replace_at_next_start_without_editable_end_dates(app_page):
    """Desktop/mobile save consecutive rules using starts only. The latest rule remains open-ended and removal reconnects coverage."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_label("Bidding area", exact=True)).to_have_count(0)
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-01-01")
    page.get_by_label("Price rule starts", exact=True).fill("2026-01-01")
    expect(page.get_by_label("Price rule ends", exact=True)).to_have_count(0)
    add_price_rule(page)
    page.get_by_label("Price rule starts", exact=True).nth(1).fill("2026-07-01")
    page.get_by_label("Import formula", exact=True).nth(1).fill("market + 0.20")
    page.get_by_role("button", name="Preview rates", exact=True).click()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    body = page.request.get(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    assert (
        body["periods"][0]["effective_until"]
        == body["periods"][1]["effective_from"]
        == "2026-06-30T22:00:00+00:00"
    )
    assert body["periods"][1]["effective_until"] is None
    page.locator("#price-rule-select").select_option("1")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#contract-more summary").click()
    page.get_by_role("button", name="Delete selected price rule…", exact=True).click()
    page.get_by_role("button", name="Preview rates", exact=True).click()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    body = page.request.get(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    assert len(body["periods"]) == 1 and body["periods"][0]["effective_until"] is None


def test_saved_rule_selector_shows_active_summary_and_edits_only_selected_rule(app_page):
    """Desktop/mobile summarize the active rule. Editing an older selected rule stays an unsaved draft."""
    page, url = app_page
    discover(page, url)
    now = datetime.now(UTC)
    starts = [now - timedelta(days=400), now - timedelta(days=1), now + timedelta(days=1)]
    body = {
        "contract_id": "rule-list",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "effective_from": start.isoformat(),
                "effective_until": starts[index + 1].isoformat() if index < 2 else None,
                "import_formula": "market",
                "export_formula": "market",
            }
            for index, start in enumerate(starts)
        ],
        "bonuses": [
            {
                "rule_id": identity,
                "effective_from": start.isoformat(),
                "rate_formula": "0.03",
                "cap_kwh": 1000,
                "timezone": "Europe/Amsterdam",
            }
            for identity, start in zip(["old-bonus", "current-bonus", "future-bonus"], starts)
        ],
    }
    headers = {"Authorization": "Bearer " + TOKEN}
    response = page.request.put(url + "/v1/contracts", headers=headers, data=body)
    assert response.status == 200, response.text()
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    rows = page.locator("#contract-periods > details")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_be_hidden()
    expect(rows.nth(1)).to_be_hidden()
    expect(rows.nth(2)).to_be_hidden()
    expect(page.locator("#contract-summary")).to_be_visible()
    expect(page.locator("#price-rule-select")).to_have_value("1")
    saved_context = page.locator("#rule-context").inner_text()
    dates = [start.astimezone(ZoneInfo("Europe/Amsterdam")).date().isoformat() for start in starts]
    for index, status in enumerate(("Superseded", "Active", "Scheduled")):
        expect(rows.nth(index).locator(":scope > summary")).to_have_text(
            f"Price rule: {dates[index]}: {status}"
        )
    expect(rows.nth(1).get_by_label("Import formula", exact=True)).to_be_disabled()
    page.locator("#price-rule-select").select_option("0")
    older = rows.nth(0)
    expect(older).to_be_hidden()
    edit_price_rule(page)
    expect(older).to_be_visible()
    expect(rows.nth(1)).to_be_hidden()
    older.get_by_label("Extra export rate", exact=True).fill("0.04")
    expect(older.locator(":scope > summary")).to_have_text(
        f"Price rule: {dates[0]}: Superseded: Unsaved changes"
    )
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert saved["bonuses"][0]["rate_formula"] == "0.03"
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("button", name="Edit price rule", exact=True)).to_be_visible()
    edit_price_rule(page)
    page.get_by_label("Price rule starts", exact=True).nth(1).fill("2030-01-01")
    expect(page.locator("#rule-context")).to_have_text(saved_context)
    assert page.request.get(url + "/v1/contracts", headers=headers).json() == saved


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("missing", [False, True])
def test_price_chart_shows_total_export_with_bonus_before_exhausted_cap(app_page, missing):
    """A frozen .08 base plus .03/.07 bonus plots .11/.15 despite zero quota. Absent bonus evidence stays null."""
    page, url = app_page

    def frozen_bonus(route):
        """Supply known compiled display evidence. No current contract lookup or invoice recalculation is involved."""
        body = route.fetch().json()
        snapshot = body["plan"]["input_snapshot"]
        for step in snapshot["problem"]["intervals"]:
            step["sell_price"] = 0.08
        for tariff in snapshot["tariff_evidence"]:
            tariff["sell_price"] = 0.08
        snapshot["problem"]["export_bonuses"] = [
            {
                "rule_id": "display-example",
                "rates": [0.03, None if missing else 0.07],
                "caps_kwh": [0, 0],
                "initial_kwh": 100,
            }
        ]
        route.fulfill(json=body)

    page.route("**/v1/plans/*/comparison", frozen_bonus)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#price-chart-legend")).to_contain_text("Export rate incl. bonus")
    values = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).data.datasets.at(-1).data.map(point => point.y)"
    )
    assert values[:2] == pytest.approx([0.11, 0.11])
    assert values[2:] == ([None, None] if missing else pytest.approx([0.15, 0.15]))
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    assert page.evaluate(
        "(() => {const x=Chart.getChart(document.getElementById('price-chart')).options.scales.x; return x.max-x.min===21600000;})()"
    )


@pytest.mark.parametrize(
    "country, choices",
    [
        ("DK", ["DK1", "DK2"]),
        ("SE", ["SE1", "SE2", "SE3", "SE4"]),
        ("NO", ["NO1", "NO2", "NO3", "NO4", "NO5"]),
    ],
)
def test_contract_market_area_choice_is_limited_to_ha_country(app_page, country, choices):
    """Desktop/mobile offer only the discovered country's zones, require a choice and reject a foreign zone at both API boundaries."""
    page, url = app_page
    discover(page, url, country=country)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    area = page.get_by_role("combobox", name="Bidding area", exact=True)
    expect(area).to_have_value("")
    assert area.locator("option").evaluate_all(
        "options => options.map(option => option.value)"
    ) == ["", *choices]
    expect(area.locator("..")).to_contain_text("grid operator or energy supplier")
    area.select_option(choices[0])
    edit_price_rule(page)
    page.get_by_label("Import formula", exact=True).fill("market + 0.10")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    foreign = {
        "contract_id": "foreign-area",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "effective_from": "2026-01-01",
                "effective_until": None,
                "import_formula": "market",
                "export_formula": "market",
            }
        ],
        "bonuses": [],
    }
    headers = {"Authorization": "Bearer " + TOKEN}
    for method, path in [("post", "/v1/contracts/preview"), ("put", "/v1/contracts")]:
        response = getattr(page.request, method)(url + path, headers=headers, data=foreign)
        assert response.status == 400
        assert "Home Assistant country" in response.text()


def test_contract_automatic_validation_is_local_to_rule_and_never_saves(app_page):
    """Typing a valid formula enables explicit save and shows .20 beside its rule. Invalid input revokes save without creating a revision."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_label("Bidding area", exact=True)).to_have_count(0)
    expect(page.locator("#contract-identity")).to_contain_text("Bidding area: NL")
    expect(page.locator("#budget-status")).to_contain_text(
        "No active saved bonus. Save a price rule"
    )
    edit_price_rule(page)
    page.get_by_label("Import formula", exact=True).fill("market + 0.20")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    expect(page.locator("#contract-preview .price-preview:visible")).to_contain_text("0.20")
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.get(url + "/v1/contracts", headers=headers).json()["version"] is None
    page.get_by_label("Import formula", exact=True).fill("market / 0")
    expect(page.locator("#contract-status")).to_contain_text("Changes remain unsaved")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_disabled()
    expect(page.locator(".rule-preview")).to_have_count(0)


def test_contract_saved_date_replaces_original_timestamp_preservation(app_page):
    """Saving a changed calendar start replaces the prior timestamp. Returning to the old date must use HA midnight rather than revive an old 01:00 time."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    body = {
        "contract_id": "date-baseline",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "effective_from": "2026-01-01T00:00:00+00:00",
                "effective_until": None,
                "import_formula": "market",
                "export_formula": "market",
            }
        ],
        "bonuses": [],
    }
    assert page.request.put(url + "/v1/contracts", headers=headers, data=body).status == 200
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-01-01")
    page.get_by_label("Price rule starts", exact=True).fill("2026-02-01")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-01-01")
    page.get_by_label("Price rule starts", exact=True).fill("2026-01-01")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert saved["periods"][0]["effective_from"] == "2025-12-31T23:00:00+00:00"


@pytest.mark.parametrize("country", [None, "SE"])
def test_contract_country_problem_preserves_saved_area_without_guessing(app_page, country):
    """Missing HA country or a saved NL area outside Sweden blocks editing without rewriting the existing contract."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    body = {
        "contract_id": "retained",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "effective_from": "2026-01-01",
                "effective_until": None,
                "import_formula": "market",
                "export_formula": "market",
            }
        ],
        "bonuses": [],
    }
    response = page.request.put(url + "/v1/contracts", headers=headers, data=body)
    assert response.status == 200
    saved = response.json()
    discover(page, url, country=country)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Home Assistant country")
    expect(page.locator("#contract-editor")).to_be_hidden()
    assert (
        page.request.get(url + "/v1/contracts", headers=headers).json()["version"]
        == saved["version"]
    )


def test_draft_bonus_budget_appears_inline_and_refreshes_after_history(app_page):
    """Desktop/mobile show history acquisition beside the draft, then measured 5 kWh and remaining 95/45 kWh as its cap changes. Nothing is saved."""
    page, url = app_page
    discover(page, url)
    requests = []

    def measured_preview(route):
        """Supply explicit display evidence after one pending response. Real history arithmetic is tested through the API separately."""
        result = route.fetch().json()
        rule = route.request.post_data_json["bonuses"][0]
        requests.append(rule)
        pending = len(requests) == 1
        result["bonus_budget_preview"] = [
            {
                "rule_id": result["bonuses"][0]["rule_id"],
                "period_start": "2025-12-31T23:00:00+00:00",
                "cap_kwh": rule["cap_kwh"],
                "used_kwh": None if pending else 5,
                "remaining_kwh": None if pending else max(0, rule["cap_kwh"] - 5),
                "measured_at": None if pending else datetime.now(UTC).isoformat(),
                "issue": "bonus_counter_baseline_required" if pending else None,
            }
        ]
        result["bonus_history_pending"] = pending
        route.fulfill(json=result)

    page.route("**/v1/contracts/preview", measured_preview)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_price_rule(page)
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    page.get_by_label("Annual export cap (kWh)", exact=True).fill("100")
    block = page.locator("#contract-preview .bonus-preview:visible")
    expect(block).to_contain_text("Reading Home Assistant history")
    expect(block).to_contain_text("Measured export: 5.00 kWh: Remaining: 95.00 kWh:")
    page.get_by_label("Annual export cap (kWh)", exact=True).fill("50")
    expect(block).to_contain_text("Remaining: 45.00 kWh:")
    saved = page.request.get(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    assert saved["version"] is None


def test_replacement_retains_absolute_supplier_start_instead_of_reinterpreting_its_date(app_page):
    """An API contract beginning at 09:00 UTC keeps that exact instant and bonus identity when copied in an Amsterdam browser editor."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    body = {
        "contract_id": "retained-absolute-start",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "contract_start": "2026-05-01T09:00:00+00:00",
                "effective_from": "2026-05-01T12:30:00+00:00",
                "effective_until": None,
                "import_formula": "market",
                "export_formula": "market",
            }
        ],
        "bonuses": [
            {
                "rule_id": "retained-counter",
                "effective_from": "2026-05-01T12:30:00+00:00",
                "rate_formula": "0.03",
                "cap_kwh": 1000,
                "timezone": "Europe/Amsterdam",
            }
        ],
    }
    assert page.request.put(url + "/v1/contracts", headers=headers, data=body).status == 200
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    add_price_rule(page)
    replacement = page.locator("#contract-periods > details").nth(1)
    replacement.get_by_label("Price rule starts", exact=True).fill("2027-01-01")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert [period["contract_start"] for period in saved["periods"]] == [
        "2026-05-01T09:00:00+00:00"
    ] * 2
    assert [rule["rule_id"] for rule in saved["bonuses"]] == ["retained-counter"] * 2


def test_contract_information_dialogs_explain_zonneplan_without_changing_the_draft(app_page):
    """Desktop/mobile supplier tabs keep values above their explanations, support keyboard navigation and preserve the draft."""

    def check_layout(panel):
        """Each visible field has a monospace value above a separate prose block, even on mobile."""
        fields = panel.locator("dd").evaluate_all("""elements => elements.map(element => {
            const value = element.querySelector('.example-value'), explanation = element.querySelector('p');
            return {separate: value.getBoundingClientRect().bottom <= explanation.getBoundingClientRect().top,
                    font: getComputedStyle(value).fontFamily, explanation: explanation.textContent.trim()};
        })""")
        assert fields and all(
            field["separate"] and "monospace" in field["font"] and field["explanation"]
            for field in fields
        )

    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    before = page.get_by_label("Import formula", exact=True).input_value()
    before_export = page.get_by_label("Export formula", exact=True).input_value()
    information = page.get_by_role("button", name="Price rule example", exact=True)
    information.focus()
    information.press("Enter")
    dialog = page.get_by_role("dialog", name="Price rule examples", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("published 2026 amounts")
    expect(dialog).to_contain_text("(market + 0.09161) * 1.21 + 0.02")
    expect(dialog).to_contain_text("not taxed twice")
    expect(dialog.get_by_role("link")).to_have_count(0)
    expect(dialog).to_contain_text("must be on or after Contract starts")
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_focused()
    zonneplan = dialog.get_by_role("tabpanel", name="Zonneplan", exact=True)
    base_export = zonneplan.locator("dd .example-value").last
    expect(base_export).to_have_text("market + 0.02")
    assert Formula(base_export.inner_text()).evaluate(0.06) == pytest.approx(0.08)
    assert Formula(base_export.inner_text()).evaluate(-0.03) == pytest.approx(-0.01)
    expect(zonneplan).to_contain_text("base export income is €0.08/kWh")
    check_layout(zonneplan)
    nextenergy = dialog.get_by_role("tab", name="NextEnergy", exact=True)
    nextenergy.click()
    expect(nextenergy).to_have_attribute("aria-selected", "true")
    expect(zonneplan).not_to_be_visible()
    panel = dialog.get_by_role("tabpanel", name="NextEnergy", exact=True)
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("(market + 0.09161) * 1.21 + 0.0219")
    base_export = panel.locator("dd .example-value").last
    expect(base_export).to_have_text("market")
    assert Formula(base_export.inner_text()).evaluate(0.06) == pytest.approx(0.06)
    assert Formula(base_export.inner_text()).evaluate(-0.03) == pytest.approx(-0.03)
    expect(panel).to_contain_text("base export income is €0.06/kWh")
    expect(panel).to_contain_text("purchase fee above is not automatically an export fee")
    check_layout(panel)
    nextenergy.press("Home")
    expect(dialog.get_by_role("tab", name="Zonneplan", exact=True)).to_be_focused()
    expect(zonneplan).to_be_visible()
    dialog.get_by_role("tab", name="Zonneplan", exact=True).press("ArrowLeft")
    expect(nextenergy).to_be_focused()
    expect(panel).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(information).to_be_focused()
    edit_price_rule(page)
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    bonus_information = page.get_by_role("button", name="Export bonus example", exact=True)
    bonus_information.click()
    dialog = page.get_by_role("dialog", name="Export bonus examples", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog).not_to_contain_text("Base export formula")
    expect(dialog.locator("dt").first).to_have_text("Extra export rate")
    expect(dialog).to_contain_text("0.10 * (market + 0.02)")
    expect(dialog).to_contain_text("Enable price condition")
    expect(dialog).to_contain_text("Greater than")
    expect(dialog).not_to_contain_text("Alternative:")
    expect(dialog.get_by_role("link")).to_have_count(0)
    expect(dialog).to_contain_text("total income €0.22")
    expect(dialog).to_contain_text("0.50 kWh of quota")
    expect(dialog).to_contain_text("Source exclusions are unsupported")
    check_layout(dialog.get_by_role("tabpanel", name="Zonneplan", exact=True))
    nextenergy = dialog.get_by_role("tab", name="NextEnergy", exact=True)
    nextenergy.click()
    panel = dialog.get_by_role("tabpanel", name="NextEnergy", exact=True)
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("0.50 * market")
    expect(panel).to_contain_text("Contract anniversary")
    expect(panel).to_contain_text("06:00")
    expect(panel).to_contain_text("22:00")
    check_layout(panel)
    nextenergy.press("ArrowRight")
    expect(dialog.get_by_role("tab", name="Zonneplan", exact=True)).to_be_focused()
    assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth")
    assert dialog.evaluate("element => element.getBoundingClientRect().width <= innerWidth")
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(bonus_information).to_be_focused()
    expect(page.get_by_label("Import formula", exact=True)).to_have_value(before)
    expect(page.get_by_label("Export formula", exact=True)).to_have_value(before_export)
    response = page.request.get(url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN})
    assert response.json()["version"] is None


def test_replacement_price_rule_copies_bonus_and_keeps_contract_anniversary(app_page):
    """Changed terms retain May's counter and disabling emits a zero rate. A March supplier change creates a new contract budget."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-05-12")
    page.get_by_label("Price rule starts", exact=True).fill("2026-08-20")
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    page.get_by_label("Extra export rate", exact=True).fill("0.03")
    page.get_by_label("Annual export cap (kWh)", exact=True).fill("1000")
    page.get_by_label("Annual reset", exact=True).select_option("contract_anniversary")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    add_price_rule(page)
    replacement = page.locator("#contract-periods > details:not([hidden])")
    expect(page.locator("#contract-status")).to_contain_text("Enter Price rule starts")
    expect(
        replacement.get_by_role("checkbox", name="Include export bonus", exact=True)
    ).to_be_checked()
    expect(replacement.get_by_label("Extra export rate", exact=True)).to_have_value("0.03")
    replacement.get_by_label("Price rule starts", exact=True).fill("2027-01-01")
    replacement.get_by_label("Contract starts", exact=True).fill("")
    expect(page.locator("#contract-status")).to_contain_text("Enter Contract starts")
    replacement.get_by_label("Contract starts", exact=True).fill("2026-05-12")
    expect(replacement.get_by_label("Price rule starts", exact=True)).to_have_attribute(
        "min", "2026-05-12"
    )
    replacement.get_by_label("Price rule starts", exact=True).fill("2026-05-11")
    expect(page.locator("#contract-status")).to_contain_text(
        "Price rule starts must be on or after Contract starts"
    )
    replacement.get_by_label("Price rule starts", exact=True).fill("2027-01-01")
    replacement.get_by_label("Import formula", exact=True).fill("market + 0.20")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    headers = {"Authorization": "Bearer " + TOKEN}
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert [period["contract_start"] for period in saved["periods"]] == [
        "2026-05-11T22:00:00+00:00"
    ] * 2
    assert UUID(saved["contract_id"]).version == 4
    assert saved["bonuses"][0]["rule_id"] == saved["bonuses"][1]["rule_id"]
    assert [rule["effective_from"] for rule in saved["bonuses"]] == [
        rule["effective_from"] for rule in saved["periods"]
    ]
    assert [(rule["reset_month"], rule["reset_day"]) for rule in saved["bonuses"]] == [
        (5, 12),
        (5, 12),
    ]
    edit_price_rule(page)
    replacement.get_by_role("checkbox", name="Include export bonus", exact=True).uncheck()
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert saved["bonuses"][1]["rate_formula"] == "0"
    assert saved["bonuses"][1]["rule_id"] == saved["bonuses"][0]["rule_id"]
    add_price_rule(page)
    supplier = page.locator("#contract-periods > details").nth(2)
    expect(supplier.get_by_label("Contract starts", exact=True)).to_have_value("2026-05-12")
    supplier.get_by_label("Contract starts", exact=True).fill("2027-03-01")
    supplier.get_by_label("Price rule starts", exact=True).fill("2027-03-01")
    supplier.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    supplier.get_by_label("Extra export rate", exact=True).fill("0.04")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert [period["contract_start"] for period in saved["periods"]] == [
        "2026-05-11T22:00:00+00:00",
        "2026-05-11T22:00:00+00:00",
        "2027-02-28T23:00:00+00:00",
    ]
    assert saved["bonuses"][2]["rule_id"] != saved["bonuses"][0]["rule_id"]
    assert (saved["bonuses"][2]["reset_month"], saved["bonuses"][2]["reset_day"]) == (3, 1)
    expect(
        page.locator("#reload-contract")
        if page.locator(".settings-toolbar").count()
        else page.get_by_role("button", name="Discard changes", exact=True)
    ).to_be_disabled()
    page.reload()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#contract-periods > details")).to_have_count(3)
    edit_price_rule(page)
    expect(supplier.get_by_label("Contract starts", exact=True)).to_have_value("2027-03-01")


def test_independently_dated_saved_bonus_joins_rule_boundary_only_on_save(app_page):
    """An old Jan price and July bonus render as complete dated rows. Loading never rewrites them, and explicit save aligns their equivalent boundaries."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    body = {
        "contract_id": "old-boundaries",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "effective_from": "2026-01-01T00:00:00+00:00",
                "effective_until": None,
                "import_formula": "market + 0.10",
                "export_formula": "market",
            }
        ],
        "bonuses": [
            {
                "rule_id": "retained-counter",
                "effective_from": "2026-07-01T00:00:00+00:00",
                "rate_formula": "0.03",
                "cap_kwh": 1000,
                "timezone": "Europe/Amsterdam",
            }
        ],
    }
    response = page.request.put(url + "/v1/contracts", headers=headers, data=body)
    assert response.status == 200
    original = response.json()
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#contract-periods > details")).to_have_count(2)
    assert (
        page.request.get(url + "/v1/contracts", headers=headers).json()["version"]
        == original["version"]
    )
    expect(page.get_by_label("Bonus rule ID", exact=True)).to_have_count(0)
    expect(page.locator("#save-contract")).to_be_hidden()
    page.locator("#price-rule-select").select_option("1")
    edit_price_rule(page)
    page.locator("#contract-periods > details").nth(1).get_by_label(
        "Annual export cap (kWh)", exact=True
    ).fill("1001")
    page.get_by_role("button", name="Preview rates", exact=True).click()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert len(saved["periods"]) == 2
    assert saved["contract_id"] == "old-boundaries"
    assert (
        saved["periods"][0]["import_formula"]
        == saved["periods"][1]["import_formula"]
        == "market + 0.10"
    )
    assert (
        saved["bonuses"][0]["effective_from"]
        == saved["periods"][1]["effective_from"]
        == original["bonuses"][0]["effective_from"]
    )
    assert saved["bonuses"][0]["rule_id"] == "retained-counter"
    assert saved["bonuses"][0]["cap_kwh"] == 1001


def test_contract_budget_wait_preserves_edits_and_locks_reloaded_controls(app_page):
    """A delayed budget read cannot baseline user edits. Discard keeps replacement form controls locked until its read finishes."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    expect(page.locator("#contract-save-bar")).to_be_hidden()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-01-01")
    page.get_by_label("Price rule starts", exact=True).fill("2026-01-01")
    page.get_by_label("Import formula", exact=True).fill("market + 0.10")
    invalid = page.locator("#contract-editor").evaluate(
        "form => [...form.elements].filter(input => input.willValidate && !input.validity.valid).map(input => ({label: input.getAttribute('aria-label'), value: input.value, issue: input.validationMessage}))"
    )
    assert not invalid, invalid
    page.get_by_role("button", name="Save contract", exact=True).click()

    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    page.reload()
    pending = []
    page.route("**/v1/contracts/budgets", lambda route: pending.append(route))
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    formula = page.get_by_label("Import formula", exact=True)
    expect(formula).to_have_value("market + 0.10")
    edit_price_rule(page)
    formula.fill("market + 0.22")
    expect(page.locator("#contract-save-state")).to_contain_text("Unsaved changes")
    assert len(pending) == 1
    pending.pop().fulfill(status=200, json={"budgets": []})
    expect(page.locator("#budget-status")).to_contain_text("No active saved bonus")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    expect(formula).to_have_value("market + 0.22")
    page.get_by_role(
        "button",
        name="Cancel" if page.locator(".settings-toolbar").count() else "Discard changes",
        exact=True,
    ).click()
    expect(formula).to_have_value("market + 0.10")
    expect(formula).to_be_disabled()
    assert len(pending) == 1
    pending.pop().fulfill(status=200, json={"budgets": []})
    expect(formula).to_be_disabled()
    expect(page.locator("#contract-save-state")).to_contain_text("All changes saved")


@pytest.mark.parametrize("app_site", ["prices"], indirect=True)
def test_current_price_chart_shows_saved_bonus_without_any_plan_or_volume_history(app_page):
    """No plan exists. Cached .10 market gives .08 export and .11 including bonus, then .12 immediately after a saved rule change."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    body = {
        "contract_id": "price-preview",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_until": None,
                "import_formula": "market + 0.10",
                "export_formula": "market - 0.02",
            }
        ],
        "bonuses": [
            {
                "rule_id": "preview",
                "effective_from": "2026-01-01T00:00:00Z",
                "rate_formula": "0.03",
                "cap_kwh": 7500,
                "timezone": "Europe/Amsterdam",
            }
        ],
    }
    response = page.request.put(url + "/v1/contracts", headers=headers, data=body)
    assert response.ok
    saved = response.json()
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_role("heading", name="Price inputs", exact=True)).to_be_visible()
    expect(page.locator("#price-chart-legend")).to_contain_text("Export rate incl. bonus")
    expect(page.locator("#current-price-status")).to_contain_text("Bonus volume is not applied")
    values = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).data.datasets.at(-1).data.map(point => point.y)"
    )
    assert values and all(value == pytest.approx(0.11) for value in values)
    assert page.request.get(url + "/v1/plans", headers=headers).json()["plans"] == []
    page.get_by_role("button", name="Next 6 hours", exact=True).click()
    assert page.evaluate(
        "(() => {const axis=Chart.getChart(document.getElementById('price-chart')).options.scales.x;return axis.min%900000===0 && axis.max-axis.min===21600000;})()"
    )
    body["expected_version"] = saved["version"]
    body["bonuses"][0]["rate_formula"] = "0.04"
    assert page.request.put(url + "/v1/contracts", headers=headers, data=body).ok
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    page.wait_for_function(
        "() => Chart.getChart(document.getElementById('price-chart')).data.datasets.at(-1).data.every(point => Math.abs(point.y-0.12)<1e-12)"
    )
    page.route("**/v1/prices/inputs", lambda route: route.abort())
    page.get_by_role("button", name="Refresh overview", exact=True).click()
    expect(page.locator("#current-price-status")).to_contain_text("Failed to fetch")
    assert (
        page.evaluate("Chart.getChart(document.getElementById('price-chart')).data.datasets.length")
        == 0
    )


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_bonus_chart_preserves_microsecond_daylight_edges_without_false_gaps(app_page):
    """Exact daylight eligibility remains in prices and inspection while the hour renders four quarter balance bars."""
    page, url = app_page

    def daylight_edge(route):
        """Give the frozen two-interval chart exact authoritative endpoints and independent .08/.11 expected rates."""
        body = route.fetch().json()
        plan = body["plan"]
        intervals = plan["input_snapshot"]["problem"]["intervals"]
        start = datetime(2026, 10, 27, 6, tzinfo=UTC)
        edge = datetime(2026, 10, 27, 6, 19, 27, 14508, tzinfo=UTC)
        end = datetime(2026, 10, 27, 7, tzinfo=UTC)
        for index, (left, right) in enumerate(((start, edge), (edge, end))):
            intervals[index].update(
                start=left.isoformat(),
                duration_h=(right - left).total_seconds() / 3600,
                sell_price=0.08,
            )
            plan["steps"][index].update(start=left.isoformat(), end=right.isoformat())
        # One unchanged device command can span several independent billing intervals.
        plan["steps"] = [{**plan["steps"][0], "end": end.isoformat()}]
        plan["input_snapshot"]["tariff_evidence"] = [
            {"start": left.isoformat(), "end": right.isoformat(), "market_price": 0.10}
            for left, right in ((start, edge), (edge, end))
        ]
        plan["input_snapshot"]["problem"]["export_bonuses"] = [
            {
                "rule_id": "daylight",
                "rates": [0, 0.03],
                "caps_kwh": [7500, 7500],
                "initial_kwh": 4000,
            }
        ]
        route.fulfill(json=body)

    page.route("**/v1/plans/*/comparison", daylight_edge)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-chart-legend")).to_contain_text("Export rate incl. bonus")
    values = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).data.datasets.at(-1).data.map(point => point.y)"
    )
    assert values == pytest.approx([0.08, 0.08, 0.11, 0.11])
    x_values = page.evaluate(
        "Chart.getChart(document.getElementById('price-chart')).data.datasets.at(-1).data.map(point => point.x)"
    )
    assert x_values == [1793080800000, 1793081967014, 1793081967014, 1793084400000]
    bars = page.evaluate(
        "Chart.getChart(document.getElementById('plan-chart')).data.datasets[0].data.map(point => [point.start,point.end])"
    )
    assert bars == [
        [1793080800000 + index * 900000, 1793080800000 + (index + 1) * 900000] for index in range(4)
    ]
    assert page.locator("#inspect-time").get_attribute("max") == "1"


@pytest.mark.parametrize("origin", ["automatic", "manual", "roundoff", "concurrent"])
def test_central_bonus_budget_shows_opening_origin_and_preserves_price_draft(app_page, origin):
    """Exercise desktop/mobile opening origins and preserve drafts across refreshes.

    Ordinary hours add 2 kWh across two statistics series. Live physical meter
    updates add nothing. A late reference asks only for the missing prefix
    and adds the two measured kWh that follow it. Recorder roundoff adds no export.
    Refreshes preserve newer drafts. Rejected confirmations keep the opening
    draft, while accepted writes remove it even if the follow-up read fails.
    Refresh failures preserve the price draft and leave refresh available.
    A concurrent contract save retains its newer budget-read status and keeps
    Refresh available.
    The next reset month uses the discovered HA timezone to avoid the annual
    reset boundary and retain usable historical data.
    """
    page, url = app_page
    catalogue = discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    bindings = [
        {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"},
        {"key": "grid.export_energy", "entity_id": "sensor.export", "quantity": "energy"},
        {"key": "grid.export_energy.second", "entity_id": "sensor.export2", "quantity": "energy"},
    ]
    response = page.request.put(url + "/v1/sources", headers=headers, data={"bindings": bindings})
    assert response.ok
    revision = response.json()["config_revision"]
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    now = datetime.now(UTC)
    supplier_start = (
        (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    )
    contract = {
        "contract_id": saved["contract_id"],
        "expected_version": saved["version"],
        "market_area": "NL",
        "currency": "EUR",
        "periods": [
            {
                "effective_from": supplier_start,
                "effective_until": None,
                "contract_start": supplier_start,
                "import_formula": "market + 0.1",
                "export_formula": "market",
            }
        ],
        "bonuses": [
            {
                "rule_id": "solar",
                "effective_from": supplier_start,
                "rate_formula": "0.04",
                "cap_kwh": 100000,
                "reset_month": now.astimezone(ZoneInfo(catalogue["timezone"])).month % 12 + 1,
                "reset_day": 1,
                "timezone": "UTC",
            }
        ],
    }
    assert page.request.put(url + "/v1/contracts", headers=headers, data=contract).ok
    request = page.request.get(url + "/v1/snapshot", headers=headers).json()["bonus_history"][
        "request"
    ]
    first, end = (datetime.fromisoformat(request[key]) for key in ("start", "end"))
    count = int((end - first).total_seconds() / 3600)
    hours = [
        [(first + timedelta(hours=index)).timestamp(), 100 + 2 * index, 100 + 2 * index]
        for index in range(count)
    ]
    endpoint = hours[-1][2]
    history_body = {
        "request_version": request["version"],
        "entity_id": request["entity_id"],
        "unit": "kWh",
        "hours": hours,
        "component_meters": [["sensor.export", endpoint * 0.6], ["sensor.export2", endpoint * 0.4]],
    }
    values = (0, endpoint * 0.6 + 1, endpoint * 0.4 + 2)
    report = {
        "config_revision": revision,
        "captured_at": now.isoformat(),
        "issues": [],
        "readings": [
            {
                "key": spec["key"],
                "entity_id": spec["entity_id"],
                "value": value,
                "unit": "W" if spec["quantity"] == "power" else "kWh",
                "timestamp_kind": "ha_report",
                "observed_at": now.isoformat(),
                "reported_at": now.isoformat(),
            }
            for spec, value in zip(bindings, values, strict=True)
        ],
    }
    assert page.request.post(url + "/v1/observations", headers=headers, data=report).ok
    selected = hours if origin == "automatic" else hours[-2:]
    history_body = {
        "request_version": request["version"],
        "statistics": [
            {
                "statistic_id": entity,
                "metadata_id": index + 1,
                "source": "recorder",
                "unit": "kWh",
                "hours": [[row[0], row[1] * factor, row[2] * factor] for row in selected],
            }
            for index, (entity, factor) in enumerate(
                (("sensor.export", 0.6), ("sensor.export2", 0.4))
            )
        ],
    }
    if origin == "roundoff":
        for channel in history_body["statistics"]:
            channel["hours"][0][1] = 0
            channel["hours"][1][1] = -1.225398591486737e-7
    assert page.request.post(url + "/v1/history", headers=headers, data=history_body).ok
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    panel = page.locator("#contract-budget")
    expect(panel.get_by_role("heading", name="Current bonus budget", exact=True)).to_be_visible()
    if origin != "automatic":
        expect(panel).to_contain_text("Only export before that endpoint", ignore_case=True)
        invalid_requests = []

        def record_invalid_confirmation_request(request):
            """A purely local invalid value must not read or write the saved budget."""
            if request.url.endswith("/v1/contracts/budgets"):
                invalid_requests.append(request.method)

        page.on("request", record_invalid_confirmation_request)
        for invalid_value in ("", "-1"):
            used = panel.get_by_label("Export before reference (kWh)", exact=True)
            used.fill(invalid_value)
            panel.get_by_role("button", name="Confirm opening volume", exact=True).click()
            expect(page.locator("#budget-status")).to_have_text(
                "Enter a nonnegative confirmed used volume."
            )
            expect(used).to_be_focused()
            expect(used).to_have_value(invalid_value)
            expect(page.get_by_role("button", name="Refresh budget", exact=True)).to_be_enabled()
            assert invalid_requests == []
        page.remove_listener("request", record_invalid_confirmation_request)
        panel.get_by_label("Export before reference (kWh)", exact=True).fill("25")
        refreshed_budget = page.request.get(url + "/v1/contracts/budgets", headers=headers).json()

        def refresh_after_edit(route):
            """Hold the actual request while checking loading and typing a newer draft."""
            expect(page.locator("#budget-status")).to_have_text(
                "Reading the latest bonus budget..."
            )
            expect(page.get_by_role("button", name="Refresh budget", exact=True)).to_be_disabled()
            panel.get_by_label("Export before reference (kWh)", exact=True).fill("26")
            route.fulfill(status=200, json=refreshed_budget)

        page.route("**/v1/contracts/budgets", refresh_after_edit)
        page.get_by_role("button", name="Refresh budget", exact=True).click()
        expect(page.locator("#budget-status")).to_contain_text("Budget refreshed")
        expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_value("26")
        page.unroute("**/v1/contracts/budgets")
        interrupted = deepcopy(refreshed_budget)
        interrupted["budgets"][0].update(
            issue="bonus_statistics_revalidation_required", confirmation=None
        )
        page.route(
            "**/v1/contracts/budgets",
            lambda route: route.fulfill(status=200, json=interrupted),
            times=1,
        )
        page.get_by_role("button", name="Refresh budget", exact=True).click()
        expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_count(0)
        page.get_by_role("button", name="Refresh budget", exact=True).click()
        expect(page.locator("#budget-status")).to_contain_text("Budget refreshed")
        expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_value("26")
        changed = deepcopy(refreshed_budget)
        changed["budgets"][0]["confirmation"]["reference_version"] = "different-reference"
        page.route(
            "**/v1/contracts/budgets",
            lambda route: route.fulfill(status=200, json=changed),
            times=1,
        )
        page.get_by_role("button", name="Refresh budget", exact=True).click()
        expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_value("")
        page.get_by_role("button", name="Refresh budget", exact=True).click()
        expect(page.locator("#budget-status")).to_contain_text("Budget refreshed")
        expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_value("26")
        panel.get_by_label("Export before reference (kWh)", exact=True).fill("25")

        def reject_confirmation(route):
            """A rejected write and optionally failed recovery read keep the draft and retry controls."""
            if route.request.method == "POST":
                route.fulfill(status=409, json={"error": "Reference changed"})
            elif origin == "roundoff":
                route.fulfill(status=503, json={"error": "Budget temporarily unavailable"})
            else:
                route.fallback()

        page.route("**/v1/contracts/budgets", reject_confirmation)
        panel.get_by_role("button", name="Confirm opening volume", exact=True).click()
        expect(page.locator("#budget-status")).to_contain_text("409")
        if origin == "roundoff":
            expect(page.locator("#budget-status")).to_contain_text("Budget refresh failed")
        expect(
            panel.get_by_role("button", name="Confirm opening volume", exact=True)
        ).to_be_enabled()
        expect(page.get_by_role("button", name="Refresh budget", exact=True)).to_be_enabled()
        expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_value("25")
        page.unroute("**/v1/contracts/budgets")

        if origin == "concurrent":
            edit_price_rule(page)
            page.get_by_label("Annual export cap (kWh)", exact=True).first.fill("90000")
            page.get_by_role("button", name="Preview rates", exact=True).click()
            expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()

            def reject_confirmation_after_save(route):
                """Save a changed cap while POST is pending, then reject that old confirmation."""
                if route.request.method == "POST":
                    page.get_by_role("button", name="Save contract", exact=True).click()
                    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
                    route.fulfill(status=409, json={"error": "Contract changed"})
                else:
                    route.fallback()

            page.route("**/v1/contracts/budgets", reject_confirmation_after_save)
            panel.get_by_role("button", name="Confirm opening volume", exact=True).click()
            expect(
                panel.locator("dl > div").filter(has_text="Annual cap").locator("dd")
            ).to_have_text("90,000.00 kWh")
            expect(page.locator("#budget-status")).to_contain_text("409")
            expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_value(
                "25"
            )
            expect(page.get_by_role("button", name="Refresh budget", exact=True)).to_be_enabled()
            page.unroute("**/v1/contracts/budgets")
            refreshed_budget = page.request.get(
                url + "/v1/contracts/budgets", headers=headers
            ).json()

            edit_price_rule(page)
            page.get_by_label("Import formula", exact=True).first.fill("market + 0.04")
            page.get_by_role("button", name="Preview rates", exact=True).click()
            expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
            panel.get_by_role("button", name="Confirm opening volume", exact=True).evaluate(
                """button => {
                    const confirm = button.onclick;
                    button.onclick = event => {
                        window.confirmationFinished = confirm.call(button, event);
                        return window.confirmationFinished;
                    };
                }"""
            )

        held_read = None
        superseded_read = None
        superseded_status = None

        def fail_confirmation_read(route):
            """Fail the accepted write's read, optionally after a real save starts a newer read."""
            nonlocal held_read, superseded_read, superseded_status
            if route.request.method == "POST":
                held_read.fulfill(status=200, json=refreshed_budget)
                expect(
                    panel.get_by_role("button", name="Confirm opening volume", exact=True)
                ).to_be_disabled()
                route.fallback()
            elif held_read is None:
                held_read = route
                panel.get_by_role("button", name="Confirm opening volume", exact=True).click()
            elif origin == "concurrent" and superseded_read is None:
                superseded_read = route
                page.get_by_role("button", name="Save contract", exact=True).click()
            elif origin == "concurrent":
                superseded_read.fulfill(status=503, json={"error": "Older read failed"})
                try:
                    page.evaluate("() => window.confirmationFinished")
                    superseded_status = page.locator("#budget-status").inner_text()
                finally:
                    current = page.request.get(url + "/v1/contracts/budgets", headers=headers)
                    assert current.ok
                    route.fulfill(status=200, json=current.json())
            else:
                route.fulfill(status=503, json={"error": "Budget temporarily unavailable"})

        page.route("**/v1/contracts/budgets", fail_confirmation_read)
        page.get_by_role("button", name="Refresh budget", exact=True).click()
        if origin == "concurrent":
            expect(page.locator("#budget-status")).to_have_text(
                "Measured export volumes are independent of proposed plans."
            )
            assert superseded_status == "Reading the latest bonus budget..."
        else:
            expect(page.locator("#budget-status")).to_contain_text(
                "Opening volume saved. Budget refresh failed"
            )
        expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_count(0)
        expect(
            panel.get_by_role("button", name="Confirm opening volume", exact=True)
        ).to_have_count(0)
        expect(page.get_by_role("button", name="Refresh budget", exact=True)).to_be_enabled()
        accepted = page.request.get(url + "/v1/contracts/budgets", headers=headers).json()
        assert accepted["budgets"][0]["opening"]["used_kwh"] == 25
        page.unroute("**/v1/contracts/budgets")
        page.get_by_role("button", name="Refresh budget", exact=True).click()
        expect(panel).to_contain_text("Manually confirmed")
        expect(panel.locator("dl > div").filter(has_text="Used export").locator("dd")).to_have_text(
            "25.00 kWh" if origin == "roundoff" else "27.00 kWh"
        )
    else:
        expect(panel).to_contain_text(
            "Automatically derived from Home Assistant history"
            if origin == "automatic"
            else "Origin unknown"
        )
    expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_count(0)
    expect(
        panel.locator("dl > div").filter(has_text="Export since opening").locator("dd")
    ).to_have_text(
        f"{2 * (len(hours) - 1):,.2f} kWh"
        if origin == "automatic"
        else "0.00 kWh"
        if origin == "roundoff"
        else "2.00 kWh"
    )
    panel.get_by_text("Opening evidence and grid export meters", exact=True).click()
    expect(panel).to_contain_text("sensor.export")
    expect(panel).to_contain_text("sensor.export2")
    expect(panel).to_contain_text("Total used = opening")
    if origin == "automatic":
        expected = 2 * (len(hours) - 1)
        expect(panel).to_contain_text(f"= {expected:,.2f} kWh")
        expect(panel).to_contain_text("History digest")
    else:
        expect(panel).to_contain_text("25.00 kWh")
    edit_price_rule(page)
    field = page.get_by_label("Import formula", exact=True).first
    field.fill("market + 0.05")
    page.get_by_role("button", name="Refresh budget", exact=True).click()
    expect(page.locator("#budget-status")).to_contain_text("Budget refreshed")
    expect(field).to_have_value("market + 0.05")
    expect(page.locator("#contract-save-state")).to_contain_text("Unsaved")
    unavailable = page.request.get(url + "/v1/contracts/budgets", headers=headers).json()
    unavailable["budgets"][0].update(
        issue="bonus_counter_baseline_required",
        confirmation=None,
        statistics=None,
        opening=None,
        used_kwh=None,
        remaining_kwh=None,
    )
    page.route("**/v1/contracts/budgets", lambda route: route.fulfill(status=200, json=unavailable))
    page.get_by_role("button", name="Refresh budget", exact=True).click()
    expect(panel).to_contain_text("Opening confirmation needs a validated history reference first")
    expect(panel.get_by_label("Export before reference (kWh)", exact=True)).to_have_count(0)
    expect(field).to_be_editable()
    expect(field).to_have_value("market + 0.05")
    page.route(
        "**/v1/contracts/budgets",
        lambda route: route.fulfill(status=503, json={"error": "History temporarily unavailable"}),
    )
    page.get_by_role("button", name="Refresh budget", exact=True).click()
    expect(page.locator("#budget-status")).to_contain_text("Budget refresh failed")
    expect(field).to_have_value("market + 0.05")
    expect(page.get_by_role("button", name="Refresh budget", exact=True)).to_be_enabled()
    page.unroute("**/v1/contracts/budgets")
    page.get_by_role("button", name="Refresh budget", exact=True).click()
    expect(page.locator("#budget-status")).to_contain_text("Budget refreshed")
    expect(panel).not_to_contain_text(
        "Opening confirmation needs a validated history reference first"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize(
    "timezone, reset",
    [
        ("Europe/Amsterdam", "2025-12-31T23:00:00+00:00"),
        ("America/New_York", "2026-01-01T05:00:00+00:00"),
    ],
)
def test_contract_prototype_summary_combined_rates_and_annual_budget(app_page, timezone, reset):
    """Saved terms stay a summary until Edit. Five combined rate samples and a 95-of-100-kWh allowance remain beside it."""
    page, url = app_page
    discover(page, url, timezone=timezone)
    headers = {"Authorization": "Bearer " + TOKEN}
    body = {
        "contract_id": "prototype-parity",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "contract_start": "2026-01-01",
                "effective_from": "2026-01-01",
                "effective_until": None,
                "import_formula": "(market + 0.10) * 1.21",
                "export_formula": "market - 0.02",
            }
        ],
        "bonuses": [
            {
                "effective_from": "2026-01-01",
                "rate_formula": "0.10 * max(market, 0)",
                "cap_kwh": 100,
                "reset_month": 1,
                "reset_day": 1,
                "timezone": timezone,
                "window": "always",
            }
        ],
    }
    response = page.request.put(url + "/v1/contracts", headers=headers, data=body)
    assert response.ok
    version = response.json()["version"]
    actual = page.request.get(url + "/v1/contracts/budgets", headers=headers).json()["budgets"][0]
    assert "T" in actual["period_start"] and actual["period_start"].endswith("+00:00")
    page.route(
        "**/v1/contracts/budgets",
        lambda route: route.fulfill(
            json={
                "budgets": [
                    {
                        "active": True,
                        "period_start": reset,
                        "cap_kwh": 100,
                        "used_kwh": 5,
                        "remaining_kwh": 95,
                        "measured_at": "2026-10-26T10:00:00+00:00",
                        "issue": None,
                        "meters": [],
                    }
                ]
            }
        ),
    )
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    summary = page.locator("#contract-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("(market + 0.10) * 1.21")
    expect(page.locator("#contract-periods")).to_be_hidden()
    expect(page.get_by_role("button", name="Preview rates", exact=True)).to_be_hidden()
    rows = page.locator("#contract-preview .price-preview tbody tr")
    expect(rows).to_have_count(5)
    expect(page.locator("#contract-preview th")).to_have_text(
        ["Spot", "Import cost", "Base export", "Extra bonus"]
    )
    expect(rows.nth(3).locator("td").nth(3)).to_have_text("0.01")
    expect(page.locator("#bonus-budgets meter")).to_have_attribute("max", "100")
    expect(page.locator("#bonus-budgets meter")).to_have_attribute("value", "5")
    expect(page.locator(".budget-remaining strong")).to_have_text("95.00")
    expect(page.locator(".budget-period")).to_have_text("Current annual period: 2026-01-01")
    page.get_by_role("button", name="Edit price rule", exact=True).click()
    expect(summary).to_be_hidden()
    start = page.get_by_label("Contract starts", exact=True).bounding_box()
    effective = page.get_by_label("Price rule starts", exact=True).bounding_box()
    if page.viewport_size["width"] > 700:
        assert abs(start["y"] - effective["y"]) <= 1
    else:
        assert effective["y"] > start["y"] + start["height"]
    page.get_by_label("Import formula", exact=True).fill("market + 0.20")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("(market + 0.10) * 1.21")
    assert page.request.get(url + "/v1/contracts", headers=headers).json()["version"] == version
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_contract_selector_inserts_and_moves_rules_in_date_order_without_stale_labels(app_page):
    """An April rule inserted between January and July stays selected and can move past July without corrupting coverage."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    body = {
        "contract_id": "sorted-layout",
        "market_area": "NL",
        "currency": "EUR",
        "expected_version": None,
        "periods": [
            {
                "contract_start": "2026-01-01T00:00:00+00:00",
                "effective_from": "2026-01-01T00:00:00+00:00",
                "effective_until": "2026-07-01T00:00:00+00:00",
                "import_formula": "market + 0.01",
                "export_formula": "market",
            },
            {
                "contract_start": "2026-01-01T00:00:00+00:00",
                "effective_from": "2026-07-01T00:00:00+00:00",
                "effective_until": None,
                "import_formula": "market + 0.07",
                "export_formula": "market",
            },
        ],
        "bonuses": [],
    }
    assert page.request.put(url + "/v1/contracts", headers=headers, data=body).ok
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#price-rule-select")).to_be_enabled()
    page.locator("#price-rule-select").select_option("0")
    add_price_rule(page)
    selected = page.locator("#contract-periods > details:not([hidden])")
    expect(selected.get_by_label("Import formula", exact=True)).to_have_value("market + 0.01")
    selected.get_by_label("Price rule starts", exact=True).fill("2026-04-01")
    selected.get_by_label("Import formula", exact=True).fill("market + 0.04")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    expect(page.locator("#price-rule-select option:checked")).to_contain_text("2026-04-01")
    expect(page.locator("#price-rule-select option:checked")).not_to_contain_text("Unsaved")
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert [period["import_formula"] for period in saved["periods"]] == [
        "market + 0.01",
        "market + 0.04",
        "market + 0.07",
    ]
    assert (
        saved["periods"][0]["effective_until"]
        == saved["periods"][1]["effective_from"]
        == "2026-03-31T22:00:00+00:00"
    )
    assert (
        saved["periods"][1]["effective_until"]
        == saved["periods"][2]["effective_from"]
        == "2026-07-01T00:00:00+00:00"
    )
    edit_price_rule(page)
    selected.get_by_label("Price rule starts", exact=True).fill("2026-08-01")
    expect(selected.get_by_label("Import formula", exact=True)).to_have_value("market + 0.04")
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    saved = page.request.get(url + "/v1/contracts", headers=headers).json()
    assert [period["import_formula"] for period in saved["periods"]] == [
        "market + 0.01",
        "market + 0.07",
        "market + 0.04",
    ]
    assert (
        saved["periods"][1]["effective_until"]
        == saved["periods"][2]["effective_from"]
        == "2026-07-31T22:00:00+00:00"
    )
    expect(page.locator("#price-rule-select option:checked")).to_contain_text("2026-08-01")


@pytest.mark.parametrize(
    "app_page",
    [{"width": 1280, "height": 900}, {"width": 768, "height": 1024}, {"width": 390, "height": 844}],
    indirect=True,
    ids=["desktop", "tablet", "mobile"],
)
def test_monthly_bonus_editor_saves_reloads_and_switches_strategy(app_page):
    """Desktop/tablet/mobile save exact month ranges and disabled months. Inactive fields never block saving."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-01-01")
    page.get_by_label("Price rule starts", exact=True).fill("2026-01-01")
    page.get_by_label("Import formula", exact=True).fill("market + 0.10")
    page.get_by_label("Export formula", exact=True).fill("market")
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    page.get_by_label("Extra export rate", exact=True).fill("0.04")
    page.get_by_label("Annual export cap (kWh)", exact=True).fill("7500")
    page.get_by_label("Paid window", exact=True).select_option("monthly_schedule")
    expect(page.get_by_label("Window start", exact=True)).to_be_hidden()
    page.get_by_text("Monthly hours", exact=True).click()
    expect(page.get_by_label("January start", exact=True)).not_to_have_attribute(
        "inputmode", "numeric"
    )
    expect(page.get_by_label("January end", exact=True)).not_to_have_attribute(
        "inputmode", "numeric"
    )
    page.get_by_label("January start", exact=True).fill("")
    page.get_by_label("January end", exact=True).fill("")
    for title in ("January start", "January end"):
        assert page.get_by_label(title, exact=True).evaluate("input => input.validity.valueMissing")
    page.get_by_label("January start", exact=True).fill("22:00")
    page.get_by_label("January end", exact=True).fill("06:00")
    page.get_by_label("February enabled", exact=True).uncheck()
    expect(page.get_by_label("February start", exact=True)).to_be_disabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".monthly-window").screenshot(
        path=f"results/ci/bonus-monthly-{page.viewport_size['width']}.png"
    )
    page.get_by_role("button", name="Preview rates", exact=True).click()
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_have_text(
        "Contract saved. New planning and affected cost evaluations use this revision. "
        "Original plans are preserved."
    )
    body = page.request.get(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    months = body["bonuses"][0]["monthly_schedule"]
    assert months[0] == {"start_time": "22:00", "end_time": "06:00"}
    assert months[1] is None
    assert len(months) == 12
    page.reload()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    edit_price_rule(page)
    expect(page.get_by_label("Paid window", exact=True)).to_have_value("monthly_schedule")
    page.get_by_text("Monthly hours", exact=True).click()
    expect(page.get_by_label("January start", exact=True)).to_have_value("22:00")
    expect(page.get_by_label("February enabled", exact=True)).not_to_be_checked()
    page.get_by_label("January start", exact=True).fill("invalid")
    page.get_by_label("Paid window", exact=True).select_option("always")
    expect(page.get_by_label("January start", exact=True)).to_be_disabled()
    expect(page.get_by_text("Monthly hours", exact=True)).to_be_hidden()
    page.get_by_role("button", name="Preview rates", exact=True).click()
    expect(page.get_by_role("button", name="Save contract", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_have_text(
        "Contract saved. New planning and affected cost evaluations use this revision. "
        "Original plans are preserved."
    )
    saved = page.request.get(
        url + "/v1/contracts", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    assert "monthly_schedule" not in saved["bonuses"][0]
