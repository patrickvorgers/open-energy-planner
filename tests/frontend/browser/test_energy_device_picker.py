"""Exercise one Energy picker and exclusive appliance assignment through real app APIs."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from tests.app.ev.support.devices import settings
from tests.frontend.browser.helpers import TOKEN
from tests.frontend.browser.test_charge_point_energy_selection import (
    prepare_charge_point,
    publish_choices,
)
from tests.frontend.browser.thermal_fixture import document as thermal_document


def assigned_devices(page, url):
    """Reserve one source per category and leave one Energy-defined meter/power pair available."""
    body, headers = prepare_charge_point(page, url)
    body["entities"].append(
        {
            "entity_id": "sensor.already_appliance",
            "name": "Ordinary appliance",
            "device_id": "",
            "unit": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        }
    )
    next(source for source in body["entities"] if source["entity_id"] == "sensor.not_in_energy")[
        "energy_name"
    ] = "Available energy device"
    body["suggestions"]["consumers"].extend(
        [
            {"entity_id": "sensor.already_appliance", "upstream": None},
            {
                "entity_id": "sensor.not_in_energy",
                "upstream": None,
                "power_entity": "sensor.battery1",
            },
        ]
    )
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    ev = page.request.get(url + "/v1/ev", headers=headers).json()
    assert page.request.post(
        url + "/v1/ev/configuration",
        headers=headers,
        data={"configuration": settings(), "expected_version": ev["configuration_version"]},
    ).ok
    thermal = page.request.get(url + "/v1/thermal", headers=headers).json()
    configuration = thermal_document()["configuration"]
    configuration["stores"][0].update(
        energy_entity="sensor.second_meter",
        power_entity="sensor.grid",
        temperature_entity="sensor.temperature",
    )
    assert page.request.post(
        url + "/v1/thermal/configuration",
        headers=headers,
        data={"configuration": configuration, "expected_version": thermal["configuration_version"]},
    ).ok
    consumers = page.request.get(url + "/v1/consumers", headers=headers).json()
    assert page.request.put(
        url + "/v1/consumers",
        headers=headers,
        data={
            "selected": [
                *consumers["selected"],
                {"entity_id": "sensor.already_appliance", "weather_sensitive": False},
            ],
            "expected_version": consumers["version"],
        },
    ).ok
    return headers


def open_sources(page, category):
    """Open the same searchable Energy dialog through each existing typed Add action."""
    if category == "point":
        page.get_by_role("button", name="Add vehicle or charge point", exact=True).click()
        kind = page.get_by_role("dialog", name="Add vehicle or charge point", exact=True)
        kind.get_by_role("radio", name="Charge point", exact=False).check()
        kind.get_by_role("button", name="Continue", exact=True).click()
        title = "Add charge point"
    else:
        title = "Add appliance" if category == "consumption" else "Add electric boiler"
        page.get_by_role("button", name=title, exact=True).click()
    return page.get_by_role("dialog", name=title, exact=True)


@pytest.mark.parametrize("category", ["consumption", "point", "boiler"])
def test_shared_picker_excludes_every_assigned_category_and_preserves_cancel(app_page, category):
    """All categories share search, disabled assigned sources and Cancel before staging the correct details."""
    page, url = app_page
    headers = assigned_devices(page, url)
    route = {
        "consumption": "/appliance-settings",
        "point": "/appliance-settings/ev",
        "boiler": "/appliance-settings/thermal",
    }[category]
    page.goto(url + route)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    picker = open_sources(page, category)
    expect(picker.get_by_label("Search devices", exact=True)).to_be_focused()
    expect(picker.locator(".source-choice")).to_have_count(1)
    picker.get_by_label("Show", exact=True).select_option("all")
    expect(picker.locator(".source-choice")).to_have_count(4)
    expect(picker.locator(".source-choice:disabled")).to_have_count(3)
    for entity in ("sensor.charger_energy", "sensor.second_meter", "sensor.already_appliance"):
        expect(picker.locator(f'button[title="{entity}"]')).to_be_disabled()
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f"results/ci/energy-picker-{category}-{page.viewport_size['width']}.png")
    picker.get_by_role("button", name="Cancel", exact=True).click()
    expect(picker).not_to_be_visible()
    origin = {
        "consumption": "Add appliance",
        "point": "Add vehicle or charge point",
        "boiler": "Add electric boiler",
    }[category]
    expect(page.get_by_role("button", name=origin, exact=True)).to_be_focused()
    picker = open_sources(page, category)
    picker.get_by_label("Search devices", exact=True).fill("SENSOR.NOT_IN_ENERGY")
    expect(picker.locator(".source-choice")).to_have_count(1)
    picker.locator(".source-choice").click()
    if category == "consumption":
        expect(page.locator("#appliance-title")).to_have_text("Available energy device")
        expect(page.locator("#appliance-detail")).to_contain_text("sensor.battery1")
    else:
        meter = "Charging consumption meter" if category == "point" else "Boiler consumption meter"
        power = "Measured AC charging power" if category == "point" else "Measured heater power"
        editor = page.locator(
            "#points > section:visible" if category == "point" else "#thermal-editor"
        )
        expect(editor.get_by_label(meter, exact=category == "point")).to_have_value(
            "sensor.not_in_energy"
        )
        expect(editor.get_by_label(power, exact=False)).to_have_value("sensor.battery1")
    page.screenshot(
        path=f"results/ci/energy-details-{category}-{page.viewport_size['width']}.png",
        full_page=True,
    )
    page.get_by_role("button", name="Cancel", exact=True).click()
    expected = {"consumption": "#save-state", "point": "#dirty", "boiler": "#thermal-save-state"}[
        category
    ]
    expect(page.locator(expected)).to_contain_text("All changes saved")
    consumers = page.request.get(url + "/v1/consumers", headers=headers).json()
    assert "sensor.not_in_energy" not in {row["entity_id"] for row in consumers["selected"]}


@pytest.mark.parametrize("category", ["ev", "thermal"])
def test_assignment_race_is_rejected_before_mutation_and_existing_self_edits_remain_valid(
    app_page, category
):
    """A source claimed after reading cannot be stolen by either specialized API, while its existing owner stays editable."""
    page, url = app_page
    headers = assigned_devices(page, url)
    before = page.request.get(url + "/v1/" + category, headers=headers).json()
    configuration = settings() if category == "ev" else deepcopy(before["configuration"])
    field = "points" if category == "ev" else "stores"
    own = configuration[field][0]
    own["maximum_w" if category == "ev" else "heater_w"] += 1
    assert page.request.post(
        url + "/v1/" + category + "/configuration",
        headers=headers,
        data={"configuration": configuration, "expected_version": before["configuration_version"]},
    ).ok
    retained = page.request.get(url + "/v1/" + category, headers=headers).json()
    consumers = page.request.get(url + "/v1/consumers", headers=headers).json()
    selection = [
        *consumers["selected"],
        {"entity_id": "sensor.not_in_energy", "weather_sensitive": False},
    ]
    assert page.request.put(
        url + "/v1/consumers",
        headers=headers,
        data={"selected": selection, "expected_version": consumers["version"]},
    ).ok
    own.update(
        id=str(uuid4()), energy_entity="sensor.not_in_energy", power_entity="sensor.battery1"
    )
    rejected = page.request.post(
        url + "/v1/" + category + "/configuration",
        headers=headers,
        data={
            "configuration": configuration,
            "expected_version": retained["configuration_version"],
        },
    )
    assert rejected.status == 400
    assert "already added as an appliance" in rejected.text()
    after = page.request.get(url + "/v1/" + category, headers=headers).json()
    assert after["configuration_version"] == retained["configuration_version"]
    assert (
        after[field] == retained[field]
        if category == "ev"
        else after["configuration"] == retained["configuration"]
    )


def test_generic_routes_cannot_release_or_enable_a_specialized_meter(app_page):
    """Consumer selection cannot drop a specialized forecast and cycle permission cannot claim either typed meter."""
    page, url = app_page
    headers = assigned_devices(page, url)
    before = page.request.get(url + "/v1/consumers", headers=headers).json()
    selected = [row for row in before["selected"] if row["entity_id"] != "sensor.charger_energy"]
    rejected = page.request.put(
        url + "/v1/consumers",
        headers=headers,
        data={"selected": selected, "expected_version": before["version"]},
    )
    assert rejected.status == 400
    assert (
        page.request.get(url + "/v1/consumers", headers=headers).json()["selected"]
        == before["selected"]
    )
    for entity in ("sensor.charger_energy", "sensor.second_meter"):
        rejected = page.request.post(
            url + "/v1/appliances/device",
            headers=headers,
            data={"entity_id": entity, "shiftable": True},
        )
        assert rejected.status == 400
        assert "already added as an appliance" in rejected.text()


def test_boiler_energy_pair_override_refresh_and_cancel_match_charge_point_behavior(app_page):
    """Boiler meter changes apply only explicit power links, preserve overrides on refresh and leave Cancel available."""
    page, url = app_page
    body, headers = prepare_charge_point(page, url)
    before = page.request.get(url + "/v1/thermal", headers=headers).json()
    configuration = thermal_document()["configuration"]
    configuration["stores"][0].update(
        energy_entity="sensor.charger_energy",
        power_entity="sensor.charger_power",
        temperature_entity="sensor.temperature",
    )
    assert page.request.post(
        url + "/v1/thermal/configuration",
        headers=headers,
        data={"configuration": configuration, "expected_version": before["configuration_version"]},
    ).ok
    page.goto(url + "/appliance-settings/thermal")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    publish_choices(
        page,
        url,
        headers,
        body,
        lambda: page.get_by_role("button", name="Edit settings", exact=True).click(),
        scope="thermal",
    )
    meter = page.get_by_label("Boiler consumption meter", exact=False)
    power = page.get_by_label("Measured heater power", exact=False)
    meter.select_option("sensor.second_meter")
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
        scope="thermal",
    )
    expect(power).to_have_value("sensor.grid")
    meter.select_option("sensor.charger_energy")
    expect(power).to_have_value("sensor.charger_power")
    cancel = page.get_by_role("button", name="Cancel", exact=True)
    expect(cancel).to_be_enabled()
    cancel.click()
    after = page.request.get(url + "/v1/thermal", headers=headers).json()
    assert after["configuration"] == configuration


@pytest.mark.parametrize("category", ["point", "boiler"])
@pytest.mark.parametrize("change", ["linked", "unlinked", "removed", "claimed"])
def test_pending_refresh_updates_authoritative_choice_before_typed_draft(
    app_page, category, change
):
    """A refresh completed while the picker is open updates power or rejects a vanished or claimed choice."""
    page, url = app_page
    body, headers = prepare_charge_point(page, url)
    editor = "ev" if category == "point" else "thermal"
    if category == "boiler":
        thermal = page.request.get(url + "/v1/thermal", headers=headers).json()
        configuration = thermal_document()["configuration"]
        configuration["stores"][0].update(
            energy_entity="sensor.second_meter",
            power_entity="sensor.grid",
            temperature_entity="sensor.temperature",
        )
        assert page.request.post(
            url + "/v1/thermal/configuration",
            headers=headers,
            data={
                "configuration": configuration,
                "expected_version": thermal["configuration_version"],
            },
        ).ok
        page.goto(url + "/appliance-settings/thermal")
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    before = page.request.get(url + "/v1/" + editor, headers=headers).json()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/v1/ha/source-choices/" + editor)
        )
    ) as pending:
        page.get_by_role("button", name="Refresh Home Assistant sources", exact=True).click()
    picker = open_sources(page, category)
    chosen = picker.locator('button[title="sensor.charger_energy"]')
    expect(chosen).to_be_enabled()
    changed = deepcopy(body)
    meter = next(
        row
        for row in changed["suggestions"]["consumers"]
        if row["entity_id"] == "sensor.charger_energy"
    )
    if change == "linked":
        meter["power_entity"] = "sensor.grid"
    elif change == "unlinked":
        meter.pop("power_entity")
    elif change == "removed":
        changed["suggestions"]["consumers"].remove(meter)
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=changed).ok
    if change == "claimed":
        consumers = page.request.get(url + "/v1/consumers", headers=headers).json()
        assert page.request.put(
            url + "/v1/consumers",
            headers=headers,
            data={
                "selected": [
                    *consumers["selected"],
                    {"entity_id": "sensor.charger_energy", "weather_sensitive": False},
                ],
                "expected_version": consumers["version"],
            },
        ).ok
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": editor, "id": pending.value.json()["id"], "page": changed},
    ).ok
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(picker).to_be_visible()
    chosen.click()
    power = page.get_by_label(
        "Measured AC charging power" if category == "point" else "Measured heater power",
        exact=False,
    )
    if change in ("linked", "unlinked"):
        expect(
            page.locator("#ev-editor" if category == "point" else "#thermal-editor")
        ).to_be_visible()
        expect(power).to_have_value("sensor.grid" if change == "linked" else "")
    else:
        expect(page.locator("#status")).to_contain_text("no longer available")
        expect(
            page.locator("#ev-editor" if category == "point" else "#thermal-editor")
        ).not_to_be_visible()
        expect(
            page.get_by_role(
                "button",
                name="Add vehicle or charge point"
                if category == "point"
                else "Add electric boiler",
                exact=True,
            )
        ).to_be_enabled()
    after = page.request.get(url + "/v1/" + editor, headers=headers).json()
    assert after["configuration_version"] == before["configuration_version"]
