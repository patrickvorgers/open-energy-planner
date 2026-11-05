"""Check contextual interface wording in the actual responsive application."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from open_energy_planner.storage.adapters.usage import StorageUsage
from tests.frontend.browser.helpers import discover


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
@pytest.mark.parametrize(
    "route,heading",
    [
        ("setup", "Set up Open Energy Planner"),
        ("contract", "Contract settings"),
        ("appliance-settings", "Appliances settings"),
        ("appliance-settings/ev", "Appliances settings"),
        ("appliance-settings/thermal", "Appliances settings"),
        ("data-storage", "Data storage"),
        ("overview", "Planning"),
        ("ev", "EV charging"),
        ("thermal", "Thermal storage"),
    ],
)
def test_owned_screen_copy_uses_context_instead_of_the_product_abbreviation(
    app_page, route, heading, monkeypatch
):
    """Synthetic empty screens identify their purpose without repeating the product abbreviation."""
    page, url = app_page
    if route == "data-storage":
        usage = StorageUsage(
            "sqlite",
            database_bytes=2048,
            disk_total_bytes=100 * 1024**3,
            disk_free_bytes=20 * 1024**3,
        )
        monkeypatch.setattr(
            "open_energy_planner.storage.adapters.usage.storage_usage", lambda _engine: usage
        )
    page.goto(url + "/" + route)
    expect(page.get_by_role("heading", name=heading, exact=True).first).to_be_visible()
    page.wait_for_load_state("networkidle")
    expect(page.locator("body")).not_to_contain_text(re.compile(r"\bOEP\b"))
    if route in {"appliance-settings", "data-storage", "contract"}:
        destination = Path("results/ci")
        destination.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(destination / f"interface-wording-{route}-{page.viewport_size['width']}.png"),
            full_page=True,
        )


@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_bonus_examples_distinguish_app_accounting_from_supplier_terms(app_page):
    """Both supplier examples attribute all-export accounting to the app and retain exclusions."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/contract")
    page.get_by_role("checkbox", name="Include export bonus", exact=True).check()
    page.get_by_role("button", name="Export bonus example", exact=True).click()
    zonneplan = page.get_by_role("tabpanel", name="Zonneplan", exact=True)
    expect(zonneplan).to_be_visible()
    expect(zonneplan.locator("dd").filter(has_text="7500 kWh").locator("p")).to_have_text(
        "This app counts all grid export towards the cap, including unpaid export and battery export."
    )
    expect(zonneplan.locator(":scope > p").last).to_have_text(
        "This app counts all grid export towards the cap, including unpaid hours and battery export. "
        "Source exclusions are unsupported. This example cannot reproduce contract terms that "
        "exclude particular energy sources. Changing terms within the same contract does not reset "
        "used volume."
    )
    destination = Path("results/ci")
    destination.mkdir(parents=True, exist_ok=True)
    zonneplan.locator(":scope > p").last.scroll_into_view_if_needed()
    page.screenshot(
        path=str(destination / f"interface-wording-zonneplan-{page.viewport_size['width']}.png"),
        full_page=True,
    )
    page.get_by_role("tab", name="NextEnergy", exact=True).click()
    nextenergy = page.get_by_role("tabpanel", name="NextEnergy", exact=True)
    expect(nextenergy).to_be_visible()
    expect(nextenergy.locator("dd").filter(has_text="6000 kWh").locator("p")).to_have_text(
        "This app counts all grid export towards the cap. Supplier source exclusions cannot be applied."
    )
    expect(nextenergy.locator(":scope > p").last).to_have_text(
        "NextEnergy excludes stored battery energy from the bonus. This app counts all grid export "
        "towards the cap. Source exclusions cannot be applied. This example is not an exact supplier "
        "preset when those exclusions affect settlement. A changed price rule within the same "
        "contract retains used volume."
    )
    nextenergy.locator(":scope > p").last.scroll_into_view_if_needed()
    page.screenshot(
        path=str(destination / f"interface-wording-nextenergy-{page.viewport_size['width']}.png"),
        full_page=True,
    )
