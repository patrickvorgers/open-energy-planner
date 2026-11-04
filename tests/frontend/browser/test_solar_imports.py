"""Follow HA panel geometry through real setup without losing independent manual settings or drafts."""

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
    expect(source.get_by_label("Inverter AC rating (kW)", exact=True)).to_have_value("")
    source.get_by_label("Inverter AC rating (kW)", exact=True).fill("3")
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
def test_solar_panels_follow_ha_without_losing_physical_drafts(app_page):
    """Desktop, tablet and ingress retain converter drafts while HA owns linked geometry through outages and relinks."""
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
            "entry_id": "synthetic-roof",
            "inverter_limit_w": 9900,
            "panels": [
                {
                    "panel_id": "east",
                    "name": "East roof",
                    "capacity_w": 1500,
                    "azimuth": 100,
                    "tilt": 25,
                },
                {
                    "panel_id": "west",
                    "name": "West roof",
                    "capacity_w": 2200,
                    "azimuth": 280,
                    "tilt": 40,
                },
            ],
        }
    ]
    linked = {"entity_id": "sensor.roof", "providers": providers, "issue": None}
    body["suggestions"]["solar_configurations"] = [
        linked,
        {"entity_id": "sensor.shed", "providers": [], "issue": "solar_configuration_not_available"},
    ]

    def refresh():
        """Send real HA metadata, then let the normal read-only polling consume it."""
        response = page.request.post(
            url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
        )
        assert response.ok, response.text()
        page.clock.fast_forward(31000)

    refresh()
    connect_solar_setup(page, url)
    grid(page)
    page.locator("#installation-solar").select_option("0")
    source = page.locator("#solar-rows > fieldset").first
    panels = source.locator(".solar-panel")
    angle = panels.first.get_by_label("Azimuth (° clockwise from north)", exact=True)
    expect(panels).to_have_count(2)
    expect(angle).to_have_value("100")
    expect(angle).to_be_disabled()
    expect(source.get_by_role("button", name="Add panel group", exact=True)).to_be_disabled()
    expect(
        panels.first.get_by_role("button", name="Remove panel group", exact=True)
    ).to_be_disabled()
    expect(source).to_contain_text("Managed by Home Assistant")
    rating = source.get_by_label("Inverter AC rating (kW)", exact=True)
    expect(rating).to_have_value("")
    expect(rating).to_be_enabled()
    rating.fill("2.9")
    page.locator("#installation-solar").select_option("1")
    manual = page.locator("#solar-rows > fieldset").nth(1)
    expect(manual).to_contain_text("Manual panel settings")
    manual.get_by_label("Panel capacity (kWp)", exact=True).fill("1")
    manual.get_by_label("Panel group name", exact=True).fill("Garden roof")
    manual.get_by_label("Direction shortcut", exact=True).select_option("45")
    expect(manual.get_by_label("Azimuth (° clockwise from north)", exact=True)).to_have_value("45")
    manual.get_by_label("Azimuth (° clockwise from north)", exact=True).fill("22.7")
    expect(manual.get_by_label("Direction shortcut", exact=True)).to_have_value("")
    manual.get_by_label("Inverter AC rating (kW)", exact=True).fill("2")
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]
    assert saved["sources"][0]["capacity_w"] == 3700
    assert saved["sources"][0]["ha_imports"] == providers
    assert saved["sources"][1]["panels"][0]["azimuth"] == 22.7
    installation_area(page, 1)
    page.locator("#installation-solar").select_option("0")
    providers[0]["panels"][0]["azimuth"] = 115
    refresh()
    expect(page.locator("#installation-summary")).to_contain_text("115")
    installation_area(page, 1, edit=True)
    expect(angle).to_have_value("115")
    expect(angle).to_be_disabled()
    rating.fill("3.1")
    providers[0]["panels"][0]["azimuth"] = 120
    refresh()
    expect(rating).to_have_value("3.1")
    expect(angle).to_have_value("115")
    expect(source).to_contain_text("Your draft is retained")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("Reload before saving")
    expect(rating).to_have_value("3.1")
    page.get_by_role("button", name="Cancel", exact=True).click()
    installation_area(page, 1, edit=True)
    expect(angle).to_have_value("120")
    expect(rating).to_have_value("2.9")
    linked.update(issue="solar_configuration_incomplete", providers=[])
    refresh()
    expect(angle).to_have_value("120")
    expect(angle).to_be_disabled()
    expect(source).to_contain_text("Last accepted settings are retained")
    mode = "ingress" if "/api/hassio_ingress/" in url else "standalone"
    page.screenshot(
        path=f"results/ci/solar-follow-outage-{page.viewport_size['width']}-{mode}.png",
        full_page=True,
    )
    linked["issue"] = "solar_configuration_not_available"
    refresh()
    expect(angle).to_be_enabled()
    expect(angle).to_have_value("120")
    angle.fill("135")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("Reload before saving")
    expect(angle).to_have_value("135")
    page.get_by_role("button", name="Cancel", exact=True).click()
    installation_area(page, 1, edit=True)
    angle.fill("140")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    linked.update(issue=None, providers=providers)
    providers[0]["panels"][0]["azimuth"] = 85
    refresh()
    installation_area(page, 1, edit=True)
    expect(angle).to_have_value("85")
    expect(angle).to_be_disabled()
    expect(rating).to_have_value("2.9")
    assert read_settings(page, url)["solar"]["inverters"] == saved["inverters"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=f"results/ci/solar-follow-linked-{page.viewport_size['width']}-{mode}.png",
        full_page=True,
    )


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize("recovered_link", ["linked", "removed"])
def test_first_unreadable_link_has_no_assumed_panel_geometry(app_page, recovered_link):
    """Desktop/mobile keep first-link geometry blank until recovery, preserving physical drafts through updates and save."""
    page, url = app_page
    page.clock.install()
    body = discover(page, url)
    body["suggestions"].update(
        solar=True,
        solar_sources=["sensor.roof"],
        terms=[
            {"entity_id": "sensor.incoming", "sign": 1},
            {"entity_id": "sensor.roof", "sign": 1},
        ],
        solar_configurations=[
            {"entity_id": "sensor.roof", "providers": [], "issue": "solar_configuration_incomplete"}
        ],
    )
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    page.reload()
    connect_solar_setup(page, url)
    grid(page)
    source = page.locator("#solar-rows > fieldset").first
    for label in ("Panel capacity (kWp)", "Azimuth (° clockwise from north)", "Tilt (°)"):
        expect(source.get_by_label(label, exact=True)).to_have_value("")
        expect(source.get_by_label(label, exact=True)).to_be_disabled()
    expect(source).to_contain_text("No panel geometry has been assumed")
    expect(source.get_by_label("Inverter AC rating (kW)", exact=True)).to_be_enabled()
    mode = "ingress" if "/api/hassio_ingress/" in url else "standalone"
    page.screenshot(
        path=f"results/ci/solar-follow-incomplete-{page.viewport_size['width']}-{mode}.png",
        full_page=True,
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    rating = source.get_by_label("Inverter AC rating (kW)", exact=True)
    rating.fill("2.4")
    proposal = body["suggestions"]["solar_configurations"][0]
    proposal.update(
        issue=None,
        providers=[
            {
                "adapter": "forecast_solar",
                "entry_id": "synthetic-recovery",
                "panels": [
                    {"panel_id": "morning", "capacity_w": 1850, "azimuth": 82, "tilt": 22},
                    {"panel_id": "evening", "capacity_w": 950, "azimuth": 277, "tilt": 37},
                ],
            }
        ],
    )
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    page.clock.fast_forward(31000)
    panels = source.locator(".solar-panel")
    expect(panels).to_have_count(2)
    angle = panels.first.get_by_label("Azimuth (° clockwise from north)", exact=True)
    expect(angle).to_have_value("82")
    expect(angle).to_be_disabled()
    expect(rating).to_have_value("2.4")
    assert read_settings(page, url)["solar"] is None
    proposal["providers"][0]["panels"][0]["azimuth"] = 91
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    page.clock.fast_forward(31000)
    expect(angle).to_have_value("91")
    expect(rating).to_have_value("2.4")
    if recovered_link == "removed":
        proposal.update(issue="solar_configuration_not_available", providers=[])
        assert page.request.post(
            url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
        ).ok
        page.clock.fast_forward(31000)
        expect(angle).to_be_enabled()
        angle.fill("123")
        expect(rating).to_have_value("2.4")
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]
    assert saved["sources"][0]["capacity_w"] == 2800
    assert saved["sources"][0]["panels"][0]["azimuth"] == (
        123 if recovered_link == "removed" else 91
    )
    imports = saved["sources"][0]["ha_imports"]
    if recovered_link == "removed":
        assert imports == []
    else:
        assert len(imports) == 1 and imports[0]["entry_id"] == "synthetic-recovery"
        assert [
            (panel["capacity_w"], panel["azimuth"], panel["tilt"]) for panel in imports[0]["panels"]
        ] == [
            (1850, 91, 22),
            (950, 277, 37),
        ]
    assert saved["inverters"][0]["ac_limit_w"] == 2400


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
        solar_configurations=[
            {
                "entity_id": "sensor.roof",
                "providers": [],
                "issue": "solar_configuration_not_available",
            }
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
