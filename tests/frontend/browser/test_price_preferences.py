"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    connect,
    discover,
    read_settings,
    show_installation_sources,
)


@pytest.mark.parametrize("app_site", ["prices"], indirect=True)
def test_active_price_sources_distinguish_published_preference_and_valuation_tail(app_page):
    """Desktop/mobile users see published prices beyond 24h, the overlap preference and the initial Epex valuation preference."""
    page, url = app_page
    discover(page, url)
    connect(page)
    show_installation_sources(page)
    expect(page.get_by_role("heading", name="Active price sources")).to_be_visible()
    rows = page.locator("#price-source-list li")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_contain_text("Published market prices used")
    expect(page.get_by_role("heading", name="Nord Pool: Fixed price basis")).to_be_visible()
    expect(page.locator("#price-published-coverage")).to_contain_text("Known prices from now until")
    expect(page.locator("#price-horizon")).to_contain_text("Blocks count hours ahead from now")
    expect(rows.nth(1)).to_contain_text("Nord Pool (published), EnergyPriceForecast EU")
    expect(rows.nth(1)).to_contain_text("Collecting comparable forecast history")
    expect(rows.nth(1).locator("dl").first.locator("dd").nth(2)).to_have_text("0")
    expect(rows.nth(1).locator("dl").first.locator("dd").nth(3)).to_have_text("0")
    expect(rows.nth(2)).to_contain_text("Active: EpexPredictor")
    expect(rows.nth(2)).to_contain_text("Collecting comparable forecast history")
    expect(page.locator("#price-sources")).to_contain_text("They do not extend the plan")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("app_site", ["prices_corrupt"], indirect=True)
def test_price_status_explains_unreadable_preference_without_claiming_retention(app_page):
    """A failed preference restore leaves usable prices visible but explicitly warns that selection may be defaulted."""
    page, url = app_page
    discover(page, url)
    connect(page)
    expect(page.locator("#price-source-list")).to_contain_text(
        "The preference may be defaulted after restart"
    )
    expect(page.locator("#price-source-list")).not_to_contain_text(
        "retaining the last known preference"
    )


