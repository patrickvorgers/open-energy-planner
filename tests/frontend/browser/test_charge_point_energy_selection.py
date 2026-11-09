"""Select charge points from authoritative Energy meters without device or integration prerequisites."""

from copy import deepcopy
from pathlib import Path

from playwright.sync_api import expect

from tests.app.ev.support.devices import catalogue, settings
from tests.frontend.browser.helpers import TOKEN, discover
from tests.frontend.browser.test_appliance_settings import add_ev_appliance


def prepare_charge_point(page, url):
    """Register synthetic Energy membership and saved cars, leaving the point to the real editor."""
    body = discover(page, url)
    sources = catalogue()
    for source in sources["entities"]:
        source.update(device_id="", state_class="")
    sources["entities"][2]["energy_name"] = "Garage charging"
    sources["suggestions"]["consumers"][0]["power_entity"] = "sensor.charger_power"
    body["entities"].extend(sources["entities"])
    body["suggestions"]["consumers"] = sources["suggestions"]["consumers"]
    body["entities"].extend(
        [
            {
                "entity_id": entity,
                "name": name,
                "device_id": "" if entity == "sensor.second_meter" else "unrelated-device",
                "unit": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
            }
            for entity, name in (
                ("sensor.second_meter", "Second charging meter"),
                ("sensor.not_in_energy", "Unregistered meter"),
            )
        ]
    )
    body["suggestions"]["consumers"].append({"entity_id": "sensor.second_meter", "upstream": None})
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    current = page.request.get(url + "/v1/ev", headers=headers).json()
    configuration = settings()
    configuration["points"] = []
    assert page.request.post(
        url + "/v1/ev/configuration",
        headers=headers,
        data={"configuration": configuration, "expected_version": current["configuration_version"]},
    ).ok
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    return body, headers


def publish_choices(page, url, headers, body, action, scope="ev"):
    """Complete one actual chooser request without replacing authoritative Energy preferences."""
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/v1/ha/source-choices/" + scope)
        )
    ) as pending:
        action()
    nonce = pending.value.json()["id"]
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": scope, "id": nonce, "page": body},
    ).json()["accepted"]
    expect(page.locator("#status")).to_contain_text("sources refreshed")


def test_charge_point_energy_picker_binds_meter_and_power_then_preserves_explicit_edits(app_page):
    """Energy display names select the defined sensor pair before charging details, with safe overrides and Cancel."""
    page, url = app_page
    body, headers = prepare_charge_point(page, url)
    publish_choices(
        page, url, headers, body, lambda: add_ev_appliance(page, "point", "sensor.charger_energy")
    )
    card = page.locator("#points > section:visible")
    assert card.locator("select").first.get_attribute("data-field") == "energy_entity"
    expect(card.get_by_label("Home Assistant device", exact=True)).to_have_count(0)
    expect(card.get_by_label("Home Assistant integration", exact=True)).to_have_count(0)
    meter = page.get_by_label("Charging consumption meter", exact=True)
    assert meter.locator("option").evaluate_all(
        "options => options.map(option => option.value)"
    ) == [
        "",
        "sensor.charger_energy",
        "sensor.second_meter",
    ]
    expect(meter).to_have_value("sensor.charger_energy")
    expect(page.locator("#ev-title")).to_have_text("Garage charging")
    power = page.get_by_label("Measured AC charging power", exact=True)
    connected = page.get_by_label("Vehicle connected", exact=True)
    expect(power).to_have_value("sensor.charger_power")
    expect(connected).to_have_value("")
    connected.select_option("binary_sensor.charger_connected")
    page.get_by_label("Minimum active charging power (kW)", exact=True).fill("1.4")
    page.get_by_label("Maximum charging power (kW)", exact=True).fill("11")
    page.get_by_role("checkbox", name="Car A", exact=True).check()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.evaluate("scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/charge-point-energy-{page.viewport_size['width']}.png", full_page=True
    )
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    saved = page.request.get(url + "/v1/ev", headers=headers).json()["points"]
    assert len(saved) == 1
    assert saved[0]["energy_entity"] == "sensor.charger_energy"
    assert saved[0]["power_entity"] == "sensor.charger_power"
    assert saved[0]["connected_entity"] == "binary_sensor.charger_connected"
    publish_choices(
        page,
        url,
        headers,
        body,
        lambda: page.get_by_role("button", name="Edit settings", exact=True).click(),
    )
    meter.select_option("sensor.second_meter")
    expect(power).to_have_value("")
    power.select_option("sensor.grid")
    meter.select_option("sensor.charger_energy")
    expect(power).to_have_value("sensor.charger_power")
    expect(connected).to_have_value("binary_sensor.charger_connected")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["points"] == saved


