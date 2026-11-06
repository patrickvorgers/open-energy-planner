"""Capture useful guide figures from real pages with explicitly synthetic example data."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.documentation_fixture import START, quality_document
from tests.frontend.browser.helpers import (
    TOKEN,
    connect,
    discover,
    edit_price_rule,
    finish,
    installation_area,
    read_settings,
)


def authenticate(page, url, route):
    """Open a real page and authenticate with only the isolated fixture credential."""
    page.goto(url + route)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()


def capture(page, name, selector=None):
    """Retain safe synthetic captures in CI output without changing maintained assets."""
    destination = Path("results/ci/documentation-examples")
    destination.mkdir(parents=True, exist_ok=True)
    page.evaluate("document.fonts.ready")
    page.evaluate("window.scrollTo(0, 0)")
    path = str(destination / f"{name}-{page.viewport_size['width']}.png")
    if selector:
        page.screenshot(path=path, full_page=True, clip=page.locator(selector).bounding_box())
    else:
        page.screenshot(path=path, full_page=True)


@pytest.mark.parametrize("app_site", ["documentation"], indirect=True)
def test_documentation_plan_contains_real_solar_storage_and_daily_variation(app_page):
    """Show a solved and validated 48-hour plan with nonzero supply, use and both battery directions."""
    page, url = app_page
    page.clock.install(time=START)
    authenticate(page, url, "/overview")
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    page.get_by_role("button", name="Full horizon", exact=True).click()
    groups = page.locator("#plan-chart").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.map(series => ({name:series.label, values:series.data.map(point => point.y)}))"
    )
    for label in (
        "Used solar: DC",
        "Grid import: AC",
        "Battery charge: DC",
        "Battery discharge: DC",
    ):
        values = next(series["values"] for series in groups if series["name"] == label)
        assert any(value is not None and abs(value) > 0.01 for value in values), label
    expect(page.locator("#ems-costs > dl dd").first).not_to_have_text("- EUR")
    capture(page, "planning-example", ".optimizer-scenario")
    capture(page, "planning-scenarios", ".scenario-card:has(#self-consumption-chart)")


def test_documentation_installation_shows_saved_limits_and_real_source_relationships(app_page):
    """Save two physical source/battery definitions and show the same persisted 5-kW and reserve values."""
    page, url = app_page
    catalogue = discover(page, url, devices=True)
    names = {
        "sensor.roof": "South roof",
        "sensor.shed": "Garden roof",
        "sensor.discharge0": "Home battery",
        "sensor.discharge1": "Garage battery",
    }
    for entity in catalogue["entities"]:
        if entity["entity_id"] in names:
            entity["energy_name"] = names[entity["entity_id"]]
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=catalogue
    ).ok
    connect(page)
    page.get_by_label("Maximum grid import (kW)", exact=True).fill("5")
    page.get_by_label("Maximum grid export (kW)", exact=True).fill("8")
    page.get_by_label("Annual whole-home consumption (kWh)", exact=True).fill("3500")
    page.get_by_role("button", name="Continue", exact=True).click()
    for index in range(2):
        page.locator("#installation-solar").select_option(str(index))
        source = page.locator("#solar-rows > fieldset").nth(index)
        source.get_by_label("Panel capacity (kWp)", exact=True).fill("3")
        source.get_by_text("Shared inverter settings", exact=True).click()
        source.get_by_label("Inverter ID", exact=True).fill(f"example-inverter-{index}")
        source.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
        if index == 0:
            source.get_by_label("Hybrid inverter with battery connection", exact=True).check()
    page.get_by_role("button", name="Continue", exact=True).click()
    for index in range(2):
        page.locator("#installation-battery").select_option(str(index))
        battery = page.locator("#battery-rows fieldset").nth(index)
        battery.get_by_label("Maximum charge power (kW)", exact=True).fill("2")
        battery.get_by_label("Maximum discharge power (kW)", exact=True).fill("2")
        battery.get_by_label("Minimum state of charge (%)", exact=True).fill("20")
        if index == 0:
            battery.get_by_label("Converter connection", exact=True).select_option(
                "example-inverter-0"
            )
        else:
            battery.get_by_label("Own inverter AC rating (kW)", exact=True).fill("2")
    finish(page)
    settings = read_settings(page, url)
    assert len(settings["installation"]["batteries"]) == 2
    assert settings["installation"]["batteries"][0]["minimum_soc"] == 20
    installation_area(page, 0)
    expect(page.locator("#installation-summary")).to_contain_text("Maximum grid import")
    capture(page, "installation-connection", "#installation-shell")
    installation_area(page, 2)
    capture(page, "installation-battery", "#installation-shell")
    installation_area(page, 4)
    expect(page.locator("#installation-strategy-value dd")).to_have_text("Minimize cost")
    capture(page, "optimization-goal", "#installation-shell")


def test_documentation_contract_explains_actual_formula_preview(app_page):
    """Save dated formulas whose 0.10-EUR market example has 0.24 import and 0.08 export rates."""
    page, url = app_page
    discover(page, url)
    authenticate(page, url, "/contract")
    edit_price_rule(page)
    page.get_by_label("Contract starts", exact=True).fill("2026-01-01")
    page.get_by_label("Price rule starts", exact=True).fill("2026-01-01")
    page.get_by_label("Import formula", exact=True).fill("(market + 0.10) * 1.21")
    page.get_by_label("Export formula", exact=True).fill("market - 0.02")
    page.get_by_role("button", name="Preview rates", exact=True).click()
    expect(page.locator("#contract-preview")).to_contain_text("0.24")
    expect(page.locator("#contract-preview")).to_contain_text("0.08")
    page.get_by_role("button", name="Save contract", exact=True).click()
    expect(page.locator("#contract-status")).to_contain_text("Contract saved")
    capture(page, "contract-rule")


def test_documentation_quality_shows_complete_nonconstant_hourly_comparison(app_page):
    """Render an explicitly synthetic full day whose evening timing errors survive similar daily totals."""
    page, url = app_page
    document = quality_document()
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(json={"days": ["2026-11-04"], "next_before": None}),
    )
    page.route("**/v1/forecast-quality/days/2026-11-04", lambda route: route.fulfill(json=document))
    authenticate(page, url, "/quality")
    expect(page.locator("#quality-status")).to_contain_text("2026-11-04")
    data = page.locator("#quality-chart-0").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.slice(0,2).map(series => series.data.map(point => point.y))"
    )
    assert len(data[0]) == len(data[1]) == 24
    assert max(data[0]) == 1.7 and data[0][17] != data[1][17]
    capture(page, "forecast-quality", "#quality-charts")


def test_documentation_appliance_has_a_nonempty_cycle_and_explicit_readiness(app_page):
    """A synthetic 0.83-kWh, 120-minute profile explains the ready action without issuing physical work."""
    page, url = app_page
    state = {
        "revision": "documentation-example",
        "issue": None,
        "devices": [
            {
                "entity_id": "sensor.example_dishwasher",
                "name": "Example dishwasher",
                "shiftable": True,
                "profile": {
                    "power_w": [1800, 80, 80, 0, 80, 1200, 80, 0],
                    "samples": 3,
                    "evidence": "synthetic",
                },
                "task": None,
            }
        ],
    }
    page.route("**/v1/appliances", lambda route: route.fulfill(json=state))
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={
                "selected": [{"entity_id": "sensor.example_dishwasher"}],
                "options": [
                    {"entity_id": "sensor.example_dishwasher", "name": "Example dishwasher"}
                ],
                "thermal_meters": [],
                "charging_meters": [],
            }
        ),
    )
    authenticate(page, url, "/appliances")
    expect(page.locator("#profile-evidence")).to_contain_text("0.83 kWh")
    expect(page.locator("#profile-evidence")).to_contain_text("120 minutes")
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_be_visible()
    capture(page, "appliance-request")


@pytest.mark.parametrize("app_site", ["maintenance"], indirect=True)
def test_documentation_storage_describes_the_actual_isolated_sqlite_backend(app_page):
    """Show a real local SQLite connection and measured capacity without opening migration consent."""
    page, url = app_page
    authenticate(page, url, "/data-storage")
    expect(page.locator("#connection-summary")).to_have_text("SQLite: Local database")
    expect(page.get_by_role("button", name="Change database", exact=True)).to_be_visible()
    capture(page, "data-storage")