@pytest.mark.parametrize("app_site", ["prices"], indirect=True)
def test_manual_price_source_preserves_draft_on_poll_and_saves_only_source_preferences(app_page):
    """Desktop/mobile users retain installation edits and focused source choices across polling and an independent save."""
    page, url = app_page
    page.clock.install()
    discover(page, url)
    connect(page)
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("6")
    show_installation_sources(page)
    controls = page.locator("[data-price-preference]")
    expect(controls).to_have_count(3)
    expect(controls.nth(1)).to_be_disabled()
    page.get_by_role("button", name="Edit installation", exact=True).click()
    for window_index in (0, 1):
        assert (
            controls.nth(window_index)
            .locator('option[value="energy_price_forecast_eu"]')
            .evaluate("option => option.disabled")
            is False
        )
    unsupported_source = controls.nth(2).locator('option[value="energy_price_forecast_eu"]')
    assert unsupported_source.evaluate("option => option.disabled") is True
    expect(unsupported_source).to_have_text("EnergyPriceForecast EU: Up to 48 hours")
    for source_id in ("epex_predictor", "jeroen_nl"):
        assert (
            controls.nth(2)
            .locator(f'option[value="{source_id}"]')
            .evaluate("option => option.disabled")
            is False
        )
    rejected_preference = page.request.put(
        url + "/v1/prices/preferences",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={
            "preferences": [None, None, "energy_price_forecast_eu"],
            "configuration_version": None,
            "market_area": "NL",
        },
    )
    assert rejected_preference.status == 400
    assert "does not support" in rejected_preference.text()
    assert controls.nth(0).evaluate(
        "control => control.getBoundingClientRect().top >= document.getElementById('installation-toolbar').getBoundingClientRect().bottom"
    )
    controls.nth(1).select_option("jeroen_nl")
    controls.nth(1).focus()
    with page.expect_response(
        lambda response: response.url.endswith("/v1/prices") and response.request.method == "GET"
    ):
        page.clock.fast_forward(31000)
    expect(controls.nth(1)).to_have_value("jeroen_nl")
    expect(controls.nth(1)).to_be_focused()
    writes = []
    page.on(
        "request", lambda request: writes.append(request.url) if request.method == "PUT" else None
    )
    page.get_by_role("button", name="Save price sources", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text(
        "Price source preferences saved"
    )
    expect(controls.nth(1)).to_be_disabled()
    row = page.locator("#price-source-list li").nth(1)
    expect(row).to_contain_text("Jeroen.nl")
    expect(row).to_contain_text("Automatic recommendation: EnergyPriceForecast EU")
    assert writes and all(url.endswith("/v1/prices/preferences") for url in writes)
    assert read_settings(page, url)["installation"] is None
    page.get_by_role("button", name="Back to setup", exact=True).click()
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_label("Maximum grid import (kW)", exact=True)).to_have_value("6")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("app_site", ["prices"], indirect=True)
def test_price_source_failure_keeps_editable_draft_and_cancel_discards_only_that_draft(app_page):
    """A failed preference write stays unsaved and retryable. Cancel restores only source choices, retaining physical drafts."""
    page, url = app_page
    discover(page, url)
    connect(page)
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("6")
    show_installation_sources(page)
    page.get_by_role("button", name="Edit installation", exact=True).click()
    control = page.locator("[data-price-preference]").nth(1)
    control.select_option("jeroen_nl")
    page.route(
        "**/v1/prices/preferences",
        lambda route: route.fulfill(status=503, body="Storage temporarily unavailable"),
    )
    page.get_by_role("button", name="Save price sources", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("Save could not be confirmed")
    expect(control).to_have_value("jeroen_nl")
    expect(control).to_be_enabled()
    expect(page.get_by_role("button", name="Save price sources", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(control).to_have_value("")
    expect(control).to_be_disabled()
    assert read_settings(page, url)["installation"] is None
    page.get_by_role("button", name="Back to setup", exact=True).click()
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_label("Maximum grid import (kW)", exact=True)).to_have_value("6")


@pytest.mark.parametrize("app_site", ["prices"], indirect=True)
def test_late_price_status_cannot_replace_a_confirmed_manual_save(app_page):
    """A poll issued before a successful save cannot restore old preferences or active-source claims afterward."""
    page, url = app_page
    page.clock.install()
    discover(page, url)
    connect(page)
    show_installation_sources(page)
    original = page.request.get(
        url + "/v1/prices", headers={"Authorization": "Bearer " + TOKEN}
    ).json()
    pending = []

    def hold_first_read(route):
        """Delay one old snapshot while later reads use the actual committed API state."""
        if not pending:
            pending.append(route)
        else:
            route.continue_()

    page.route("**/v1/prices", hold_first_read)
    page.get_by_role("button", name="Edit installation", exact=True).click()
    control = page.locator("[data-price-preference]").nth(1)
    control.select_option("jeroen_nl")
    with page.expect_request(lambda request: request.url.endswith("/v1/prices")):
        page.clock.fast_forward(31000)
    page.get_by_role("button", name="Save price sources", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text(
        "Price source preferences saved"
    )
    with page.expect_response(lambda response: response.url.endswith("/v1/prices")) as completed:
        assert len(pending) == 1
        pending[0].fulfill(json=original)
    completed.value.finished()
    page.clock.run_for(50)
    expect(control).to_have_value("jeroen_nl")
    expect(control).to_be_disabled()
    expect(page.locator("#price-source-list li").nth(1)).to_contain_text("Mode: Manual")


@pytest.mark.parametrize("app_site", ["prices"], indirect=True)
def test_price_status_failure_clears_old_active_claims_and_reports_automatic_retry(app_page):
    """A failed status read must not keep stale source rows labelled active. Retry remains visible on desktop/mobile."""
    page, url = app_page
    discover(page, url)
    connect(page)
    expect(page.locator("#price-source-list li")).to_have_count(3)
    expect(page.locator("#price-source-availability summary").first).to_have_text(
        "Forecast availability"
    )
    page.route("**/v1/prices", lambda route: route.abort())
    page.get_by_role("button", name="Home Assistant", exact=True).click()
    page.get_by_role("button", name="Refresh Home Assistant data").click()
    show_installation_sources(page)
    expect(page.locator("#price-market")).to_have_text(
        "Price source status unavailable. Retrying automatically."
    )
    expect(page.locator("#price-source-list li")).to_have_count(0)
    expect(page.locator("#price-source-provenance")).to_be_empty()
    expect(page.locator("#price-published-coverage")).to_have_text(
        "Published Nord Pool coverage unavailable. Retrying automatically."
    )


@pytest.mark.parametrize("app_site", ["prices"], indirect=True)
def test_cancel_price_preference_after_failed_poll_keeps_coverage_unavailable(app_page):
    """Cancel restores saved preferences without reviving coverage after a failed status read."""
    page, url = app_page
    page.clock.install()
    discover(page, url)
    connect(page)
    show_installation_sources(page)
    page.get_by_role("button", name="Edit installation", exact=True).click()
    control = page.locator("[data-price-preference]").nth(1)
    control.select_option("jeroen_nl")
    page.route("**/v1/prices", lambda route: route.abort())
    page.clock.fast_forward(31000)
    coverage = page.locator("#price-published-coverage")
    expect(coverage).to_have_text(
        "Published Nord Pool coverage unavailable. Retrying automatically."
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(control).to_have_value("")
    expect(control).to_be_disabled()
    expect(coverage).to_have_text(
        "Published Nord Pool coverage unavailable. Retrying automatically."
    )
    expect(page.locator("#price-source-list li")).to_have_count(0)
    expect(page.locator("#price-source-provenance")).to_be_empty()