def test_charge_point_refresh_updates_energy_membership_without_replacing_draft(app_page):
    """Refreshed active Energy meters replace the list while a removed choice and numeric edits remain explicit."""
    page, url = app_page
    body, headers = prepare_charge_point(page, url)
    publish_choices(
        page, url, headers, body, lambda: add_ev_appliance(page, "point", "sensor.charger_energy")
    )
    meter = page.get_by_label("Charging consumption meter", exact=True)
    meter.select_option("sensor.charger_energy")
    maximum = page.get_by_label("Maximum charging power (kW)", exact=True)
    maximum.fill("7.4")
    active = deepcopy(body)
    active["suggestions"]["consumers"] = [{"entity_id": "sensor.second_meter", "upstream": None}]
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=active).ok
    candidates = deepcopy(body)
    candidates["suggestions"]["consumers"].append(
        {"entity_id": "sensor.not_in_energy", "upstream": None}
    )
    publish_choices(
        page,
        url,
        headers,
        candidates,
        lambda: page.get_by_role(
            "button", name="Refresh Home Assistant sources", exact=True
        ).click(),
    )
    expect(meter).to_have_value("sensor.charger_energy")
    expect(meter.locator('option[value="sensor.charger_energy"]')).to_contain_text("unavailable")
    expect(meter.locator('option[value="sensor.not_in_energy"]')).to_have_count(0)
    expect(maximum).to_have_value("7.4")
    page.get_by_label("Measured AC charging power", exact=True).select_option(
        "sensor.charger_power"
    )
    page.get_by_label("Vehicle connected", exact=True).select_option(
        "binary_sensor.charger_connected"
    )
    page.get_by_label("Minimum active charging power (kW)", exact=True).fill("1.4")
    page.get_by_role("checkbox", name="Car A", exact=True).check()
    with page.expect_response(url + "/v1/ev/configuration") as rejected:
        page.get_by_role("button", name="Save settings", exact=True).click()
    assert rejected.value.status == 400
    expect(page.locator("#status")).to_contain_text("Settings were rejected")
    expect(meter).to_have_value("sensor.charger_energy")
    meter.select_option("sensor.second_meter")
    expect(maximum).to_have_value("7.4")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["points"] == []


def test_charge_point_without_energy_devices_explains_where_to_add_meter(app_page):
    """An empty Energy list gives actionable advice instead of substituting an arbitrary HA sensor."""
    page, url = app_page
    body, headers = prepare_charge_point(page, url)
    body["suggestions"]["consumers"] = []
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    page.get_by_role("button", name="Add vehicle or charge point", exact=True).click()
    kind = page.get_by_role("dialog", name="Add vehicle or charge point", exact=True)
    kind.get_by_role("radio", name="Charge point", exact=False).check()
    kind.get_by_role("button", name="Continue", exact=True).click()
    chooser = page.get_by_role("dialog", name="Add charge point", exact=True)
    expect(chooser.get_by_text("No matching devices", exact=False)).to_be_visible()
    expect(chooser.get_by_text("Add its energy meter", exact=False)).to_be_visible()
    expect(chooser.locator("button.source-choice")).to_have_count(0)
    chooser.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["points"] == []


def test_energy_device_without_power_definition_keeps_manual_override_during_refresh(app_page):
    """A device without a power link stays explicit, while refresh never replaces a chosen override."""
    page, url = app_page
    body, headers = prepare_charge_point(page, url)
    publish_choices(
        page,
        url,
        headers,
        body,
        lambda: add_ev_appliance(page, "point", "sensor.second_meter"),
    )
    power = page.get_by_label("Measured AC charging power", exact=True)
    expect(power).to_have_value("")
    power.select_option("sensor.grid")
    publish_choices(
        page,
        url,
        headers,
        body,
        lambda: page.get_by_role(
            "button", name="Refresh Home Assistant sources", exact=True
        ).click(),
    )
    expect(power).to_have_value("sensor.grid")
    expect(page.get_by_label("Charging consumption meter", exact=True)).to_have_value(
        "sensor.second_meter"
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert page.request.get(url + "/v1/ev", headers=headers).json()["points"] == []
