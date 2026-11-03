"""Import editable HA panel geometry through the real setup API without losing drafts or provenance."""

import re

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    connect,
    discover,
    finish,
    grid,
    installation_area,
    read_settings,
)


def connect_solar_setup(page, url):
    """Open setup through standalone authentication or the already authenticated ingress session."""
    if "/api/hassio_ingress/" not in url:
        connect(page)
        return
    expect(page.locator("#installation-shell")).to_be_visible()
    if page.locator("#installation-wizard-actions").is_visible():
        expect(page.locator("#progress")).to_have_text("Stage 1 of 5")
        page.get_by_role("button", name="Continue", exact=True).click()


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize("failed_write", ["solar", "installation"])
def test_new_prefilled_solar_source_stays_unsaved_until_all_writes_succeed(app_page, failed_write):
    """Desktop/mobile confirm a new Energy source without editing prefilled fields and retain retry after partial writes."""
    page, url = app_page
    body = discover(page, url)
    page.reload()
    connect_solar_setup(page, url)
    grid(page)
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    original = read_settings(page, url)
    assert original["solar"]["sources"] == []
    incoming = next(
        entity for entity in body["entities"] if entity["entity_id"] == "sensor.incoming"
    )
    body["entities"].append({**incoming, "entity_id": "sensor.outgoing", "name": "Grid export"})
    body["suggestions"].update(
        solar=True,
        solar_sources=["sensor.roof"],
        grid_export="sensor.outgoing",
        terms=[
            {"entity_id": "sensor." + name, "sign": sign}
            for name, sign in (("incoming", 1), ("roof", 1), ("outgoing", -1))
        ],
        solar_configurations=[
            {
                "entity_id": "sensor.roof",
                "issue": None,
                "providers": [
                    {
                        "adapter": "forecast_solar",
                        "entry_id": "core",
                        "panels": [
                            {
                                "panel_id": "core.plane",
                                "capacity_w": 1000,
                                "azimuth": 205,
                                "tilt": 35,
                            }
                        ],
                        "inverter_limit_w": 5000,
                    }
                ],
            }
        ],
    )
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    page.reload()
    connect_solar_setup(page, url)
    state = page.locator("#installation-save-state")
    expect(state).to_contain_text("Unsaved changes")
    expect(state).to_contain_text("Review and save new Home Assistant solar sources")
    installation_area(page, 1, edit=True)
    expect(state).to_contain_text("Unsaved changes")
    source = page.locator("#solar-rows > fieldset").first
    expect(source.get_by_label("Inverter AC rating (kW)", exact=True)).to_have_value("5")
    expect(source.get_by_label("Panel capacity (kWp)", exact=True)).to_have_value("1")
    save = page.get_by_role("button", name="Save installation", exact=True)
    expect(save).to_be_enabled()
    if failed_write == "solar":
        mode = "ingress" if "/api/hassio_ingress/" in url else "standalone"
        page.screenshot(
            path=f"results/ci/solar-new-source-{page.viewport_size['width']}-{mode}.png",
            full_page=True,
        )
    pattern = "**/v1/" + failed_write

    def fail_write(route):
        """Reject only the selected write, retaining real reads and all other persistence boundaries."""
        if route.request.method == "PUT":
            route.fulfill(status=503, body="Fixture storage unavailable")
        else:
            route.continue_()

    page.route(pattern, fail_write)
    save.click()
    expect(state).to_contain_text("Save could not be confirmed")
    expect(state).to_contain_text("Unsaved changes")
    expect(save).to_be_enabled()
    partial = read_settings(page, url)
    assert len(partial["solar"]["sources"]) == (0 if failed_write == "solar" else 1)
    page.unroute(pattern, fail_write)
    save.click()
    expect(state).to_contain_text("All changes saved")
    saved = read_settings(page, url)
    assert saved["installation"]["grid_export_entity"] == "sensor.outgoing"
    assert len(saved["solar"]["sources"]) == 1
    assert saved["solar"]["sources"][0]["entity_id"] == "sensor.roof"
    assert saved["solar"]["sources"][0]["panels"][0]["azimuth"] == 205
    page.reload()
    connect_solar_setup(page, url)
    expect(state).to_contain_text("All changes saved")


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize(
    "app_page",
    [
        {"width": 1440, "height": 1000},
        {"width": 768, "height": 1024},
        {"width": 390, "height": 844},
    ],
    indirect=True,
)
def test_solar_import_is_editable_persistent_and_not_overwritten_by_ha_refresh(app_page):
    """Desktop/mobile retain exact independent planes and manual overrides while archiving the original import."""
    page, url = app_page
    page.clock.install()
    body = discover(page, url, devices=True)
    body["suggestions"]["batteries"] = []
    body["suggestions"]["soc"] = []
    body["suggestions"]["terms"] = [
        {"entity_id": "sensor." + name, "sign": sign}
        for name, sign in (("incoming", 1), ("roof", 1), ("shed", 1), ("outgoing", -1))
    ]
    providers = [
        {
            "adapter": "forecast_solar",
            "entry_id": "eleven",
            "panels": [
                {
                    "panel_id": "eleven.plane",
                    "name": "South roof",
                    "capacity_w": 3410,
                    "azimuth": 205,
                    "tilt": 45,
                }
            ],
            "inverter_limit_w": 12000,
        },
        {
            "adapter": "forecast_solar",
            "entry_id": "ten",
            "panels": [
                {
                    "panel_id": "ten.plane",
                    "name": "East roof",
                    "capacity_w": 3100,
                    "azimuth": 125,
                    "tilt": 40,
                }
            ],
            "inverter_limit_w": 12000,
        },
    ]
    body["suggestions"]["solar_configurations"] = [
        {"entity_id": "sensor.roof", "providers": providers, "issue": None},
        {"entity_id": "sensor.shed", "providers": [], "issue": "solar_configuration_not_available"},
    ]
    response = page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    )
    assert response.ok, response.text()
    connect_solar_setup(page, url)
    grid(page)
    page.locator("#installation-solar").select_option("0")
    source = page.locator("#solar-rows > fieldset").first
    source.get_by_label("Forecast source", exact=True).select_option("ha")
    panels = source.locator(".solar-panel")
    expect(panels).to_have_count(2)
    expect(panels.nth(0)).to_contain_text("205°: South-southwest")
    shortcut = panels.nth(0).get_by_label("Direction shortcut", exact=True)
    angle = panels.nth(0).get_by_label("Azimuth (° clockwise from north)", exact=True)
    expect(shortcut).to_have_value("")
    shortcut.select_option("270")
    expect(angle).to_have_value("270")
    expect(panels.nth(0)).to_contain_text("270°: West")
    angle.fill("205")
    expect(shortcut).to_have_value("")
    expect(panels.nth(0).get_by_label("Panel capacity (kWp)", exact=True)).to_have_value("3.41")
    expect(
        panels.nth(0).get_by_label("Azimuth (° clockwise from north)", exact=True)
    ).to_have_value("205")
    expect(panels.nth(1).get_by_label("Tilt (°)", exact=True)).to_have_value("40")
    expect(source.get_by_label("Inverter AC rating (kW)", exact=True)).to_have_value("")
    source.get_by_role("button", name="Add panel group", exact=True).click()
    expect(panels).to_have_count(3)
    panels.nth(2).get_by_role("button", name="Remove panel group", exact=True).click()
    expect(panels).to_have_count(2)
    expect(panels.nth(1)).to_have_attribute("aria-label", "East roof")
    expect(panels.nth(0).get_by_label("Panel group name", exact=True)).to_have_value("South roof")
    panels.nth(0).get_by_label("Panel group name", exact=True).fill("House roof")
    expect(panels.nth(0).get_by_role("heading", name="House roof", exact=True)).to_be_visible()
    panels.nth(0).get_by_label("Panel capacity (kWp)", exact=True).fill("3.2")
    panels.nth(0).get_by_label("Azimuth (° clockwise from north)", exact=True).fill("210")
    source.get_by_label("Inverter AC rating (kW)", exact=True).fill("12")
    providers[0]["panels"][0]["azimuth"] = 230
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    page.clock.fast_forward(31000)
    expect(
        panels.nth(0).get_by_label("Azimuth (° clockwise from north)", exact=True)
    ).to_have_value("210")
    mode = "ingress" if "/api/hassio_ingress/" in url else "standalone"
    page.evaluate("scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/solar-import-{page.viewport_size['width']}-{mode}.png", full_page=True
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#installation-solar").select_option("1")
    manual = page.locator("#solar-rows > fieldset").nth(1)
    expect(manual).to_contain_text("No solar forecast configuration is linked")
    manual.get_by_label("Panel capacity (kWp)", exact=True).fill("1")
    manual.get_by_label("Panel group name", exact=True).fill("Shed roof")
    manual.get_by_label("Direction shortcut", exact=True).select_option("45")
    expect(manual.get_by_label("Azimuth (° clockwise from north)", exact=True)).to_have_value("45")
    manual.get_by_label("Azimuth (° clockwise from north)", exact=True).fill("22.7")
    expect(manual.get_by_label("Direction shortcut", exact=True)).to_have_value("")
    manual.get_by_label("Inverter AC rating (kW)", exact=True).fill("2")
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]
    roof = next(item for item in saved["sources"] if item["entity_id"] == "sensor.roof")
    assert roof["capacity_w"] == 6300
    assert roof["panels"][0]["azimuth"] == 210
    assert roof["ha_imports"][0]["panels"][0]["azimuth"] == 205
    assert roof["forecast_source"] == "ha"
    assert roof["panels"][0]["name"] == "House roof"
    assert roof["ha_imports"][0]["panels"][0]["name"] == "South roof"
    shed = next(item for item in saved["sources"] if item["entity_id"] == "sensor.shed")
    assert shed["panels"][0]["name"] == "Shed roof"
    assert shed["panels"][0]["azimuth"] == 22.7
    page.reload()
    connect_solar_setup(page, url)
    installation_area(page, 1)
    page.locator("#installation-solar").select_option("0")
    summary = page.locator("#installation-summary")
    groups = summary.locator(".solar-panel-summary")
    expect(page.locator("#installation-capacities")).to_contain_text(re.compile(r"7[.,]3 kWp"))
    expect(page.locator("#review-solar")).to_have_text(re.compile(r"2 sources: 7[.,]3 kWp"))
    expect(groups).to_have_count(2)
    expect(groups.nth(0).get_by_role("heading", name="House roof", exact=True)).to_be_visible()
    expect(groups.nth(0)).to_contain_text("3.2")
    expect(groups.nth(0)).to_contain_text("210")
    expect(groups.nth(1).get_by_role("heading", name="East roof", exact=True)).to_be_visible()
    expect(groups.nth(1)).to_contain_text("3.1")
    expect(groups.nth(1)).to_contain_text("125")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=f"results/ci/solar-saved-{page.viewport_size['width']}-{mode}.png", full_page=True
    )
    installation_area(page, 1, edit=True)
    page.locator("#installation-solar").select_option("0")
    source = page.locator("#solar-rows > fieldset").first
    expect(
        source.locator(".solar-panel").first.get_by_label(
            "Azimuth (° clockwise from north)", exact=True
        )
    ).to_have_value("210")
    source.get_by_role("button", name="Use Home Assistant panel settings", exact=True).click()
    expect(
        source.locator(".solar-panel").first.get_by_label("Panel group name", exact=True)
    ).to_have_value("South roof")
    expect(
        source.locator(".solar-panel").first.get_by_label(
            "Azimuth (° clockwise from north)", exact=True
        )
    ).to_have_value("230")
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert read_settings(page, url)["solar"] == saved

    # The next import changes provenance only, with identical visible panel controls.
    providers[0]["panels"][0].update(capacity_w=3200, azimuth=210, name="House roof")
    providers[0]["inverter_limit_w"] = 11000
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    page.reload()
    connect_solar_setup(page, url)
    installation_area(page, 1, edit=True)
    page.locator("#installation-solar").select_option("0")
    source = page.locator("#solar-rows > fieldset").first
    controls = source.locator("input, select")
    before = controls.evaluate_all("controls => controls.map(control => control.value)")
    source.get_by_role("button", name="Use Home Assistant panel settings", exact=True).click()
    assert controls.evaluate_all("controls => controls.map(control => control.value)") == before
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    updated = read_settings(page, url)["solar"]
    updated_roof = next(item for item in updated["sources"] if item["entity_id"] == "sensor.roof")
    assert updated_roof["panels"] == roof["panels"]
    assert updated_roof["ha_imports"] == providers
    assert updated_roof["ha_imports"][0]["inverter_limit_w"] == 11000
    page.reload()
    connect_solar_setup(page, url)
    assert read_settings(page, url)["solar"] == updated
    installation_area(page, 1, edit=True)
    page.locator("#installation-solar").select_option("0")
    page.locator("#solar-rows > fieldset").first.get_by_role(
        "button", name="Use Home Assistant panel settings", exact=True
    ).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    expect(page.get_by_role("button", name="Save installation", exact=True)).to_be_disabled()


