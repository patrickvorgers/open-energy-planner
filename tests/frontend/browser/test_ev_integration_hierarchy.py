"""Integration-first vehicle selection with explicit ambiguity and independent saved identities."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover
from tests.frontend.browser.test_appliance_settings import add_ev_appliance


def vehicle_source(
    entity_id, device_id, *, entry="account-a", name="Battery level", kind="battery"
):
    """Use synthetic account/device metadata with a public integration name independent of the account title."""
    return {
        "entity_id": entity_id,
        "device_id": device_id,
        "device_name": "Zeekr GT" if device_id == "synthetic-zeekr" else "Synthetic second car",
        "name": name,
        "unit": "%",
        "device_class": kind,
        "state_class": "measurement",
        "integration_id": entry,
        "integration_name": "Synthetic account A"
        if entry == "account-a"
        else "Synthetic account B",
        "integration_domain": "zeekr_ev",
        "integration_display_name": "Zeekr EV API Integration",
    }


def prepare_vehicle(page, url, *, extra=()):
    """Install only independently generated source metadata, then open a fresh unsaved vehicle."""
    body = discover(page, url)
    service = vehicle_source(
        "sensor.synthetic_api_health", "synthetic-api", name="API health", kind=""
    )
    service.update(device_name="Zeekr API", device_entry_type="service")
    body["entities"].extend(
        [
            vehicle_source("sensor.synthetic_zeekr_soc", "synthetic-zeekr"),
            vehicle_source(
                "sensor.synthetic_zeekr_humidity",
                "synthetic-zeekr",
                name="Humidity",
                kind="humidity",
            ),
            service,
            *extra,
        ]
    )
    headers = {"Authorization": "Bearer " + TOKEN}
    response = page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body)
    assert response.ok
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        add_ev_appliance(page, "vehicle")
    nonce = pending.value.json()["id"]
    accepted = page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": nonce, "page": body},
    )
    assert accepted.ok and accepted.json()["accepted"]
    expect(
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True)
    ).to_be_enabled()
    return headers


def test_integration_type_precedes_device_and_unique_soc_is_selected(app_page):
    """Choose the public integration name, then automatically bind its sole eligible physical car and SoC."""
    page, url = app_page
    headers = prepare_vehicle(page, url)
    integration = page.get_by_role("combobox", name="Home Assistant integration", exact=True)
    expect(integration.locator("option")).to_contain_text(
        ["Select a source", "Other Home Assistant sources", "Zeekr EV API Integration"]
    )
    integration.select_option("zeekr_ev")
    device = page.get_by_role("combobox", name="Home Assistant device", exact=True)
    soc = page.get_by_label("Vehicle state of charge", exact=True)
    expect(device).to_have_value("synthetic-zeekr")
    expect(device.locator("option")).to_have_text(["Select a source", "Zeekr GT"])
    expect(soc).to_have_value("sensor.synthetic_zeekr_soc")
    expect(soc.locator("option:checked")).to_have_text("Battery level")
    expect(page.locator("#ev-device-select option:checked")).to_have_text("Zeekr GT: Vehicle")
    expect(
        page.get_by_role("combobox", name="Home Assistant integration instance", exact=True)
    ).to_have_count(0)
    order = page.locator("#vehicles select").evaluate_all(
        "controls=>controls.map(control=>control.getAttribute('aria-label'))"
    )
    assert order[:3] == [
        "Home Assistant integration",
        "Home Assistant device",
        "Vehicle state of charge",
    ]
    page.get_by_label("Usable battery capacity (kWh)", exact=True).fill("75")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("11")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/ev-integration-hierarchy-{page.viewport_size['width']}.png",
        full_page=True,
    )
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    saved = page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]
    assert saved["name"] == "Zeekr GT"
    assert saved["soc_entity"] == "sensor.synthetic_zeekr_soc"


def test_new_vehicle_requires_integration_choice_even_if_only_one_type_exists(app_page):
    """A lone group of percentage sources does not silently turn a household battery into a new vehicle."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    add_ev_appliance(page, "vehicle")
    integration = page.get_by_label("Home Assistant integration", exact=True)
    expect(integration.locator("option")).to_have_count(2)
    expect(integration).to_have_value("")
    expect(page.get_by_label("Home Assistant device", exact=True)).to_have_value("")
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value("")


