"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

import json
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.control_layout import assert_field_actions_aligned
from tests.frontend.browser.helpers import TOKEN, assert_floating_save_bar, discover


@pytest.mark.parametrize(
    "app_page",
    [{"width": 1280, "height": 900}, {"width": 768, "height": 1024}, {"width": 390, "height": 844}],
    indirect=True,
)
def test_historical_learning_progress_preserves_drafts_and_concurrent_write_guard(app_page):
    """Real source-bound progress refreshes on desktop/mobile without changing drafts or hiding a stale save."""
    page, url = app_page
    body = discover(page, url)
    entity, power = "sensor.washer_energy", "sensor.washer_power"
    for identity, unit, kind, state_class in (
        (entity, "kWh", "energy", "total_increasing"),
        (power, "W", "power", "measurement"),
    ):
        body["entities"].append(
            {
                "entity_id": identity,
                "name": "Washer",
                "unit": unit,
                "device_class": kind,
                "state_class": state_class,
                "device_id": "",
            }
        )
    body["suggestions"]["consumers"] = [
        {"entity_id": entity, "upstream": None, "power_entity": power}
    ]
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    consumers = page.request.get(url + "/v1/consumers", headers=headers).json()
    assert page.request.put(
        url + "/v1/consumers",
        headers=headers,
        data={
            "expected_version": consumers["version"],
            "selected": [{"entity_id": entity, "weather_sensitive": False}],
        },
    ).ok
    start = (datetime.now(UTC) - timedelta(days=500)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(days=14)
    pair = {"entity_id": entity, "power_entity": power}
    response = page.request.post(
        url + "/v1/appliances/history_scan",
        headers=headers,
        data={**pair, "phase": "processing", "start": start.isoformat(), "end": end.isoformat()},
    )
    assert response.ok, response.text()
    pending = response.json()["appliances"]["devices"][0]["history_request"]
    page.clock.install()
    page.goto(url + "/appliances")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    history = page.locator("#profile-history")
    expect(history).to_contain_text("Processing hourly history")
    expect(history).to_contain_text("0.0%")
    expect(page.locator("#cycle-charts")).to_be_hidden()
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_have_count(0)
    assert page.request.post(
        url + "/v1/appliances/hourly_history",
        headers=headers,
        data={
            **pair,
            **pending,
            "synthetic": True,
            "rows": [
                {
                    "at": (start + timedelta(hours=index)).isoformat(),
                    "power_w": power_w,
                    "kwh": energy_kwh,
                }
                for index, (power_w, energy_kwh) in enumerate(
                    [(0, 0), (0, 0), (500, 0.5), (0, 0.5), (0, 0.5), (1000, 1.5), (0, 1.5)]
                )
            ],
        },
    ).ok
    page.clock.fast_forward(10000)
    expect(history).to_contain_text("50.0%")
    expect(history).to_contain_text("Hourly evidence: Simulated data")
    expect(history).to_contain_text("Hourly estimated cycles2")
    expect(page.get_by_role("progressbar", name="Hourly history progress")).to_have_attribute(
        "value", "604800000"
    )
    directory = Path("results/ci")
    directory.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    page.screenshot(
        path=str(directory / f"historical-learning-progress-{width}.png"), full_page=True
    )
    pending = page.request.get(url + "/v1/appliances", headers=headers).json()["devices"][0][
        "history_request"
    ]
    page.goto(url + "/appliance-settings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    evidence = page.locator("#appliance-evidence")
    expect(evidence).to_contain_text("No profile available yet")
    expect(page.get_by_role("heading", name="Planning readiness", exact=True)).to_be_visible()
    expect(page.get_by_role("progressbar", name="Hourly history progress")).to_have_count(0)
    page.get_by_role("button", name="Edit appliance settings", exact=True).click()
    duration = page.get_by_label("Duration (minutes)", exact=True)
    energy = page.get_by_label("Energy consumption (kWh)", exact=True)
    duration.fill("90")
    energy.fill("0.6")
    duration.focus()
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    assert page.request.post(
        url + "/v1/appliances/hourly_history",
        headers=headers,
        data={**pair, **pending, "synthetic": True, "rows": []},
    ).ok
    page.clock.fast_forward(10000)
    expect(evidence).to_contain_text("No profile available yet")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    page.clock.fast_forward(10000)
    expect(evidence).to_contain_text("Profile: Historical estimate: Simulated data")
    expect(duration).to_have_value("90")
    expect(energy).to_have_value("0.6")
    expect(duration).to_be_focused()
    page.get_by_role("button", name="Save appliance settings", exact=True).click()
    expect(page.locator("#save-state")).to_contain_text("Appliance settings changed")
    expect(duration).to_have_value("90")
    expect(energy).to_have_value("0.6")
    page.route(
        "**/v1/appliances", lambda route: route.fulfill(status=503, body="Fixture evidence outage")
    )
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    page.clock.fast_forward(10000)
    expect(evidence).not_to_contain_text("Learning evidence refresh unavailable")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    page.clock.fast_forward(10000)
    expect(evidence).to_contain_text("Learning evidence refresh unavailable")
    expect(evidence).to_contain_text("Historical estimate")
    page.unroute("**/v1/appliances")
    page.goto(url + "/appliances")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    history = page.locator("#profile-history")
    expect(page.locator("#profile-evidence")).to_contain_text("Profile: Historical estimate")
    expect(history).to_contain_text("Historical scan complete")
    expect(history).to_contain_text("Provisional planning profile")
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_have_count(0)
    page.evaluate(
        "document.querySelectorAll('canvas').forEach(canvas => { const chart = Chart.getChart(canvas); chart?.stop(); chart?.update('none'); })"
    )
    page.screenshot(
        path=str(directory / f"historical-learning-provisional-{width}.png"), full_page=True
    )
    chart_id = page.locator("#cycle-chart").evaluate("canvas => Chart.getChart(canvas).id")
    page.locator("#profile-device").focus()
    body["timezone"] = "Pacific/Auckland"
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=body).ok
    with page.expect_response(url + "/setup/session") as metadata:
        page.clock.fast_forward(20000)
    assert metadata.value.json()["timezone"] == "Pacific/Auckland"
    page.clock.fast_forward(10000)
    expected_start = page.evaluate(
        "value => new Date(value).toLocaleString(undefined, {timeZone: 'Pacific/Auckland', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit'})",
        start.isoformat(),
    )
    expect(history).to_contain_text(expected_start)
    expect(page.locator("#profile-device")).to_be_focused()
    assert page.locator("#cycle-chart").evaluate("canvas => Chart.getChart(canvas).id") == chart_id
    page.route(
        "**/v1/appliances", lambda route: route.fulfill(status=503, body="Fixture evidence outage")
    )
    page.clock.fast_forward(10000)
    expect(page.locator("#status")).to_contain_text("Learning evidence refresh unavailable")
    expect(history).to_contain_text("Historical scan complete")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def add_ev_appliance(page, kind):
    """Choose one typed EV draft through the shared Add action rather than editing every device at once."""
    page.get_by_role("button", name="Add vehicle or charge point", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add vehicle or charge point", exact=True)
    dialog.get_by_role(
        "radio", name=re.compile("^Vehicle" if kind == "vehicle" else "^Charge point")
    ).check()
    dialog.get_by_role("button", name="Continue", exact=True).click()


def discover_charging_sources(page, url):
    """Expose one Energy-owned charge point and two vehicle SoC sources through the normal API."""
    body = discover(page, url, history=True)
    for entity, name, unit, kind, state_class in (
        ("sensor.charger_energy", "Wallbox", "kWh", "energy", "total_increasing"),
        ("sensor.charger_power", "Wallbox power", "W", "power", "measurement"),
        ("binary_sensor.charger_connected", "Vehicle connected", "", "plug", ""),
    ):
        body["entities"].append(
            {
                "entity_id": entity,
                "name": name,
                "device_name": name,
                "unit": unit,
                "device_class": kind,
                "state_class": state_class,
                "device_id": "",
            }
        )
    body["suggestions"]["consumers"] = [{"entity_id": "sensor.charger_energy", "upstream": None}]
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_ev_selected_device_summary_add_edit_cancel_and_remove_preserve_saved_ids(app_page):
    """Typed EV drafts keep original IDs until Save, display kW precisely and restore the selected item on Cancel."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/appliance-settings/ev")
    if "/api/hassio_ingress/" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#ev-empty")).to_be_visible()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    for index in range(2):
        add_ev_appliance(page, "vehicle")
        card = page.locator("#vehicles > section").nth(index)
        card.get_by_label("Home Assistant integration", exact=True).select_option("other")
        card.locator('[data-field="soc_entity"]').select_option(f"sensor.soc{index}")
        card.locator('[data-field="capacity_kwh"]').fill("60")
        card.locator('[data-field="maximum_w"]').fill("11.001")
        page.get_by_role("button", name="Save settings", exact=True).click()
        expect(page.locator("#dirty")).to_have_text("All changes saved.")
    before = page.request.get(url + "/v1/ev", headers=headers).json()
    first, second = before["vehicles"]
    assert [row["maximum_w"] for row in before["vehicles"]] == [11001, 11001]
    page.get_by_role("combobox", name="Appliance", exact=True).select_option(first["id"])
    expect(page.locator("#ev-summary")).to_contain_text("11.001 kW")
    expect(page.locator("#ev-editor")).to_be_hidden()
    expect(page.locator("#ev-summary")).to_be_visible()
    page.screenshot(path=f"results/ci/ev-saved-{page.viewport_size['width']}.png", full_page=True)
    page.get_by_role("button", name="Edit settings", exact=True).click()
    page.locator('#vehicles > section [data-field="maximum_w"]').first.fill("7.4")
    expect(page.locator("#ev-device-select")).to_be_disabled()
    page.get_by_role("navigation", name="Appliance settings", exact=True).get_by_role(
        "link", name="Electric boilers", exact=True
    ).click()
    page.get_by_role("alertdialog", name="Leave without saving?", exact=True).get_by_role(
        "button", name="Cancel", exact=True
    ).click()
    expect(page).to_have_url(url + "/appliance-settings/ev")
    expect(page.locator("#dirty")).to_contain_text("Unsaved changes")
    page.screenshot(path=f"results/ci/ev-editing-{page.viewport_size['width']}.png", full_page=True)
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#ev-summary")).to_contain_text("11.001 kW")
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"] == before["vehicles"]
    )
    page.locator("#ev-device-select").select_option(second["id"])
    for dismissal in ("Cancel", "Escape"):
        page.locator("#ev-more summary").click()
        page.get_by_role("button", name="Remove appliance…", exact=True).click()
        confirmation = page.get_by_role("alertdialog", name="Remove vehicle?", exact=True)
        expect(confirmation).to_contain_text(
            "Home Assistant sources and historical evidence are retained"
        )
        expect(confirmation.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
        Path("results/ci").mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"results/ci/confirmation-ev-{page.viewport_size['width']}.png")
        if dismissal == "Cancel":
            confirmation.get_by_role("button", name="Cancel", exact=True).click()
        else:
            page.keyboard.press("Escape")
        expect(confirmation).to_have_count(0)
        expect(page.locator("#ev-more summary")).to_be_focused()
        expect(page.locator("#dirty")).to_have_text("All changes saved.")
        assert (
            page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"]
            == before["vehicles"]
        )
    page.locator("#ev-more summary").click()
    page.get_by_role("button", name="Remove appliance…", exact=True).click()
    with page.expect_response(url + "/v1/ev"):
        page.evaluate("document.getElementById('reload-ev').click()")
    expect(page.locator("#ev-device-select")).to_be_enabled()
    page.get_by_role("alertdialog").get_by_role("button", name="Remove vehicle", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"] == before["vehicles"]
    )
    for save_removal in (False, True):
        page.locator("#ev-more summary").click()
        page.get_by_role("button", name="Remove appliance…", exact=True).click()
        page.get_by_role("alertdialog").get_by_role(
            "button", name="Remove vehicle", exact=True
        ).click()
        expect(page.locator("#dirty")).to_contain_text("Unsaved changes")
        assert len(page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"]) == 2
        page.get_by_role(
            "button", name="Save settings" if save_removal else "Cancel", exact=True
        ).click()
        expect(page.locator("#dirty")).to_have_text("All changes saved.")
    after = page.request.get(url + "/v1/ev", headers=headers).json()
    assert [row["id"] for row in after["vehicles"]] == [first["id"]]
    assert after["vehicles"][0] == first
    page.get_by_role("button", name="Add vehicle or charge point", exact=True).click()
    page.screenshot(path=f"results/ci/ev-add-{page.viewport_size['width']}.png")
    page.get_by_role("dialog", name="Add vehicle or charge point", exact=True).get_by_role(
        "button", name="Cancel", exact=True
    ).click()
    assert page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"] == after["vehicles"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_ev_removal_preserves_charge_point_vehicle_dependencies(app_page):
    """Block sole-vehicle removal without a draft, then retain point settings when another allowed car remains."""
    page, url = app_page
    discover_charging_sources(page, url)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    initial = page.request.get(url + "/v1/ev", headers=headers).json()
    vehicles = [
        {
            "id": f"00000000-0000-4000-8000-00000000020{index}",
            "soc_entity": f"sensor.soc{index}",
            "soc_timestamp_attribute": None,
            "capacity_kwh": 60,
            "maximum_w": 11000,
            "target_soc": 80,
            "departure_time": "07:00",
        }
        for index in range(2)
    ]
    point = {
        "id": "00000000-0000-4000-8000-000000000301",
        "energy_entity": "sensor.charger_energy",
        "power_entity": "sensor.charger_power",
        "connected_entity": "binary_sensor.charger_connected",
        "minimum_w": 1400,
        "maximum_w": 22000,
        "control": "variable",
        "vehicle_ids": [vehicles[0]["id"]],
    }
    configured = page.request.post(
        url + "/v1/ev/configuration",
        headers=headers,
        data={
            "configuration": {"vehicles": vehicles, "points": [point]},
            "expected_version": initial["configuration_version"],
        },
    )
    assert configured.ok, configured.text()
    before = page.request.get(url + "/v1/ev", headers=headers).json()
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    selector = page.get_by_role("combobox", name="Appliance", exact=True)
    expect(selector).to_have_value(vehicles[0]["id"])

    def remove_selected():
        """Use the actual dependency-aware More action rather than bypassing UI validation."""
        page.locator("#ev-more summary").click()
        page.get_by_role("button", name="Remove appliance…", exact=True).click()
        confirmation = page.get_by_role("alertdialog")
        if confirmation.count():
            confirmation.get_by_role(
                "button", name=re.compile("^Remove (vehicle|charge point)$")
            ).click()

    remove_selected()
    expect(page.locator("#dirty")).to_contain_text("only allowed vehicle")
    expect(selector).to_be_enabled()
    expect(page.locator("#ev-editor")).to_be_hidden()
    page.screenshot(
        path=f"results/ci/ev-removal-blocked-{page.viewport_size['width']}.png",
        full_page=True,
    )
    unchanged = page.request.get(url + "/v1/ev", headers=headers).json()
    assert unchanged["configuration_version"] == before["configuration_version"]
    for actual, expected in zip(unchanged["vehicles"], vehicles, strict=True):
        for key, value in expected.items():
            assert actual[key] == value
    for key, value in point.items():
        assert unchanged["points"][0][key] == value
    selector.select_option(point["id"])
    page.get_by_role("button", name="Edit settings", exact=True).click()
    page.locator('#points input[type="checkbox"]').nth(1).check()
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    selector.select_option(vehicles[0]["id"])
    remove_selected()
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    after = page.request.get(url + "/v1/ev", headers=headers).json()
    assert [vehicle["id"] for vehicle in after["vehicles"]] == [vehicles[1]["id"]]
    for key, value in {**point, "vehicle_ids": [vehicles[1]["id"]]}.items():
        assert after["points"][0][key] == value
    remove_selected()
    expect(page.locator("#dirty")).to_contain_text("only allowed vehicle")
    selector.select_option(point["id"])
    remove_selected()
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    selector.select_option(vehicles[1]["id"])
    remove_selected()
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    final = page.request.get(url + "/v1/ev", headers=headers).json()
    assert final["vehicles"] == [] and final["points"] == []


def test_ev_configuration_reuses_typed_ha_sources_and_preserves_native_intent(app_page):
    """Keep precise EV settings and daily requests on desktop and mobile.

    Reject stale configuration writes. Daily controls preserve independent drafts,
    retain rejected input, use HA-local departures and reject nonexistent times.
    Clearing an override restores the daily departure. Late evidence cannot undo
    an accepted target request.
    """
    page, url = app_page
    discover_charging_sources(page, url)
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert_floating_save_bar(page)
    for index in range(2):
        add_ev_appliance(page, "vehicle")
        card = page.locator("#vehicles > section").nth(index)
        card.get_by_label("Home Assistant integration", exact=True).select_option("other")
        card.locator('[data-field="soc_entity"]').select_option(f"sensor.soc{index}")
        assert "wrong_soc" not in card.locator('[data-field="soc_entity"]').inner_text()
        card.locator('[data-field="capacity_kwh"]').fill("60" if index == 0 else "40")
        card.locator('[data-field="maximum_w"]').fill("11" if index == 0 else "7.4")
        page.get_by_role("button", name="Save settings", exact=True).click()
        expect(page.locator("#dirty")).to_have_text("All changes saved.")
    add_ev_appliance(page, "point")
    point = page.locator("#points > section")
    for field, value in (
        ("energy_entity", "sensor.charger_energy"),
        ("power_entity", "sensor.charger_power"),
        ("connected_entity", "binary_sensor.charger_connected"),
        ("control", "variable"),
    ):
        point.locator(f'[data-field="{field}"]').select_option(value)
    point.locator('[data-field="minimum_w"]').fill("1.4")
    point.locator('[data-field="maximum_w"]').fill("22")
    for checkbox in point.locator('input[type="checkbox"]').all():
        checkbox.check()
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert page.locator("#vehicles > section").count() == 2
    assert page.locator("#points > section").count() == 1
    headers = {"Authorization": f"Bearer {TOKEN}"}
    saved = page.request.get(url + "/v1/ev", headers=headers).json()
    assert len(saved["vehicles"]) == 2
    assert len(saved["points"][0]["vehicle_ids"]) == 2
    assert [vehicle["maximum_w"] for vehicle in saved["vehicles"]] == [11000, 7400]
    assert saved["points"][0]["minimum_w"] == 1400
    assert saved["points"][0]["maximum_w"] == 22000
    assert page.request.post(
        url + "/v1/ev/intent",
        headers=headers,
        data={"vehicle_id": saved["vehicles"][0]["id"], "field": "target_soc", "value": 85.25},
    ).ok
    page.locator("#ev-device-select").select_option(saved["vehicles"][0]["id"])
    page.get_by_role("button", name="Edit settings", exact=True).click()
    page.locator('#vehicles > section [data-field="target_soc"]').first.fill("90")
    page.get_by_role("button", name="Check saved settings", exact=True).click()
    expect(page.locator("#dirty")).to_contain_text("Save or discard your changes")
    expect(page.locator('#vehicles > section [data-field="target_soc"]').first).to_have_value(
        "90.0"
    )
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#status")).to_contain_text(
        "Settings were rejected. Your edits are retained. Refresh before saving and review the selected sources and physical settings."
    )
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]["target_soc"]
        == 85.25
    )
    expect(page.locator("#dirty")).to_contain_text("Unsaved changes")
    page.get_by_role(
        "button",
        name="Cancel" if page.locator(".settings-toolbar").count() else "Discard changes",
        exact=True,
    ).click()
    expect(page.locator('#vehicles > section [data-field="target_soc"]').first).to_have_value(
        "85.3"
    )
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    page.get_by_role("button", name="Edit settings", exact=True).click()
    target = page.locator('#vehicles > section [data-field="target_soc"]').first
    target.focus()
    expect(target).to_have_value("85.25")
    target.fill("86.125")
    target.press("Tab")
    expect(target).to_have_value("86.1")
    assert target.evaluate("input => input.validity.valid")
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#dirty")).to_have_text("All changes saved.")
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]["target_soc"]
        == 86.125
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=f"results/ci/ev-settings-{page.viewport_size['width']}.png", full_page=True
    )
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#vehicle-controls > article")).to_have_count(2)
    expect(page.get_by_role("button", name="Add vehicle", exact=True)).to_have_count(0)
    expect(page.locator('[data-field="capacity_kwh"]')).to_have_count(0)
    vehicle = page.locator("#vehicle-controls > article").first
    page.evaluate("document.fonts.ready")
    for request_form in vehicle.locator("form").all():
        assert_field_actions_aligned(request_form, expected_buttons=2)
        expect(request_form.get_by_role("status")).to_have_count(1)
        expect(request_form.get_by_role("status")).to_have_text("")
    for control_column in ("input", "button:first-of-type", "button:last-of-type"):
        positions = vehicle.locator(f"form {control_column}").evaluate_all(
            "controls => controls.map(control => control.getBoundingClientRect().x)"
        )
        assert max(positions) - min(positions) <= 1
    page.screenshot(
        path=f"results/ci/ev-controls-{page.viewport_size['width']}.png", full_page=True
    )
    target = vehicle.get_by_label("Target state of charge (%)", exact=True)
    target.fill("87.125")
    page.get_by_role("button", name="Refresh charging evidence", exact=True).click()
    target.focus()
    expect(target).to_have_value("87.125")
    vehicle.get_by_role("button", name="Apply target state of charge (%)", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Request saved")
    updated = page.request.get(url + "/v1/ev", headers=headers).json()
    assert updated["vehicles"][0]["target_soc"] == 87.125
    assert updated["points"] == saved["points"]
    for field in ("id", "soc_entity", "capacity_kwh", "maximum_w"):
        assert updated["vehicles"][0][field] == saved["vehicles"][0][field]
    vehicle.get_by_label("Daily departure time", exact=True).fill("08:45")
    vehicle.get_by_role("button", name="Apply daily departure time", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Request saved")
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]["departure_time"]
        == "08:45"
    )

    target = vehicle.get_by_label("Target state of charge (%)", exact=True)
    departure = vehicle.get_by_label("Daily departure time", exact=True)
    target.fill("88.25")
    departure.fill("09:15")
    target.locator("xpath=ancestor::form").get_by_role(
        "button", name="Reset request", exact=True
    ).click()
    expect(departure).to_have_value("09:15")
    target.focus()
    expect(target).to_have_value("87.125")
    page.route("**/v1/ev/intent", lambda route: route.fulfill(status=409, body="Request rejected"))
    target.fill("89.25")
    vehicle.get_by_role("button", name="Apply target state of charge (%)", exact=True).click()
    expect(target.locator("xpath=ancestor::form").get_by_role("status")).to_have_text(
        "Request rejected"
    )
    expect(target.locator("xpath=ancestor::form").get_by_role("status")).to_be_visible()
    target.focus()
    expect(target).to_have_value("89.25")
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0]["target_soc"]
        == 87.125
    )
    page.unroute("**/v1/ev/intent")
    target.locator("xpath=ancestor::form").get_by_role(
        "button", name="Reset request", exact=True
    ).click()
    departure.locator("xpath=ancestor::form").get_by_role(
        "button", name="Reset request", exact=True
    ).click()
    override = vehicle.get_by_label("One-off departure", exact=True)
    override.fill("2027-03-28T02:30")
    vehicle.get_by_role("button", name="Apply one-off departure", exact=True).click()
    expect(override.locator("xpath=ancestor::form").get_by_role("status")).to_contain_text(
        "does not exist"
    )
    # Browser UTC must not replace the HA installation timezone when submitting a local departure.
    local_departure = (datetime.now(ZoneInfo("Europe/Amsterdam")) + timedelta(days=1)).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    override.fill(local_departure.strftime("%Y-%m-%dT%H:%M"))
    vehicle.get_by_role("button", name="Apply one-off departure", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Request saved")
    expect(override.locator("xpath=ancestor::form")).not_to_have_attribute("data-dirty", "true")
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0][
            "departure_override"
        ]
        == local_departure.astimezone(UTC).isoformat()
    )
    expect(override).to_be_enabled()
    expect(override).to_have_value(local_departure.strftime("%Y-%m-%dT%H:%M"))
    page.reload()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(override).to_have_value(local_departure.strftime("%Y-%m-%dT%H:%M"))
    override.fill((local_departure + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
    override.locator("xpath=ancestor::form").get_by_role(
        "button", name="Reset request", exact=True
    ).click()
    expect(override).to_have_value(local_departure.strftime("%Y-%m-%dT%H:%M"))
    assert (
        page.request.get(url + "/v1/ev", headers=headers).json()["vehicles"][0][
            "departure_override"
        ]
        == local_departure.astimezone(UTC).isoformat()
    )
    vehicle.get_by_role("button", name="Use daily departure", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Request saved")
    expect(override).to_have_value("")
    assert (
        page.request.get(url + "/v1/ev", headers=headers)
        .json()["vehicles"][0]
        .get("departure_override")
        is None
    )

    expect(vehicle.get_by_role("button", name="Use daily departure", exact=True)).to_be_enabled()
    stale = page.request.get(url + "/v1/ev", headers=headers).json()
    delayed = []

    def hold_ev_evidence(route):
        """Delay only the GET started before a successful daily target change."""
        if not delayed:
            delayed.append(route)
        else:
            expect(target).to_be_disabled()
            route.fallback()

    page.route("**/v1/ev", hold_ev_evidence)
    page.get_by_role("button", name="Refresh charging evidence", exact=True).click()
    target.fill("88.25")
    vehicle.get_by_role("button", name="Apply target state of charge (%)", exact=True).click()
    expect(target.locator("xpath=ancestor::form")).not_to_have_attribute("data-dirty", "true")
    expect(target).to_be_enabled()
    target.focus()
    expect(target).to_have_value("88.25")
    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(
        path=f"results/ci/ev-controls-{page.viewport_size['width']}.png", full_page=True
    )
    delayed[0].fulfill(json=stale)
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(target).to_have_value("88.25")


@pytest.mark.parametrize("secure_uuid", [True, False])
def test_appliance_profile_task_request_and_unknown_execution_are_visible(app_page, secure_uuid):
    """A known .83-kWh cycle submits readiness even without secure-context UUID support and blocks ambiguous retries."""
    page, url = app_page
    page.clock.install()
    if not secure_uuid:
        page.add_init_script("Object.defineProperty(crypto, 'randomUUID', {value: undefined})")
    state = {
        "revision": "fixture",
        "issue": None,
        "devices": [
            {
                "entity_id": "sensor.dishwasher_energy",
                "name": "Dishwasher",
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
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={
                "thermal_meters": [],
                "charging_meters": ["sensor.wallbox_energy"],
                "selected": [
                    {"entity_id": "sensor.dishwasher_energy"},
                    {"entity_id": "sensor.wallbox_energy"},
                ],
                "options": [
                    {"entity_id": "sensor.dishwasher_energy", "name": "Dishwasher"},
                    {"entity_id": "sensor.wallbox_energy", "name": "Wallbox"},
                ],
            }
        ),
    )
    page.route(
        "**/v1/ev",
        lambda route: route.fulfill(status=503, body="EV status unavailable"),
    )
    state["devices"].append(
        {
            "entity_id": "sensor.wallbox_energy",
            "name": "Wallbox",
            "shiftable": False,
            "profile": {"power_w": [7000], "samples": 1, "evidence": "synthetic"},
            "task": None,
        }
    )
    page.route("**/v1/appliances", lambda route: route.fulfill(json=state))

    task_requests, failed_writes = [], []
    page.on(
        "requestfailed",
        lambda request: (
            failed_writes.append(request.failure)
            if request.method == "POST" and request.url.endswith("/v1/appliances/tasks")
            else None
        ),
    )

    def submit(route):
        """Hold the committed task acknowledgement while asserting a helper-free request contract."""
        body = route.request.post_data_json
        assert set(body) == {"entity_id", "request_id", "ready_at", "deadline"}
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            body["request_id"],
        )
        assert body["entity_id"] == "sensor.dishwasher_energy"
        state["devices"][0]["task"] = {
            "id": "task",
            "status": "scheduled",
            "deadline": body["deadline"],
            "scheduled_start": body["ready_at"],
            "attempt": None,
            "events": [],
        }
        task_requests.append(route)
        page.evaluate("window.applianceTaskRequestHeld = true")

    page.route("**/v1/appliances/tasks", submit)
    page.goto(url + "/appliances")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_role("heading", name="Wallbox", exact=True)).to_have_count(0)
    expect(page.locator("#profile-device option")).to_have_text(["Dishwasher"])
    expect(page.locator("#profile-evidence")).to_contain_text("0.83 kWh")
    energy = page.locator("#cycle-energy-chart").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets[0].data"
    )
    assert energy == pytest.approx([0.45, 0.02, 0.02, 0, 0.02, 0.30, 0.02, 0])
    assert sum(energy) == pytest.approx(0.83)
    power_bounds = page.locator("#cycle-chart").bounding_box()
    energy_bounds = page.locator("#cycle-energy-chart").bounding_box()
    if page.viewport_size["width"] > 1000:
        assert energy_bounds["x"] > power_bounds["x"]
        assert energy_bounds["y"] == power_bounds["y"]
    else:
        assert energy_bounds["y"] >= power_bounds["y"] + power_bounds["height"]
    assert page.evaluate("""() => {
        const chart = Chart.getChart(document.getElementById('cycle-chart'));
        return chart.config.type === 'line' && chart.data.datasets[0].cubicInterpolationMode === 'monotone';
    }""")
    expect(page.locator(".save-bar")).to_have_count(0)
    expect(page.get_by_label("Use in OEP", exact=True)).to_have_count(0)
    expect(page.get_by_label("Allow shifting complete cycles", exact=True)).to_have_count(0)
    expect(page.locator("#profile-evidence")).to_have_text(
        "Profile: Learned: Completed cycles: 3: Evidence: Synthetic: Energy: 0.83 kWh: Cycle time: 120 minutes: Zero-power phases remain part of the cycle."
    )
    assert (
        page.evaluate("Chart.getChart(document.getElementById('cycle-chart')).data.labels[0]")
        == "0–15 min"
    )
    assert page.evaluate(
        "Chart.getChart(document.getElementById('cycle-chart')).data.datasets[0].data"
    ) == [1800, 80, 80, 0, 80, 1200, 80, 0]
    page.get_by_label("Finish before (Home Assistant time)").fill("2026-10-26T18:00")
    with page.expect_response(url + "/v1/appliances"):
        page.clock.fast_forward(10000)
    expect(page.get_by_label("Finish before (Home Assistant time)")).to_have_value(
        "2026-10-26T18:00"
    )
    page.get_by_role("button", name="Appliance is ready", exact=True).click()
    page.wait_for_function("window.applianceTaskRequestHeld === true")
    try:
        page.clock.run_for(31000)
        assert failed_writes == []
        assert len(task_requests) == 1
    finally:
        for route in task_requests:
            route.fulfill(json={"appliances": state})
    expect(page.locator("#appliance-list")).to_contain_text("Task: scheduled")
    state["devices"][0]["task"].update(
        status="start_not_confirmed",
        attempt={"id": "attempt", "status": "unknown", "expires_at": "2026-10-25T10:02:00Z"},
    )
    page.get_by_role("button", name="Refresh task evidence", exact=True).click()
    expect(page.locator("#appliance-list")).to_contain_text("start not confirmed")
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_be_disabled()

    def cancel(route):
        """Explicit user cancellation withdraws unknown planning without claiming physical completion."""
        assert route.request.post_data_json == {
            "entity_id": "sensor.dishwasher_energy",
            "task_id": "task",
        }
        state["devices"][0]["task"]["status"] = "cancelled"
        route.fulfill(json={"appliances": state})

    page.route("**/v1/appliances/cancel", cancel)
    page.get_by_role("button", name="Cancel planning", exact=True).click()
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Cancel planning", exact=True
    ).click()
    expect(page.locator("#appliance-list")).to_contain_text("Task: cancelled")
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_be_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_appliance_permissions_save_explicitly_and_retain_only_failed_changes(app_page):
    """Staged permission edits survive evidence refresh and partial failure, while discard retains an accepted write."""
    page, url = app_page
    state = {
        "devices": [
            {"entity_id": entity, "name": entity, "shiftable": False, "profile": None, "task": None}
            for entity in ("sensor.dishwasher", "sensor.dryer")
        ],
        "issue": None,
    }
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={
                "thermal_meters": [],
                "charging_meters": [],
                "selected": [{"entity_id": row["entity_id"]} for row in state["devices"]],
                "options": [
                    {"entity_id": row["entity_id"], "name": row["name"]} for row in state["devices"]
                ],
            }
        ),
    )
    page.route("**/v1/appliances", lambda route: route.fulfill(json=state))
    writes = []

    def save(route):
        """Accept the first independent configuration write and fail the second before persistence."""
        body = route.request.post_data_json
        writes.append(body)
        if body["entity_id"] == "sensor.dryer":
            route.fulfill(status=503, body="Dryer storage unavailable")
        else:
            state["devices"][0]["shiftable"] = body["shiftable"]
            route.fulfill(json={"appliances": state})

    page.route("**/v1/appliances/device", save)
    page.goto(url + "/appliance-settings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    permissions = page.get_by_label("Allow shifting complete cycles", exact=True)
    expect(permissions).to_have_count(2)
    page.get_by_role("button", name="Edit appliance settings", exact=True).click()
    permissions.nth(0).check()
    assert writes == []
    page.get_by_role("button", name="Save appliance settings", exact=True).click()
    expect(page.locator("#save-state")).to_contain_text("All changes saved")
    page.locator("#appliance-select").select_option("sensor.dryer")
    page.get_by_role("button", name="Edit appliance settings", exact=True).click()
    permissions.nth(1).check()
    page.locator("#appliance-more summary").click()
    page.get_by_role("button", name="Reload appliance settings", exact=True).click()
    expect(permissions.nth(1)).to_be_checked()
    page.get_by_role("button", name="Save appliance settings", exact=True).click()
    expect(page.locator("#save-state")).to_contain_text("Dryer storage unavailable")
    expect(page.locator("#save-state")).to_contain_text("Unsaved changes")
    assert writes == [
        {"entity_id": "sensor.dishwasher", "shiftable": True},
        {"entity_id": "sensor.dryer", "shiftable": True},
    ]
    assert_floating_save_bar(page)
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(permissions.nth(0)).to_be_checked()
    expect(permissions.nth(1)).not_to_be_checked()
    expect(page.locator("#save-state")).to_have_text("All changes saved.")
    expect(page.get_by_role("button", name="Edit appliance settings", exact=True)).to_be_visible()
    assert len(writes) == 2


def test_appliance_without_complete_cycle_cannot_submit_runnable_work(app_page):
    """A selected meter alone grants no scheduling authority. Missing profile keeps readiness disabled on desktop/mobile."""
    page, url = app_page
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={
                "thermal_meters": [],
                "charging_meters": [],
                "selected": [{"entity_id": "sensor.dishwasher"}],
                "options": [{"entity_id": "sensor.dishwasher", "name": "Dishwasher"}],
            }
        ),
    )
    page.route(
        "**/v1/appliances",
        lambda route: route.fulfill(
            json={
                "revision": "fixture",
                "issue": None,
                "devices": [
                    {
                        "entity_id": "sensor.dishwasher",
                        "name": "Dishwasher",
                        "shiftable": True,
                        "profile": None,
                        "task": None,
                    }
                ],
            }
        ),
    )
    page.goto(url + "/appliances")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_be_disabled()
    expect(page.locator("#appliance-list")).to_contain_text(
        "Waiting for a completed measured cycle"
    )


def test_appliance_settings_lock_writes_reload_and_open_cycle_removal(app_page):
    """Settings excludes operational commands, locks through post-save reads and protects unresolved cycles from removal."""
    page, url = app_page
    state = {
        "devices": [
            {
                "entity_id": "sensor.dishwasher",
                "name": "Dishwasher",
                "shiftable": True,
                "profile": {"power_w": [1000, 0], "samples": 1, "evidence": "synthetic"},
                "task": None,
            }
        ],
        "issue": None,
    }
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={
                "version": "fixture",
                "thermal_meters": [],
                "charging_meters": [],
                "selected": [{"entity_id": "sensor.dishwasher", "weather_sensitive": False}],
                "options": [{"entity_id": "sensor.dishwasher", "name": "Dishwasher"}],
            }
        ),
    )
    page.route("**/v1/appliances", lambda route: route.fulfill(json=state))
    page.route("**/v1/ev", lambda route: route.fulfill(json={"points": []}))
    held = []
    page.route("**/v1/appliances/device", lambda route: held.append(route))

    reads = []

    def evidence(route):
        """Hold the confirmed post-save read while newly rebuilt fields remain locked."""
        if held:
            reads.append(route)
        else:
            route.fulfill(json=state)

    page.route("**/v1/appliances", evidence)
    page.goto(url + "/appliance-settings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    permission = page.get_by_label("Allow shifting complete cycles", exact=True)
    expect(permission).to_be_checked()
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Edit appliance settings", exact=True).click()
    permission.uncheck()
    expect(page.locator("#discard-settings")).to_be_enabled()
    page.get_by_role("button", name="Save appliance settings", exact=True).click()
    expect(permission).to_be_disabled()
    expect(page.locator("#discard-settings")).to_be_disabled()
    expect(page.locator("#save-state")).to_contain_text("Saving changes")
    assert len(held) == 1
    state["devices"][0]["shiftable"] = False
    with page.expect_request(
        lambda request: request.method == "GET" and request.url.endswith("/v1/appliances")
    ):
        held[0].fulfill(json={"appliances": state})
    expect(page.locator("#save-state")).to_contain_text("Saving changes")
    assert len(reads) == 1
    expect(permission).to_be_disabled()
    reads[0].fulfill(json=state)
    expect(permission).to_be_disabled()
    expect(permission).not_to_be_checked()
    expect(page.locator("#save-state")).to_contain_text("All changes saved")
    state["devices"][0]["task"] = {"status": "scheduled"}
    held.clear()
    page.locator("#appliance-more summary").click()
    page.get_by_role("button", name="Reload appliance settings", exact=True).click()
    page.locator("#appliance-more summary").click()
    page.get_by_role("button", name="Remove appliance…", exact=True).click()
    expect(page.locator("#save-state")).to_contain_text("Resolve this appliance's open cycle")
    expect(permission).to_be_disabled()


def test_removing_appliance_revokes_shifting_before_source_removal(app_page):
    """Explicit removal revokes cycle permission first and a later re-add keeps permission off."""
    page, url = app_page
    device = {
        "entity_id": "sensor.washer",
        "name": "Washer",
        "shiftable": True,
        "profile": None,
        "task": None,
    }
    appliances = {"revision": "before-permission", "devices": [device], "issue": None}
    consumers = {
        "version": "fixture",
        "thermal_meters": [],
        "charging_meters": [],
        "selected": [{"entity_id": "sensor.washer", "weather_sensitive": False}],
        "options": [{"entity_id": "sensor.washer", "name": "Washer"}],
    }
    writes = []

    def select(route):
        """Store only source selections after the prior permission write has succeeded."""
        if route.request.method == "PUT":
            assert not device["shiftable"]
            writes.append("selection")
            consumers["selected"] = route.request.post_data_json["selected"]
        route.fulfill(json=consumers)

    def configure(route):
        """Revoke shiftability while its source is still selected."""
        assert consumers["selected"]
        assert route.request.post_data_json == {
            "entity_id": "sensor.washer",
            "shiftable": False,
            "expected_version": "before-permission",
        }
        writes.append("permission")
        device["shiftable"] = False
        appliances["revision"] = "after-permission"
        route.fulfill(json={"appliances": appliances})

    page.route("**/v1/appliances", lambda route: route.fulfill(json=appliances))
    page.route("**/v1/consumers", select)
    page.route("**/v1/appliances/device", configure)
    page.route("**/v1/ev", lambda route: route.fulfill(json={"points": []}))
    page.goto(url + "/appliance-settings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    permission = page.get_by_label("Allow shifting complete cycles", exact=True)
    expect(permission).to_be_checked()
    page.locator("#appliance-more summary").click()
    page.get_by_role("button", name="Remove appliance…", exact=True).click()
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Remove appliance", exact=True
    ).click()
    expect(page.locator("#appliance-empty")).to_be_visible()
    expect(page.locator("#save-state")).to_contain_text("All changes saved")
    assert writes == ["permission", "selection"]
    page.get_by_role("button", name="Add appliance", exact=True).click()
    page.locator(".source-choice").filter(has_text="Washer").click()
    permission = page.get_by_label("Allow shifting complete cycles", exact=True)
    expect(permission).to_be_enabled()
    expect(permission).not_to_be_checked()


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_appliance_source_picker_searches_large_catalogue_and_preserves_ha_membership(app_page):
    """Thirty-four HA sources remain searchable without injection, duplicate membership or writes before explicit saving."""
    page, url = app_page
    options = [
        {"entity_id": f"sensor.device_{index}", "name": f"Energy device {index:02d}"}
        for index in range(34)
    ]
    options[1]["name"] = "Washer <img src=x onerror=alert(1)>"
    state = {
        "version": "first",
        "thermal_meters": [],
        "charging_meters": ["sensor.device_33"],
        "selected": [
            {"entity_id": "sensor.device_0", "weather_sensitive": False},
            {"entity_id": "sensor.device_33", "weather_sensitive": False},
        ],
        "options": options,
    }
    writes = []

    def consumers(route):
        """Persist only explicit selection writes and retain the charging point's membership."""
        if route.request.method == "PUT":
            writes.append(route.request.post_data_json)
            assert route.request.post_data_json["expected_version"] == state["version"]
            state["selected"] = route.request.post_data_json["selected"]
            state["version"] = "next"
        route.fulfill(json=state)

    page.route("**/v1/consumers", consumers)
    page.route("**/v1/appliances", lambda route: route.fulfill(json={"devices": [], "issue": None}))
    page.goto(url + "/appliance-settings")
    if "hassio_ingress" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_role("button", name="Add appliance", exact=True).click()
    expect(page.locator(".source-choice")).to_have_count(32)
    expect(page.locator("#source-results img")).to_have_count(0)
    search = page.get_by_label("Search devices", exact=True)
    search.fill("SENSOR.DEVICE_32")
    expect(page.locator(".source-choice")).to_have_count(1)
    expect(page.locator(".source-choice")).to_contain_text("Energy device 32")
    search.fill("not found")
    expect(page.locator("#source-results")).to_contain_text("No matching devices")
    search.fill("")
    page.get_by_label("Show", exact=True).select_option("added")
    expect(page.locator(".source-choice")).to_have_count(1)
    expect(page.locator(".source-choice")).to_be_disabled()
    page.get_by_label("Show", exact=True).select_option("all")
    expect(page.locator(".source-choice")).to_have_count(33)
    before = search.bounding_box()
    footer = page.get_by_role("button", name="Cancel", exact=True).bounding_box()
    page.locator("#source-results").press("End")
    expect(page.locator(".source-choice").last).to_be_visible()
    assert search.bounding_box()["y"] == before["y"]
    assert page.get_by_role("button", name="Cancel", exact=True).bounding_box()["y"] == footer["y"]
    assert page.locator("#source-results").evaluate(
        "element => element.scrollHeight > element.clientHeight"
    )
    assert footer["y"] + footer["height"] <= page.viewport_size["height"]
    search.fill("sensor.device_32")
    page.locator(".source-choice").click()
    expect(page.locator("#appliance-title")).to_have_text("Energy device 32")
    assert writes == []
    page.get_by_label("Consumption depends on outdoor temperature", exact=True).last.check()
    page.get_by_role("button", name="Save appliance settings", exact=True).click()
    expect(page.locator("#save-state")).to_contain_text("All changes saved")
    assert len(writes) == 1
    assert state["selected"] == [
        {"entity_id": "sensor.device_0", "weather_sensitive": False},
        {"entity_id": "sensor.device_32", "weather_sensitive": True},
        {"entity_id": "sensor.device_33", "weather_sensitive": False},
    ]
    page.get_by_role("button", name="Add appliance", exact=True).click()
    page.get_by_label("Search devices", exact=True).fill("sensor.device_32")
    expect(page.locator(".source-choice")).to_have_count(0)
    page.get_by_label("Show", exact=True).select_option("added")
    expect(page.locator(".source-choice")).to_be_disabled()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_appliance_prototype_cycle_facts_keep_zero_power_phases(app_page):
    """A synthetic 1-kW quarter and zero-power quarter give 0.25 kWh over 30 minutes, not a 15-minute active-only cycle."""
    page, url = app_page
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={
                "version": "fixture",
                "thermal_meters": [],
                "charging_meters": [],
                "selected": [{"entity_id": "sensor.dishwasher", "weather_sensitive": False}],
                "options": [{"entity_id": "sensor.dishwasher", "name": "Dishwasher"}],
            }
        ),
    )
    page.route(
        "**/v1/appliances",
        lambda route: route.fulfill(
            json={
                "devices": [
                    {
                        "entity_id": "sensor.dishwasher",
                        "name": "Dishwasher",
                        "shiftable": True,
                        "profile": {"power_w": [1000, 0], "samples": 3, "evidence": "synthetic"},
                        "task": None,
                    }
                ],
                "issue": None,
            }
        ),
    )
    page.goto(url + "/appliance-settings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_role("heading", name="Dishwasher", exact=True)).to_be_visible()
    expect(page.locator("#appliance-membership")).to_have_text("Added")
    expect(page.locator("#appliance-evidence")).to_contain_text("Profile: Learned: Simulated data")
    expect(page.locator("#appliance-evidence")).to_contain_text("0.25 kWh")
    expect(page.locator("#appliance-evidence")).to_contain_text("30 minutes")
    expect(page.locator("#settings-devices code")).to_have_text("sensor.dishwasher")
    expect(page.locator("#settings-devices legend")).to_be_hidden()
    expect(page.get_by_label("Allow shifting complete cycles", exact=True)).to_be_disabled()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_ev_unchanged_fold_departures_keep_exact_latest_saved_instant(app_page):
    """Keep exact saved EV instants across clock folds on desktop and mobile.

    Controlled HTTP samples test UI conversion without the server's date window.
    Reapplying a saved second-occurrence instant preserves its offset and seconds.
    A refresh plus Reset uses the latest HA preference while another draft remains.
    Only a changed local value is converted through the normal HA timezone rule.
    """
    page, url = app_page
    vehicle_id = "00000000-0000-4000-8000-000000000201"
    vehicle = {
        "id": vehicle_id,
        "name": "Family car",
        "actual": {"soc": 50},
        "target_soc": 80,
        "departure_time": "07:00",
        "departure_override": "2026-10-25T01:30:42+00:00",
        "next_departure": "2026-10-25T01:30:42+00:00",
    }
    document = {"timezone": "Europe/Amsterdam", "vehicles": [vehicle], "points": [], "issue": None}
    intents = []
    page.route("**/v1/ev", lambda route: route.fulfill(json=document))

    def accept_intent(route):
        """Retain the exact submitted instant without invoking the real server's date-window validator."""
        intent = route.request.post_data_json
        assert intent["vehicle_id"] == vehicle_id
        assert intent["field"] == "departure_override"
        intents.append(intent)
        vehicle["departure_override"] = intent["value"]
        route.fulfill(json={"accepted": True})

    page.route("**/v1/ev/intent", accept_intent)
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    override = page.get_by_label("One-off departure", exact=True)
    apply = page.get_by_role("button", name="Apply one-off departure", exact=True)
    expect(override).to_have_value("2026-10-25T02:30")
    apply.click()
    expect(override).to_be_enabled()
    assert intents[-1]["value"] == "2026-10-25T01:30:42+00:00"

    target = page.get_by_label("Target state of charge (%)", exact=True)
    target.fill("83.25")
    vehicle["departure_override"] = "2026-10-25T01:45:23+00:00"
    document["issue"] = "Newer vehicle preference available"
    with page.expect_response(lambda response: response.url == url + "/v1/ev"):
        page.get_by_role("button", name="Refresh charging evidence", exact=True).click()
    expect(page.locator("#status")).to_have_text("Newer vehicle preference available")
    override.locator("xpath=ancestor::form").get_by_role(
        "button", name="Reset request", exact=True
    ).click()
    expect(override).to_have_value("2026-10-25T02:45")
    target.focus()
    expect(target).to_have_value("83.25")
    apply.click()
    expect(override).to_be_enabled()
    assert intents[-1]["value"] == "2026-10-25T01:45:23+00:00"

    override.fill("2026-10-25T03:15")
    apply.click()
    expect(override).to_be_enabled()
    assert intents[-1]["value"] == "2026-10-25T02:15:00.000Z"
    assert len(intents) == 3
    target.focus()
    expect(target).to_have_value("83.25")


def test_appliance_learning_manual_save_failure_and_retained_evidence(app_page):
    """Desktop/mobile retain failed drafts, save explicit estimates and show learned simulated cycles."""
    page, url = app_page
    width = page.viewport_size["width"]
    device = {
        "entity_id": "sensor.washer_energy",
        "name": "Washer",
        "shiftable": False,
        "task": None,
        "manual_profile": None,
        "profile": None,
        "learning_status": "waiting_for_complete_cycles",
        "recent_cycles": [],
    }
    state = {"revision": "initial", "issue": None, "devices": [device]}
    page.route("**/v1/appliances", lambda route: route.fulfill(json=state))
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={
                "version": "selection",
                "thermal_meters": [],
                "charging_meters": [],
                "selected": [{"entity_id": "sensor.washer_energy", "weather_sensitive": False}],
                "options": [
                    {
                        "entity_id": "sensor.washer_energy",
                        "name": "Washer",
                        "power_entity": "sensor.washer_power",
                    }
                ],
            }
        ),
    )
    writes, permission_writes = [], []

    def configure(route):
        """The permission write must guard its original revision before advancing the manual-write revision."""
        request = route.request.post_data_json
        permission_writes.append(request)
        assert request == {
            "entity_id": "sensor.washer_energy",
            "shiftable": True,
            "expected_version": "initial",
        }
        state["revision"] = "permission-saved"
        device["shiftable"] = True
        route.fulfill(json={"appliances": state})

    page.route("**/v1/appliances/device", configure)

    def save(route):
        """Reject the first write, then expose only the subsequently confirmed persisted values."""
        body = route.request.post_data_json
        writes.append(body)
        if len(writes) == 1:
            route.fulfill(status=503, body="Storage temporarily unavailable")
            return
        assert body == {
            "entity_id": "sensor.washer_energy",
            "duration_minutes": 90,
            "energy_kwh": 0.6,
            "expected_version": "permission-saved",
        }
        device.update(
            manual_profile={"duration_minutes": 90, "energy_kwh": 0.6},
            profile={"power_w": [400] * 6, "samples": 1, "evidence": "manual"},
            profile_kind="Manual",
            learned_profile={"power_w": [1000, 0, 1000 / 3], "samples": 1, "evidence": "synthetic"},
        )
        state["revision"] = "saved"
        route.fulfill(json={"appliances": state})

    page.route("**/v1/appliances/manual_profile", save)
    page.goto(url + "/appliance-settings")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    duration = page.get_by_label("Duration (minutes)", exact=True)
    energy = page.get_by_label("Energy consumption (kWh)", exact=True)
    expect(duration).to_be_disabled()
    expect(page.locator("#appliance-evidence")).to_contain_text(
        "Waiting for sufficient power history"
    )
    page.get_by_role("button", name="Edit appliance settings", exact=True).click()
    page.get_by_label("Allow shifting complete cycles", exact=True).check()
    duration.fill("90")
    energy.fill("0.6")
    page.get_by_role("button", name="Save appliance settings", exact=True).click()
    expect(page.locator("#save-state")).to_contain_text("Storage temporarily unavailable")
    expect(duration).to_have_value("90")
    expect(energy).to_have_value("0.6")
    page.get_by_role("button", name="Save appliance settings", exact=True).click()
    expect(page.locator("#save-state")).to_contain_text("All changes saved")
    assert len(permission_writes) == 1
    expect(page.locator("#appliance-evidence")).to_contain_text("Profile: Manual")
    expect(
        page.locator("#appliance-evidence")
        .get_by_text("Cycles in profile", exact=True)
        .locator("..")
        .locator("dd")
    ).to_have_text("0")
    device.update(
        profile={"power_w": [1000, 0, 1000 / 3], "samples": 2, "evidence": "synthetic"},
        profile_kind="Learned",
        learning_status="ready",
        learned_profile={"power_w": [1000, 0, 1000 / 3], "samples": 2, "evidence": "synthetic"},
        recent_cycles=[
            {
                "started_at": "2026-10-29T09:00:00Z",
                "completed_at": "2026-10-29T09:45:00Z",
                "energy_kwh": 0.333,
                "synthetic": True,
            }
        ],
    )
    page.locator("#appliance-more summary").click()
    page.get_by_role("button", name="Reload appliance settings", exact=True).click()
    expect(page.locator("#appliance-evidence")).to_contain_text("Profile: Learned: Simulated data")
    expect(page.locator("#appliance-evidence")).to_contain_text("45 minutes")
    expect(duration).to_have_value("90")
    expect(energy).to_have_value("0.6")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    directory = Path("results/ci")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / f"appliance-learning-{width}.png"), full_page=True)
    expect(page.get_by_role("heading", name="Detected cycles", exact=True)).to_have_count(0)
    device["shiftable"] = False
    page.get_by_role("link", name="View cycle profiles and detected runs", exact=True).click()
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#profile-device")).to_have_value("sensor.washer_energy")
    expect(page.locator("#profile-evidence")).to_contain_text("0.33 kWh")
    expect(page.locator("#profile-evidence")).to_contain_text("45 minutes")
    expect(page.locator("#profile-history")).to_contain_text("45 minutes: 0.33 kWh: Simulated")
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_have_count(0)
    page.screenshot(path=str(directory / f"appliance-cycle-evidence-{width}.png"), full_page=True)


