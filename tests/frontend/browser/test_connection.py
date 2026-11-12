"""Verify ingress token hiding, pairing, overview authentication and wizard save recovery."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.control_layout import assert_field_actions_aligned
from tests.frontend.browser.helpers import (
    TOKEN,
    connect,
    discover,
    grid,
    open_primary_navigation,
    read_settings,
)


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_ingress_never_paints_token_forms_before_scripts_or_session_discovery(app_page):
    """Initial HTML hides token prompts only for trusted ingress, even when scripts cannot run."""
    page, url = app_page
    ingress = "/api/hassio_ingress/" in url
    page.route("**/setup/**/*.js", lambda route: route.abort())
    if not ingress:
        page.set_extra_http_headers({"X-Ingress-Path": "/api/hassio_ingress/forged"})
    for path in (
        "/setup",
        "/overview",
        "/contract",
        "/data-storage",
        "/appliances",
        "/appliance-settings",
        "/appliance-settings/ev",
        "/appliance-settings/thermal",
        "/ev",
        "/thermal",
        "/quality",
    ):
        page.goto(url + path)
        installation_token_input = page.get_by_label("Installation API token", exact=True)
        expect(installation_token_input).to_have_count(1)
        connect_form = page.locator("form").filter(has=installation_token_input)
        if ingress:
            expect(installation_token_input).to_be_hidden()
            expect(connect_form).to_be_hidden()
        else:
            expect(installation_token_input).to_be_visible()
            expect(connect_form).to_be_visible()
    if not ingress:
        assert page.request.get(url + "/v1/setup").status == 401


def test_savings_overview_authentication_empty_coverage_and_mobile_layout(app_page):
    """Desktop/mobile users open the real overview, authenticate, see explicit missing coverage and return to setup."""
    page, url = app_page
    page.goto(url + "/overview")
    expect(page.get_by_role("heading", name="Planning", exact=True)).to_be_visible()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_have_text(
        "Connected: Historical views never change active commands."
    )
    open_primary_navigation(page)
    page.get_by_role("link", name="Savings", exact=True).click()
    expect(page.locator("#savings-total")).to_contain_text("No evaluated savings yet")
    expect(page.locator("#coverage")).to_contain_text("Missing coverage is not zero savings")
    page.evaluate("document.fonts.ready")
    assert_field_actions_aligned(page.locator("#period"), expected_buttons=1, expected_fields=2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    open_primary_navigation(page)
    page.get_by_role("link", name="Installation", exact=True).click()
    expect(page.get_by_role("heading", name="Installation settings", exact=True)).to_be_visible()


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_savings_overview_uses_ingress_without_asking_for_token(app_page):
    """Desktop/mobile ingress users return from the overview to the unconfigured installation wizard.

    The app_site fixture pairs ingress without discovering an installation.
    The app_page fixture supplies isolated desktop and touch-sized mobile contexts.
    Navigation preserves the ingress prefix and never asks for a token.
    """
    page, url = app_page
    page.goto(url + "/overview")
    expect(page.locator("#overview-connect")).to_be_hidden()
    expect(page.locator("#overview-status")).to_have_text(
        "Connected: Historical views never change active commands."
    )
    open_primary_navigation(page)
    page.get_by_role("link", name="Installation", exact=True).click()
    expect(page).to_have_url(url + "/setup")
    expect(
        page.get_by_role("heading", name="Set up Open Energy Planner", exact=True)
    ).to_be_visible()


@pytest.mark.parametrize("app_site", ["pairing"], indirect=True)
def test_app_confirms_discovered_home_assistant_once_without_copying_credentials(app_page):
    """Desktop/mobile users approve a code-matched candidate in the authenticated app. A paired peer then appears without entering a HA token."""
    page, url = app_page
    discover(page, url)
    connect(page)
    page.get_by_role("button", name="Home Assistant", exact=True).click()
    expect(
        page.get_by_text("Browser test Home Assistant. Verification code:", exact=False)
    ).to_be_visible()
    page.get_by_role("button", name="Pair this Home Assistant", exact=True).click()
    expect(page.get_by_text("Connected to Browser test Home Assistant.", exact=True)).to_be_visible(
        timeout=10000
    )
    response = page.request.get(url + "/v1/pairing", headers={"Authorization": "Bearer " + TOKEN})
    assert response.json()["paired"] is True
    assert "token" not in response.json()
    page.get_by_role("button", name="Forget Home Assistant connection", exact=True).click()
    expect(page.get_by_role("button", name="Forget Home Assistant connection")).to_be_hidden(
        timeout=10000
    )
    assert (
        page.request.get(url + "/v1/pairing", headers={"Authorization": "Bearer " + TOKEN}).json()[
            "paired"
        ]
        is False
    )


def test_app_wizard_recovers_authentication_and_partially_saved_configuration(app_page):
    """Wrong tokens are rejected. A final-save network failure retains edits and the confirmed demand/PV revisions for a safe retry."""
    page, url = app_page
    discover(page, url)
    page.get_by_label("Installation API token").fill("wrong-token")
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("token was rejected")
    connect(page)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name="Review your installation", exact=True)).to_be_visible()
    page.route("**/v1/installation", lambda route: route.abort())
    with page.expect_event(
        "requestfailed",
        predicate=lambda request: (
            request.url.endswith("/v1/installation") and request.method == "PUT"
        ),
    ):
        page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Your edits are retained")
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    partial = read_settings(page, url)
    assert partial["demand"]["annual_kwh"] == 3500 and partial["solar"]["version"]
    assert partial["installation"] is None
    page.unroute("**/v1/installation")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Installation saved.")
    saved = read_settings(page, url)
    assert saved["demand"]["version"] == partial["demand"]["version"]
    assert saved["solar"]["version"] == partial["solar"]["version"]


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_ha_ingress_opens_setup_without_token_or_pairing_clicks(app_page):
    """Desktop/mobile ingress uses existing HA trust and renders the detected wizard without another login or approval."""
    page, url = app_page
    discover(page, url, history=True)
    page.get_by_role("button", name="Refresh Home Assistant data").click()
    expect(page.locator("#progress")).to_have_text("Stage 1 of 5")
    expect(page.get_by_label("Installation API token")).to_be_hidden()
    expect(page.get_by_role("button", name="Pair this Home Assistant")).to_be_hidden()
    expect(page.locator("#pairing")).to_be_hidden()
    assert (
        page.locator('meta[name="oep-api-base"]').get_attribute("content")
        == "/api/hassio_ingress/browser_test/"
    )