def test_multiple_accounts_and_sensors_require_explicit_choices(app_page):
    """Two accounts stay distinct. Account B's single car is automatic, but its two compatible SoC sensors are not guessed."""
    page, url = app_page
    extra = [
        vehicle_source("sensor.synthetic_second_soc", "second", entry="account-b"),
        vehicle_source(
            "sensor.synthetic_second_alternative",
            "second",
            entry="account-b",
            name="Battery level",
        ),
    ]
    headers = prepare_vehicle(page, url, extra=extra)
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "zeekr_ev"
    )
    instance = page.get_by_role("combobox", name="Home Assistant integration instance", exact=True)
    expect(instance).to_have_value("")
    expect(page.get_by_label("Home Assistant device", exact=True)).to_have_value("")
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value("")
    instance.select_option("account-b")
    expect(page.get_by_label("Home Assistant device", exact=True)).to_have_value("second")
    soc = page.get_by_label("Vehicle state of charge", exact=True)
    expect(soc).to_have_value("")
    expect(soc.locator("option")).to_have_count(3)
    expect(soc.locator("option")).to_have_text(
        [
            "Select a source",
            "Battery level (sensor.synthetic_second_soc)",
            "Battery level (sensor.synthetic_second_alternative)",
        ]
    )
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/ev-integration-accounts-{page.viewport_size['width']}.png", full_page=True
    )
    soc.select_option("sensor.synthetic_second_alternative")
    page.get_by_label("Usable battery capacity (kWh)", exact=True).fill("72.5")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("11")
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    saved = page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]
    assert saved["soc_entity"] == "sensor.synthetic_second_alternative"
    page.get_by_role("button", name="Edit settings", exact=True).click()
    expect(soc).to_have_value("sensor.synthetic_second_alternative")
    page.get_by_role(
        "combobox", name="Home Assistant integration instance", exact=True
    ).select_option("account-a")
    expect(soc).to_have_value("sensor.synthetic_zeekr_soc")
    expect(page.get_by_label("Usable battery capacity (kWh)", exact=True)).to_have_value("72.5")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0] == saved


def test_two_devices_under_one_account_require_a_device_choice(app_page):
    """One account with two cars does not select either device or sensor, even when each car has one SoC source."""
    page, url = app_page
    prepare_vehicle(page, url, extra=[vehicle_source("sensor.synthetic_second_soc", "second")])
    page.get_by_label("Home Assistant integration", exact=True).select_option("zeekr_ev")
    device = page.get_by_label("Home Assistant device", exact=True)
    expect(device).to_have_value("")
    expect(device.locator("option")).to_have_text(
        ["Select a source", "Synthetic second car", "Zeekr GT"]
    )
    soc = page.get_by_label("Vehicle state of charge", exact=True)
    expect(soc).to_have_value("")
    expect(soc.locator("option")).to_have_count(1)
    device.select_option("second")
    expect(soc).to_have_value("sensor.synthetic_second_soc")
    device.select_option("synthetic-zeekr")
    expect(soc).to_have_value("sensor.synthetic_zeekr_soc")


def test_selected_manual_sensor_survives_matching_device_and_refresh(app_page):
    """An explicit second sensor remains selected after matching-device selection and metadata refresh, with numeric drafts intact."""
    page, url = app_page
    headers = prepare_vehicle(
        page,
        url,
        extra=[
            vehicle_source(
                "sensor.synthetic_alternative_soc",
                "synthetic-zeekr",
                name="Alternative battery level",
            )
        ],
    )
    page.get_by_label("Home Assistant integration", exact=True).select_option("zeekr_ev")
    soc = page.get_by_label("Vehicle state of charge", exact=True)
    expect(soc).to_have_value("")
    soc.select_option("sensor.synthetic_alternative_soc")
    page.get_by_label("Usable battery capacity (kWh)", exact=True).fill("78.5")
    page.get_by_label("Home Assistant device", exact=True).select_option("synthetic-zeekr")
    expect(soc).to_have_value("sensor.synthetic_alternative_soc")
    candidates = page.request.get(url + "/v1/setup", headers=headers).json()["catalogue"]["data"]
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True).click()
    accepted = page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": pending.value.json()["id"], "page": candidates},
    )
    assert accepted.ok and accepted.json()["accepted"]
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(soc).to_have_value("sensor.synthetic_alternative_soc")
    expect(page.get_by_label("Usable battery capacity (kWh)", exact=True)).to_have_value("78.5")