@pytest.mark.parametrize("failure", ["parse", "fetch", "http"])
@pytest.mark.parametrize("entry", ["startup", "connect"])
def test_appliance_session_failures_show_only_owned_messages(app_page, failure, entry):
    """Metadata parse, fetch and HTTP failures never expose private values in the page or console."""
    page, url = app_page
    page.clock.install()
    marker = "synthetic-private-session-value"
    diagnostics = []
    page.on("pageerror", lambda error: diagnostics.append(str(error)))
    page.on("console", lambda message: diagnostics.append(message.text))
    page.add_init_script(
        f"""(() => {{
            window.failApplianceSession = {str(entry == "startup").lower()};
            const original = window.fetch;
            window.fetch = function(input, options) {{
                if ({str(failure == "fetch").lower()} && window.failApplianceSession && new URL(input, location.href).pathname.endsWith('/setup/session')) return Promise.reject(new TypeError('{marker}'));
                return original.call(this, input, options);
            }};
        }})();"""
    )

    def session(route):
        """Return healthy initial metadata or one controlled untrusted failure payload."""
        if not page.evaluate("window.failApplianceSession"):
            route.fulfill(json={"ingress": False, "timezone": "UTC"})
        elif failure == "http":
            route.fulfill(status=503, body=marker)
        else:
            route.fulfill(body=marker + "{broken")

    page.route("**/setup/session", session)
    page.route("**/v1/appliances", lambda route: route.fulfill(json={"devices": [], "issue": None}))
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={"charging_meters": [], "thermal_meters": [], "selected": [], "options": []}
        ),
    )
    page.goto(url + "/appliances")
    if entry == "connect":
        page.evaluate("window.failApplianceSession = true")
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_have_text(
        "Session information could not be loaded. Refresh this page and try again."
        if entry == "startup"
        else "Appliance information could not be loaded. Check the connection and try Refresh."
    )
    expect(page.locator("body")).not_to_contain_text(marker)
    assert all(marker not in message for message in diagnostics)


