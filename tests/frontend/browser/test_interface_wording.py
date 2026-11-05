"""Check contextual interface wording in the actual responsive application."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from open_energy_planner.storage.adapters.usage import StorageUsage


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
