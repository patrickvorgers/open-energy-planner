"""Choose actual HA integration sources without guessing vehicle or connection semantics."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover
from tests.frontend.browser.test_appliance_settings import add_ev_appliance


def test_large_catalogue_tail_vehicle_is_selectable_and_saved(app_page):
    """A vehicle beyond 2,000 ordinary sources remains selectable on desktop and mobile."""
    import json

    page, url = app_page
    body = discover(page, url)
    body["entities"].extend(
        {
            "entity_id": f"sensor.synthetic_meter_{index:04}",
            "name": f"Independent synthetic meter {index}",
            "device_name": f"Synthetic meter {index}",
            "unit": "W",
            "device_class": "power",
            "state_class": "measurement",
            "device_id": "",
        }
        for index in range(2000)
    )
    body["entities"].append(
        {
            "entity_id": "sensor.zzz_family_vehicle_soc",
            "name": "Vehicle battery level",
            "device_name": "Family EV",
            "unit": "%",
            "device_class": "battery",
            "state_class": "measurement",
            "device_id": "synthetic-family-ev",
            "integration_id": "synthetic-car-entry",
            "integration_name": "Family EV",
        }
    )
    assert 196608 < len(json.dumps(body).encode()) < 1048576
    headers = {"Authorization": "Bearer " + TOKEN}
    response = page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body)
    assert response.ok
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    add_ev_appliance(page, "vehicle")
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "synthetic-car-entry"
    )
    page.get_by_label("Vehicle state of charge", exact=True).select_option(
        "sensor.zzz_family_vehicle_soc"
    )
    page.get_by_label("Usable battery capacity (kWh)", exact=True).fill("60")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("11")
    page.evaluate("() => window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/large-catalogue-vehicle-{page.viewport_size['width']}.png", full_page=True
    )
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    saved = page.request.get(url + "/v1/ev", headers=headers).json()
    assert saved["vehicles"][0]["name"] == "Family EV"
    assert saved["vehicles"][0]["soc_entity"] == "sensor.zzz_family_vehicle_soc"


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize(
    "app_page",
    [{"width": 1440, "height": 900}, {"width": 768, "height": 1024}, {"width": 390, "height": 844}],
    indirect=True,
)
def test_ev_integration_selection_filters_sources_and_saves_ha_vehicle_name(app_page, request):
    """Desktop/tablet/mobile filter real sources, align controls and preserve independent Energy ownership."""
    page, url = app_page
    site = request.node.callspec.params["app_site"] or "standalone"
    body = discover(page, url)
    for entity_id, name, unit, kind, integration_id, integration_name in (
        ("sensor.car_charge", "Battery level", "%", "", "car", "Family car"),
        ("sensor.car_humidity", "Humidity", "%", "humidity", "car", "Family car"),
        ("binary_sensor.car_door", "Door", "", "door", "car", "Family car"),
        ("sensor.second_car", "Battery level", "%", "battery", "car2", "Second car"),
        ("sensor.charge_meter", "Charging energy", "kWh", "energy", "meter", "Energy meter"),
        ("sensor.charge_power", "Charging power", "W", "power", "charger", "Garage charger"),
        ("binary_sensor.charge_plug", "Cable plugged in", "", "plug", "charger", "Garage charger"),
        ("binary_sensor.charge_fault", "Fault", "", "problem", "charger", "Garage charger"),
        ("sensor.empty_power", "Power", "W", "power", "empty", "No connection sensor"),
        (
            "binary_sensor.helper_plug",
            "Cable connected helper",
            "",
            "plug",
            "helper",
            "Connection helper",
        ),
    ):
        body["entities"].append(
            {
                "entity_id": entity_id,
                "name": name,
                "unit": unit,
                "device_class": kind,
                "state_class": "total_increasing" if kind == "energy" else "",
                "device_id": integration_id,
                "integration_id": integration_id,
                "integration_name": integration_name,
            }
        )
    body["suggestions"]["consumers"] = [{"entity_id": "sensor.charge_meter", "upstream": None}]
    headers = {"Authorization": "Bearer " + TOKEN}
    response = page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body)
    assert response.ok, "Home Assistant catalogue request failed"
    page.goto(url + "/appliance-settings/ev")
    if site == "standalone":
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    add_ev_appliance(page, "vehicle")
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option("car")
    soc = page.get_by_label("Vehicle state of charge", exact=True)
    expect(soc.locator("option")).to_have_count(2)
    expect(soc).to_have_value("")
    assert soc.evaluate("control => control.required && !control.validity.valid")
    soc.select_option("sensor.car_charge")
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option("")
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value(
        "sensor.car_charge"
    )
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "car2"
    )
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value("")
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option("car")
    page.get_by_label("Vehicle state of charge", exact=True).select_option("sensor.car_charge")
    page.get_by_label("Usable battery capacity (kWh)", exact=True).fill("60")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("11")
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    expect(page.locator("#ev-title")).to_have_text("Family car")
    saved = page.request.get(url + "/v1/ev", headers=headers).json()
    assert saved["vehicles"][0]["name"] == "Family car"
    assert saved["vehicles"][0]["soc_entity"] == "sensor.car_charge"
    page.get_by_role("button", name="Edit settings", exact=True).click()
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option("")
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value(
        "sensor.car_charge"
    )
    assert page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"] == saved["vehicles"]
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "car2"
    )
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value("")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"] == saved["vehicles"]
    page.get_by_role("button", name="Edit settings", exact=True).click()
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option("")
    page.get_by_label("Usable battery capacity (kWh)", exact=True).fill("61")
    page.get_by_role("button", name="Save settings", exact=True).click()
    assert page.get_by_role("combobox", name="Home Assistant integration", exact=True).evaluate(
        "control => control.validity.valid"
    ), "Integration filter blocked saving a valid explicit SoC source"
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    saved_vehicle = page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]
    assert saved_vehicle["soc_entity"] == "sensor.car_charge"
    assert saved_vehicle["capacity_kwh"] == 61
    add_ev_appliance(page, "point")
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "empty"
    )
    expect(page.get_by_label("Vehicle connected", exact=True).locator("option")).to_have_count(1)
    expect(
        page.get_by_text("No matching Home Assistant sources were discovered.", exact=False)
    ).to_be_visible()
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "charger"
    )
    page.get_by_label("Charging consumption meter", exact=True).select_option("sensor.charge_meter")
    page.get_by_label("Measured AC charging power", exact=True).select_option("sensor.charge_power")
    connected = page.get_by_label("Vehicle connected", exact=True)
    expect(connected).to_have_value("")
    expect(connected.locator("option")).to_have_count(3)
    page.get_by_label("Find connection sensor", exact=True).fill("no matching name")
    expect(page.get_by_text("No sensors match this search.", exact=True)).to_be_visible()
    page.get_by_label("Find connection sensor", exact=True).fill("plugged")
    expect(connected.locator("option")).to_have_count(2)
    connected.select_option("binary_sensor.charge_plug")
    page.get_by_label("Minimum active charging power (kW)", exact=True).fill("1.4")
    page.get_by_label("Maximum charging power (kW)", exact=True).fill("11")
    page.get_by_role("checkbox", name="Family car", exact=True).check()
    if page.viewport_size["width"] >= 768:
        for left, right in (
            ("Charging consumption meter", "Measured AC charging power"),
            ("Find connection sensor", "Vehicle connected"),
            ("Minimum active charging power (kW)", "Maximum charging power (kW)"),
        ):
            first = page.get_by_label(left, exact=True).bounding_box()
            second = page.get_by_label(right, exact=True).bounding_box()
            assert abs(first["y"] - second["y"]) < 1
            assert abs(first["height"] - second["height"]) < 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    directory = Path("results/ci")
    directory.mkdir(parents=True, exist_ok=True)
    page.evaluate("scrollTo(0, 0)")
    page.screenshot(
        path=str(directory / f"ev-source-selection-{page.viewport_size['width']}-{site}.png"),
        full_page=True,
    )
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    saved = page.request.get(url + "/v1/ev", headers=headers).json()
    assert saved["points"][0]["connected_entity"] == "binary_sensor.charge_plug"
    assert saved["points"][0]["energy_entity"] == "sensor.charge_meter"
    page.get_by_role("button", name="Edit settings", exact=True).click()
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "empty"
    )
    expect(page.get_by_label("Vehicle connected", exact=True)).to_have_value(
        "binary_sensor.charge_plug"
    )
    expect(page.get_by_label("Measured AC charging power", exact=True)).to_have_value(
        "sensor.charge_power"
    )
    page.get_by_label("Measured AC charging power", exact=True).select_option("sensor.empty_power")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["points"] == saved["points"]
    for entity in body["entities"]:
        if entity["entity_id"] == "binary_sensor.charge_plug":
            entity.update(integration_id="car", integration_name="Family car")
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    page.get_by_role("button", name="Check saved settings", exact=True).click()
    page.get_by_role("button", name="Edit settings", exact=True).click()
    expect(page.get_by_label("Vehicle connected", exact=True)).to_have_value(
        "binary_sensor.charge_plug"
    )
    page.get_by_label("Find connection sensor", exact=True).fill("unrelated text")
    expect(page.get_by_label("Vehicle connected", exact=True)).to_have_value(
        "binary_sensor.charge_plug"
    )
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "helper"
    )
    page.get_by_label("Vehicle connected", exact=True).select_option("binary_sensor.helper_plug")
    expect(page.get_by_label("Measured AC charging power", exact=True)).to_have_value(
        "sensor.charge_power"
    )
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "empty"
    )
    page.get_by_label("Measured AC charging power", exact=True).select_option("sensor.empty_power")
    expect(page.get_by_label("Vehicle connected", exact=True)).to_have_value(
        "binary_sensor.helper_plug"
    )
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    saved_point = page.request.get(url + "/v1/ev", headers=headers).json()["points"][0]
    assert saved_point["power_entity"] == "sensor.empty_power"
    assert saved_point["connected_entity"] == "binary_sensor.helper_plug"
    assert saved_point["energy_entity"] == "sensor.charge_meter"
    page.get_by_role("button", name="Edit settings", exact=True).click()
    expect(page.get_by_label("Vehicle connected", exact=True)).to_have_value(
        "binary_sensor.helper_plug"
    )