def test_appliance_evidence_waits_for_all_reads_before_next_poll(app_page):
    """An early failed read retains drafts and holds the poll guard until all companion reads settle."""
    page, url = app_page
    page.clock.install()
    device = {
        "entity_id": "sensor.washer_energy",
        "name": "Washer",
        "shiftable": True,
        "task": None,
        "profile": {"power_w": [400], "samples": 1, "evidence": "manual"},
        "profile_kind": "Manual",
        "recent_cycles": [],
    }
    data = {"devices": [device], "issue": None}
    sources = {
        "charging_meters": [],
        "thermal_meters": [],
        "selected": [{"entity_id": device["entity_id"]}],
        "options": [{"entity_id": device["entity_id"], "name": "Washer"}],
    }
    failing = [False]
    reads, held = [], []
    page.route(
        "**/setup/session", lambda route: route.fulfill(json={"ingress": False, "timezone": "UTC"})
    )

    def appliances(route):
        """Fail only the evidence poll while retaining a real initialized UI."""
        reads.append(route.request.url)
        if failing[0]:
            route.fulfill(status=503, body="synthetic-private-read-failure")
        else:
            route.fulfill(json=data)

    def consumers(route):
        """Keep the companion request active until explicitly released by the test."""
        if failing[0]:
            held.append(route)
            page.evaluate("window.applianceCompanionReadHeld = true")
        else:
            route.fulfill(json=sources)

    page.route("**/v1/appliances", appliances)
    page.route("**/v1/consumers", consumers)
    page.goto(url + "/appliances")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#profile-device")).to_have_value(device["entity_id"])
    draft = page.get_by_label("Finish before (Home Assistant time)")
    draft.fill("2026-11-07T12:00")
    draft.focus()
    failing[0] = True
    initial_reads = len(reads)
    try:
        page.clock.fast_forward(10000)
        page.wait_for_function("window.applianceCompanionReadHeld === true")
        page.clock.fast_forward(10000)
        page.evaluate("async () => (await fetch('/setup/session')).json()")
        assert len(reads) == initial_reads + 1
        assert len(held) == 1
        expect(draft).to_have_value("2026-11-07T12:00")
        expect(draft).to_be_focused()
    finally:
        for route in held:
            route.fulfill(json=sources)
    expect(page.locator("#status")).to_have_text(
        "Learning evidence refresh unavailable. Retained evidence is shown."
    )
    failing[0] = False
    page.clock.fast_forward(10000)
    expect(page.locator("#status")).to_have_text(
        "Connected: A schedule never proves physical execution."
    )
    assert len(reads) == initial_reads + 2
    expect(draft).to_have_value("2026-11-07T12:00")
    expect(draft).to_be_focused()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_appliance_read_deadline_cancels_stalled_network_and_recovers(app_page, phase):
    """Real stalled headers and response bodies time out without blocking subsequent evidence polls."""
    page, url = app_page
    page.clock.install()
    entered, release = Event(), Event()
    payload = json.dumps({"devices": [], "issue": None}).encode()

    class StalledResponse(BaseHTTPRequestHandler):
        """Hold an actual socket at the selected response boundary without exposing diagnostics."""

        def do_GET(self):
            """Send headers or a partial body and retain the connection until the test releases it."""
            if phase == "headers":
                entered.set()
                release.wait(60)
            with suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if phase == "body":
                    self.wfile.write(payload[:1])
                    self.wfile.flush()
                    entered.set()
                    release.wait(60)
                    self.wfile.write(payload[1:])
                else:
                    self.wfile.write(payload)

        def log_message(self, _format, *_arguments):
            """Keep synthetic HTTP request logging out of browser error assertions."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), StalledResponse)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stalled = [False]
    failures = []

    def appliances(route):
        """Use the real browser fetch and AbortSignal against a deliberately stalled HTTP response."""
        if stalled[0]:
            route.continue_(url=f"http://127.0.0.1:{server.server_port}/v1/appliances")
        else:
            route.fulfill(json={"devices": [], "issue": None})

    page.route("**/v1/appliances", appliances)
    page.route(
        "**/v1/consumers",
        lambda route: route.fulfill(
            json={"charging_meters": [], "thermal_meters": [], "selected": [], "options": []}
        ),
    )
    page.route(
        "**/setup/session", lambda route: route.fulfill(json={"ingress": False, "timezone": "UTC"})
    )
    page.on(
        "requestfailed",
        lambda request: (
            failures.append(request.failure) if request.url.endswith("/v1/appliances") else None
        ),
    )
    try:
        page.goto(url + "/appliances")
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.locator("#status")).to_have_text(
            "Connected: A schedule never proves physical execution."
        )
        stalled[0] = True
        page.clock.run_for(10000)
        page.evaluate("async () => (await fetch('/setup/session')).json()")
        assert entered.wait(2)
        page.clock.run_for(29000)
        assert failures == []
        expect(page.locator("#status")).to_have_text(
            "Connected: A schedule never proves physical execution."
        )
        page.clock.run_for(1001)
        expect(page.locator("#status")).to_have_text(
            "Learning evidence refresh unavailable. Retained evidence is shown."
        )
        assert len(failures) == 1
        stalled[0] = False
        release.set()
        page.clock.run_for(10000)
        expect(page.locator("#status")).to_have_text(
            "Connected: A schedule never proves physical execution."
        )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_boiler_saved_summary_and_editor_retain_the_existing_navigation(app_page):
    """The real boiler route uses the approved saved/edit layout and keeps category and primary navigation."""
    from tests.frontend.browser.test_thermal import document

    page, url = app_page
    page.route("**/v1/thermal", lambda route: route.fulfill(json=document()))
    page.goto(url + "/appliance-settings/thermal")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_role("heading", name="Utility boiler", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Appliance", exact=True)).to_be_visible()
    expect(page.locator("#thermal-summary")).to_contain_text("sensor.boiler_energy")
    expect(page.locator("#thermal-editor")).to_be_hidden()
    directory = Path("results/ci")
    directory.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    page.screenshot(path=str(directory / f"boiler-saved-{width}.png"), full_page=True)
    page.get_by_role("button", name="Edit settings", exact=True).click()
    expect(page.locator("#thermal-editor")).to_be_visible()
    expect(page.locator("#thermal-store-select")).to_be_disabled()
    expect(page.get_by_role("button", name="Save settings", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Cancel", exact=True)).to_be_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(directory / f"boiler-editing-{width}.png"), full_page=True)
    page.get_by_role("button", name="Cancel", exact=True).click()
    for accept in (False, True):
        page.locator("#thermal-more summary").click()
        page.locator("#remove-thermal").click()
        confirmation = page.get_by_role("alertdialog", name="Remove electric boiler?", exact=True)
        expect(confirmation).to_contain_text("Utility boiler")
        confirmation.get_by_role(
            "button", name="Remove boiler" if accept else "Cancel", exact=True
        ).click()
        if not accept:
            expect(page.get_by_role("heading", name="Utility boiler", exact=True)).to_be_visible()
        else:
            expect(page.locator("#thermal-save-state")).to_contain_text("Unsaved changes")
