"""Keep functional page titles concise and preserve contextual help and page actions."""

import re
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START
from tests.frontend.browser.ev_request_fixture import configure_ev
from tests.frontend.browser.helpers import (
    TOKEN,
    connect,
    discover,
    finish,
    grid,
    installation_area,
    open_primary_navigation,
    wait_for_product_reads,
)


def assert_page_heading(surface, title):
    """The visible title group contains one loaded icon and its title without introductory copy."""
    heading = surface.get_by_role("heading", level=1)
    expect(heading).to_have_text(title)
    expect(heading).to_be_visible()
    expect(heading.locator("..")).to_have_text(title)
    expect(heading.locator("img")).to_be_visible()
    assert heading.locator("img").evaluate("image => image.complete && image.naturalWidth > 0")
    expect(surface.locator(".page-header p")).to_have_count(0)


@pytest.mark.parametrize("app_site", ["documentation", "ingress"], indirect=True)
def test_functional_page_headings_keep_context_and_actions(app_page):
    """All direct pages retain readable titles and local safety help on desktop, mobile and ingress."""
    page, url = app_page
    page.clock.set_fixed_time(START)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    captures = Path("results/ci/page-headings")
    captures.mkdir(parents=True, exist_ok=True)
    ingress = "/api/hassio_ingress/" in url
    for route, title in (
        ("/overview#planning", "Planning"),
        ("/overview#savings", "Savings"),
        ("/appliances", "Appliance planning"),
        ("/appliance-cycles", "Appliance cycles"),
        ("/ev", "EV charging"),
        ("/thermal", "Thermal storage"),
        ("/quality", "Forecast quality"),
        ("/planning-performance", "Planning performance"),
        ("/activity", "Activity"),
        ("/contract", "Contract settings"),
        ("/data-storage", "Data storage"),
        ("/appliance-settings", "Appliances settings"),
        ("/appliance-settings/ev", "Appliances settings"),
        ("/appliance-settings/thermal", "Appliances settings"),
        ("/setup", re.compile(r"^(Set up Open Energy Planner|Installation settings)$")),
    ):
        page.goto(url + route)
        token_field = page.get_by_label("Installation API token", exact=True)
        if not ingress:
            token_field.fill(TOKEN)
            page.get_by_role("button", name="Connect", exact=True).click()
        wait_for_product_reads(page)
        assert_page_heading(page, title)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if route == "/overview#planning":
            expect(page.locator("#overview-status")).to_contain_text("Connected")
            page.get_by_role("button", name="Refresh overview", exact=True).click()
            expect(page.locator("#overview-status")).to_contain_text("Connected")
        elif route == "/appliances":
            expect(page.get_by_role("region", name="Cycle planning")).to_contain_text(
                "Home Assistant automations control your equipment."
            )
        elif route == "/ev":
            expect(
                page.locator("section").filter(
                    has=page.get_by_role("heading", name="Charging sessions", exact=True)
                )
            ).to_contain_text("Home Assistant automations control the physical charge points.")
        elif route == "/contract":
            expect(page.locator(".contract-market")).to_contain_text("Bidding area")
        if route in ("/overview#planning", "/overview#savings", "/ev", "/appliances"):
            page.evaluate("document.fonts.ready")
            page.evaluate("window.scrollTo(0, 0)")
            page.locator(".page-header").screenshot(
                path=str(
                    captures
                    / f"{route.strip('/').replace('#', '-')}-{page.viewport_size['width']}-{'ingress' if ingress else 'direct'}.png"
                )
            )
    assert not errors


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_workspace_heading_switches_with_planning_and_savings(app_page):
    """Embedded navigation updates the icon and title while retaining the current planning document."""
    page, url = app_page
    page.goto(url + "/workspace?page=" + quote("/overview#planning", safe=""))
    content = page.frame_locator("#workspace-content")
    assert_page_heading(content, "Planning")
    content.locator("body").evaluate("element => element.dataset.retained = 'yes'")
    open_primary_navigation(page)
    page.get_by_role("region", name="Energy", exact=True).get_by_role(
        "link", name="Savings", exact=True
    ).click()
    assert_page_heading(content, "Savings")
    expect(content.locator(".page-icon")).to_have_attribute("src", re.compile(r"/savings\.svg$"))
    expect(content.locator("body")).to_have_attribute("data-retained", "yes")


def test_installation_heading_preserves_wizard_and_saved_actions(app_page):
    """Removing dynamic introductory copy leaves wizard progress, saving and maintenance editing usable."""
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    discover(page, url)
    connect(page)
    assert_page_heading(page, "Set up Open Energy Planner")
    expect(page.locator("#progress")).to_be_visible()
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    assert_page_heading(page, "Installation settings")
    captures = Path("results/ci/page-headings")
    captures.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(captures / f"installation-{page.viewport_size['width']}.png"))
    installation_area(page, 0, edit=True)
    expect(page.get_by_role("button", name="Save installation", exact=True)).to_be_visible()
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert_page_heading(page, "Installation settings")
    assert not errors


def test_charging_help_stays_with_sessions(app_page):
    """A configured charging page keeps physical-control guidance beside sessions and request actions."""
    page, url = app_page
    configure_ev(page, url)
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Connected")
    expect(page.locator("#vehicle-controls > article").first).to_contain_text(
        "No planned charging request"
    )
    assert_page_heading(page, "EV charging")
    expect(page.locator("#sessions").locator("..")).to_contain_text(
        "Home Assistant automations control the physical charge points."
    )
    captures = Path("results/ci/page-headings")
    captures.mkdir(parents=True, exist_ok=True)
    page.evaluate("document.fonts.ready")
    page.screenshot(path=str(captures / f"charging-{page.viewport_size['width']}.png"))
