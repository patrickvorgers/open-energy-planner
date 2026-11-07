"""Exercise complete chooser publication without replacing drafts, focus or saved identities."""

from copy import deepcopy
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, connect, discover, read_settings
from tests.frontend.browser.test_appliance_settings import add_ev_appliance


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}], indirect=True
)
def test_on_demand_vehicle_sources_retain_inputs_focus_and_cancel(app_page):
    """Late candidate publication adds an unconfigured car while retaining a draft and Cancel semantics."""
    page, url = app_page
    body = discover(page, url)
    candidates = deepcopy(body)
    candidates["entities"].append(
        {
            "entity_id": "sensor.synthetic_zeekr_soc",
            "name": "Battery level",
            "device_name": "Synthetic Zeekr",
            "unit": "%",
            "device_id": "synthetic-zeekr",
            "device_class": "battery",
            "state_class": "measurement",
            "integration_id": "synthetic-zeekr-entry",
            "integration_name": "Synthetic Zeekr",
        }
    )
    headers = {"Authorization": "Bearer " + TOKEN}
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
    capacity = page.get_by_label("Usable battery capacity (kWh)", exact=True)
    capacity.fill("74.5")
    page.get_by_label("Maximum vehicle AC charging power (kW)", exact=True).fill("11")
    capacity.focus()
    expect(page.locator("#status")).to_contain_text("Your edits are retained")
    result = page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": nonce, "page": candidates},
    )
    assert result.ok and result.json()["accepted"]
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(capacity).to_have_value("74.5")
    expect(capacity).to_be_focused()
    page.get_by_role("combobox", name="Home Assistant integration", exact=True).select_option(
        "synthetic-zeekr-entry"
    )
    page.get_by_label("Vehicle state of charge", exact=True).select_option(
        "sensor.synthetic_zeekr_soc"
    )
    expect(page.locator("#dirty")).to_contain_text("Unsaved changes")
    active = page.request.get(url + "/v1/setup", headers=headers).json()["catalogue"]["data"]
    assert all(row["entity_id"] != "sensor.synthetic_zeekr_soc" for row in active["entities"])
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/source-choices-ev-{page.viewport_size['width']}.png", full_page=True
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"] == []


def test_failed_choice_refresh_retains_existing_draft_and_explicit_retry(app_page):
    """A reported HA failure retains numeric edits and permits a fresh user-requested nonce."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        add_ev_appliance(page, "vehicle")
    first = pending.value.json()["id"]
    capacity = page.get_by_label("Usable battery capacity (kWh)", exact=True)
    capacity.fill("62")
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "ev", "id": first, "failed": True},
    ).ok
    expect(page.locator("#status")).to_contain_text("could not be refreshed")
    expect(capacity).to_have_value("62")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as retry:
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True).click()
    assert retry.value.json()["id"] != first
    expect(capacity).to_have_value("62")


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}], indirect=True
)
def test_boiler_source_refresh_preserves_raw_inputs_and_missing_selection(app_page):
    """A late water-heater choice updates options while preserving an unsaved boiler and focus."""
    page, url = app_page
    body = discover(page, url)
    candidates = deepcopy(body)
    candidates["entities"].append(
        {
            "entity_id": "water_heater.synthetic_tank",
            "name": "Synthetic hot-water tank",
            "unit": "°C",
            "device_id": "synthetic-tank",
            "device_class": "temperature",
            "state_class": "",
        }
    )
    headers = {"Authorization": "Bearer " + TOKEN}
    page.goto(url + "/appliance-settings/thermal")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/v1/ha/source-choices/thermal")
        )
    ) as pending:
        page.get_by_role("button", name="Add electric boiler", exact=True).click()
    volume = page.get_by_label("Water volume (litres)", exact=False)
    volume.fill("180")
    volume.focus()
    nonce = pending.value.json()["id"]
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "thermal", "id": nonce, "page": candidates},
    ).ok
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(volume).to_have_value("180")
    expect(volume).to_be_focused()
    page.get_by_label("Measured water temperature", exact=False).select_option(
        "water_heater.synthetic_tank"
    )
    page.get_by_label("Electrical heating power (kW)", exact=False).fill("2")
    page.get_by_label("Minimum comfort temperature (°C)", exact=False).fill("45")
    page.get_by_label("Maximum planning temperature (°C)", exact=False).fill("65")
    page.get_by_label("Tank surroundings temperature (°C)", exact=False).fill("20")
    page.get_by_label("Heat-loss coefficient (W/°C)", exact=False).fill("1.2")
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/source-choices-thermal-{page.viewport_size['width']}.png", full_page=True
    )
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/v1/ha/source-choices/thermal")
        )
    ) as retry:
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True).click()
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "thermal", "id": retry.value.json()["id"], "page": body},
    ).ok
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(page.get_by_label("Measured water temperature", exact=False)).to_have_value(
        "water_heater.synthetic_tank"
    )
    expect(
        page.get_by_text("water_heater.synthetic_tank: Source unavailable", exact=True)
    ).to_have_count(1)
    expect(volume).to_have_value("180")


def test_installation_force_refresh_retains_unsaved_connection_limit(app_page):
    """A manual active-source refresh leaves an open installation draft and saved settings unchanged."""
    page, url = app_page
    body = discover(page, url)
    connect(page)
    maximum = page.get_by_label("Maximum grid import (kW)", exact=True)
    maximum.fill("7.25")
    before = read_settings(page, url)["installation"]
    page.get_by_role("button", name="Home Assistant", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/catalogue/refresh")
        )
    ):
        page.get_by_role("button", name="Refresh Home Assistant data", exact=True).click()
    expect(maximum).to_have_value("7.25")
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": "Bearer " + TOKEN}, data=body
    ).ok
    expect(page.locator("#installation-save-state")).to_contain_text("sources refreshed")
    expect(maximum).to_have_value("7.25")
    assert read_settings(page, url)["installation"] == before