@pytest.mark.parametrize(
    "orientation,capacity_w,azimuth_degrees,explicit_panels,panel_name",
    [
        ("south", 1005, [180], False, ""),
        ("east-west", 2010, [90, 270], False, ""),
        ("south", 1005, [180], True, "  Forecast roof  "),
        ("south", 1005, [180], True, "Forecast\r\nroof"),
        ("south", 1005, [180], True, "Forecast\nroof"),
        ("south", 1005, [180], True, "Forecast\rroof"),
    ],
)
def test_unrelated_save_preserves_panel_watts_and_configuration_version(
    app_page, orientation, capacity_w, azimuth_degrees, explicit_panels, panel_name
):
    """Unchanged watt ratings and raw names survive the editor without drift or implicit legacy conversion."""
    page, url = app_page
    body = discover(page, url)
    body["suggestions"].update(
        solar=True,
        solar_sources=["sensor.roof"],
        terms=[
            {"entity_id": "sensor.incoming", "sign": 1},
            {"entity_id": "sensor.roof", "sign": 1},
        ],
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    initial = read_settings(page, url)["solar"]
    response = page.request.put(
        url + "/v1/solar",
        headers=headers,
        data={
            "sources": [
                {
                    "source_id": "roof",
                    "entity_id": "sensor.roof",
                    "inverter_id": "roof",
                    "capacity_w": capacity_w,
                    "orientation": orientation,
                    "tilt": 35,
                    "panels": [
                        {
                            "panel_id": "exact",
                            "name": panel_name,
                            "capacity_w": 1005,
                            "azimuth": 180,
                            "tilt": 35,
                        }
                    ]
                    if explicit_panels
                    else [],
                }
            ],
            "inverters": [{"inverter_id": "roof", "ac_limit_w": 3000}],
            "expected_version": initial["version"] if initial else None,
        },
    )
    assert response.ok, response.text()
    original = read_settings(page, url)["solar"]
    connect_solar_setup(page, url)
    grid(page)
    groups = page.locator("#solar-rows .solar-panel")
    expect(groups).to_have_count(len(azimuth_degrees))
    if explicit_panels:
        expect(groups.first.get_by_label("Panel group name", exact=True)).to_have_value(
            "Forecastroof" if "\n" in panel_name or "\r" in panel_name else panel_name
        )
    for index, azimuth in enumerate(azimuth_degrees):
        expect(groups.nth(index).get_by_label("Panel capacity (kWp)", exact=True)).to_have_value(
            "1.005"
        )
        expect(
            groups.nth(index).get_by_label("Azimuth (° clockwise from north)", exact=True)
        ).to_have_value(str(azimuth))
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    assert read_settings(page, url)["solar"] == original
    if "\n" in panel_name or "\r" in panel_name:
        installation_area(page, 1, edit=True)
        groups.first.get_by_label("Panel group name", exact=True).fill("Forecastroof")
        expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
        page.get_by_role("button", name="Save installation", exact=True).click()
        expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
        assert (
            read_settings(page, url)["solar"]["sources"][0]["panels"][0]["name"] == "Forecastroof"
        )


def test_solar_geometry_keeps_legacy_east_west_and_compass_boundaries(app_page):
    """Compass labels never alter degrees and the legacy east/west split retains two equal planes."""
    page, _url = app_page
    result = page.evaluate("""async () => {
        const {azimuthLabel, editableSolarPanels} = await import('./setup/solar-geometry.js');
        return {
            labels: [0, 205, 360, null].map(azimuthLabel),
            panels: editableSolarPanels({orientation: 'east-west', capacity_w: 3000, tilt: 35}),
        };
    }""")
    assert result["labels"] == [
        "0°: North",
        "205°: South-southwest",
        "360°: North",
        "Not configured",
    ]
    assert result["panels"] == [
        {"panel_id": "0", "name": "", "capacity_w": 1500, "azimuth": 90, "tilt": 35},
        {"panel_id": "1", "name": "", "capacity_w": 1500, "azimuth": 270, "tilt": 35},
    ]


def test_solar_accuracy_comparison_explains_current_source_and_matched_error(app_page):
    """Desktop/mobile show the selected source, common evidence window and per-candidate MAE without economic claims."""
    page, url = app_page
    page.route("**/v1/models", lambda route: route.fulfill(json={"models": [], "issue": None}))
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={
                "days": [],
                "next_before": None,
                "current_models": {
                    "solar": {
                        "configuration": {
                            "sources": [{"source_id": "roof", "entity_id": "sensor.roof"}]
                        },
                        "source_models": {"roof": "ha-production-v1"},
                        "source_names": {"roof": "South roof"},
                        "source_model_labels": {"roof": "Home Assistant solar production forecast"},
                        "calibration_status": {"roof": "waiting_for_accuracy_evidence"},
                        "source_selection": {
                            "roof": {
                                "requested": "automatic",
                                "active": "ha",
                                "complete_days": 3,
                                "required_days": 3,
                                "mae_kwh": {"pvwatts": 0.1, "ha": 0.04, "ml": 0.07},
                                "issue": None,
                            }
                        },
                    }
                },
            }
        ),
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    source = page.locator("#quality-models article").filter(has_text="South roof")
    source.get_by_text("Current model and learning", exact=True).click()
    expect(source).to_contain_text("Forecast source: Automatic: Active: Home Assistant")
    expect(source).to_contain_text("3 of 3 complete days")
    expect(source).to_contain_text("Home Assistant: Mean absolute error: 0.04 kWh per hour")
    expect(source).not_to_contain_text("Simulated baseline cost")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    source.screenshot(path=f"results/ci/solar-accuracy-{page.viewport_size['width']}.png")