@pytest.mark.parametrize("removed_level", ["instance", "device", "type"])
def test_missing_parent_remains_visible_without_rebinding_after_refresh(app_page, removed_level):
    """A disappearing account, device or integration stays explicit until the user chooses a replacement."""
    page, url = app_page
    replacement = vehicle_source("sensor.synthetic_second_soc", "second", entry="account-b")
    if removed_level == "device":
        replacement.update(integration_id="account-a", integration_name="Synthetic account A")
    elif removed_level == "type":
        replacement.update(
            integration_domain="replacement_ev", integration_display_name="Replacement EV"
        )
    headers = prepare_vehicle(page, url, extra=[replacement])
    page.get_by_label("Home Assistant integration", exact=True).select_option("zeekr_ev")
    if removed_level == "instance":
        page.get_by_label("Home Assistant integration instance", exact=True).select_option(
            "account-a"
        )
    elif removed_level == "device":
        page.get_by_label("Home Assistant device", exact=True).select_option("synthetic-zeekr")
    soc = page.get_by_label("Vehicle state of charge", exact=True)
    expect(soc).to_have_value("sensor.synthetic_zeekr_soc")
    page.get_by_label("Usable battery capacity (kWh)", exact=True).fill("78.5")
    catalogue = page.request.get(url + "/v1/setup", headers=headers).json()["catalogue"]["data"]
    catalogue["entities"] = [
        entity for entity in catalogue["entities"] if entity.get("device_id") != "synthetic-zeekr"
    ]
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=catalogue).ok
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True).click()
    accepted = page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": pending.value.json()["id"], "page": catalogue},
    )
    assert accepted.ok and accepted.json()["accepted"]
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(soc).to_have_value("sensor.synthetic_zeekr_soc")
    expect(page.get_by_label("Usable battery capacity (kWh)", exact=True)).to_have_value("78.5")
    if removed_level == "instance":
        parent = page.get_by_label("Home Assistant integration instance", exact=True)
        expect(parent).to_have_value("account-a")
        expect(parent.locator("option:checked")).to_contain_text("unavailable")
        parent.select_option("account-b")
    elif removed_level == "device":
        parent = page.get_by_label("Home Assistant device", exact=True)
        expect(parent).to_have_value("synthetic-zeekr")
        expect(parent.locator("option:checked")).to_contain_text("unavailable")
        parent.select_option("second")
    else:
        parent = page.get_by_label("Home Assistant integration", exact=True)
        expect(parent).to_have_value("zeekr_ev")
        expect(parent.locator("option:checked")).to_contain_text("unavailable")
        parent.select_option("replacement_ev")
    expect(soc).to_have_value("sensor.synthetic_second_soc")
    expect(page.get_by_label("Usable battery capacity (kWh)", exact=True)).to_have_value("78.5")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"] == []


def test_equal_device_names_keep_distinct_explicit_identities(app_page):
    """Duplicate car labels keep group-local numbers and explicit identities when an unrelated car disappears."""
    page, url = app_page
    second = vehicle_source("sensor.synthetic_second_soc", "second")
    second["device_name"] = "Zeekr GT"
    unrelated = vehicle_source("sensor.synthetic_alpha_soc", "alpha")
    unrelated["device_name"] = "Alpha"
    headers = prepare_vehicle(page, url, extra=[second, unrelated])
    page.get_by_label("Home Assistant integration", exact=True).select_option("zeekr_ev")
    device = page.get_by_label("Home Assistant device", exact=True)
    expect(device).to_have_value("")
    expect(device.locator("option")).to_have_text(
        ["Select a source", "Alpha", "Zeekr GT: 1", "Zeekr GT: 2"]
    )
    device.select_option("second")
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value(
        "sensor.synthetic_second_soc"
    )
    catalogue = page.request.get(url + "/v1/setup", headers=headers).json()["catalogue"]["data"]
    catalogue["entities"] = [
        entity for entity in catalogue["entities"] if entity.get("device_id") != "alpha"
    ]
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=catalogue).ok
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True).click()
    accepted = page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": pending.value.json()["id"], "page": catalogue},
    )
    assert accepted.ok and accepted.json()["accepted"]
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(device.locator("option")).to_have_text(["Select a source", "Zeekr GT: 1", "Zeekr GT: 2"])
    expect(device).to_have_value("second")
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value(
        "sensor.synthetic_second_soc"
    )
    device.select_option("synthetic-zeekr")
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value(
        "sensor.synthetic_zeekr_soc"
    )


def test_helper_without_device_retains_its_own_integration_and_sensor(app_page):
    """An unregistered-device helper remains usable under Template without being confused with a physical vehicle."""
    page, url = app_page
    helper = vehicle_source("sensor.synthetic_helper_soc", "")
    helper.update(
        device_name="",
        integration_id="helper-entry",
        integration_name="Synthetic helper",
        integration_domain="template",
        integration_display_name="Template",
    )
    headers = prepare_vehicle(page, url, extra=[helper])
    page.get_by_label("Home Assistant integration", exact=True).select_option("template")
    device = page.get_by_label("Home Assistant device", exact=True)
    expect(device).to_have_value("sources-without-device")
    expect(device.locator("option")).to_have_text(
        ["Select a source", "Sources without a Home Assistant device"]
    )
    expect(page.get_by_label("Vehicle state of charge", exact=True)).to_have_value(
        "sensor.synthetic_helper_soc"
    )
    page.get_by_label("Usable battery capacity (kWh)", exact=True).fill("50")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("7.4")
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    saved = page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]
    assert saved["soc_entity"] == "sensor.synthetic_helper_soc"
