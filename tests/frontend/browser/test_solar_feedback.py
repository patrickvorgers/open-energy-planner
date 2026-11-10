"""Configure actual curtailment feedback independently of proposed physical commands."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    discover,
    finish,
    grid,
    installation_area,
    read_settings,
)
from tests.frontend.browser.test_solar_imports import connect_solar_setup


def solar_setup(page, url):
    """Declare synthetic Energy membership and one actual-state feedback sensor."""
    body = discover(page, url)
    body["entities"].append(
        {
            "entity_id": "binary_sensor.synthetic_curtailment",
            "name": "Roof curtailment active",
            "unit": "",
            "device_class": "",
            "state_class": "",
            "device_id": "",
        }
    )
    body["suggestions"].update(
        solar=True,
        solar_sources=["sensor.roof"],
        solar_configurations=[
            {
                "entity_id": "sensor.roof",
                "providers": [],
                "issue": "solar_configuration_not_available",
            }
        ],
        terms=[
            {"entity_id": "sensor.incoming", "sign": 1},
            {"entity_id": "sensor.roof", "sign": 1},
        ],
    )
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": f"Bearer {TOKEN}"}, data=body
    ).ok
    page.reload()
    connect_solar_setup(page, url)
    grid(page)
    source = page.locator("#solar-rows > fieldset").first
    for label, value in [
        ("Panel capacity (kWp)", "1.2"),
        ("Azimuth (° clockwise from north)", "180"),
        ("Tilt (°)", "25"),
        ("Inverter AC rating (kW)", "1.5"),
    ]:
        source.get_by_label(label, exact=True).fill(value)
    return source


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_feedback_link_is_optional_persisted_and_independent_of_commands(app_page):
    """Desktop/mobile save the selected sensor, retain it across command toggles and restore it after reload."""
    page, url = app_page
    source = solar_setup(page, url)
    feedback = source.get_by_label("Curtailment feedback sensor", exact=True)
    expect(feedback).to_have_value("")
    expect(source).to_contain_text("before its first valid reading")
    feedback.select_option("binary_sensor.synthetic_curtailment")
    commands = source.get_by_label("Allow proposed solar on/off commands", exact=True)
    commands.check()
    commands.uncheck()
    expect(feedback).to_have_value("binary_sensor.synthetic_curtailment")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]["sources"][0]
    assert saved["curtailment_entity_id"] == "binary_sensor.synthetic_curtailment"
    assert saved["can_curtail"] is False
    page.reload()
    connect_solar_setup(page, url)
    installation_area(page, 1, edit=True)
    source.get_by_role("button", name="Change curtailment feedback sensor", exact=True).click()
    expect(feedback).to_have_value("binary_sensor.synthetic_curtailment")
    feedback.scroll_into_view_if_needed()
    page.screenshot(
        path=f"results/ci/solar-feedback-settings-{page.viewport_size['width']}-{'ingress' if '/api/hassio_ingress/' in url else 'standalone'}.png",
    )
    feedback.select_option("")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    assert read_settings(page, url)["solar"]["sources"][0].get("curtailment_entity_id") is None


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_optional_feedback_draft_follows_existing_save_and_cancel_behavior(app_page):
    """An unlinked source saves without a historical declaration, while Cancel restores its original feedback link."""
    page, url = app_page
    source = solar_setup(page, url)
    source.get_by_label("Allow proposed solar on/off commands", exact=True).check()
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]["sources"][0]
    assert saved["can_curtail"] is True
    assert saved.get("curtailment_entity_id") is None
    installation_area(page, 1, edit=True)
    feedback = source.get_by_label("Curtailment feedback sensor", exact=True)
    feedback.select_option("binary_sensor.synthetic_curtailment")
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("button", name="Edit installation", exact=True).click()
    expect(feedback).to_have_value("")
    assert read_settings(page, url)["solar"]["sources"][0] == saved


def test_feedback_restore_failure_publishes_only_owned_message(app_page, monkeypatch, caplog):
    """A private storage exception becomes the existing fixed message through the real producer, API and UI."""
    from open_energy_planner.solar import runtime

    private_value = "https://private.invalid/?token=FEEDBACK_ARCHIVE_PRIVATE_SENTINEL"

    def fail_restore(_documents, _entity, _now):
        """Inject an actual restore failure without constructing a forged HTTP issue value."""
        raise OSError(private_value)

    monkeypatch.setattr(runtime, "load_feedback", fail_restore)
    page, url = app_page
    source = solar_setup(page, url)
    source.get_by_label("Curtailment feedback sensor", exact=True).select_option(
        "binary_sensor.synthetic_curtailment"
    )
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    response = page.request.get(url + "/v1/solar", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.ok
    message = "Curtailment feedback history is unreadable. Check archive storage. Production history is retained."
    assert response.json()["feedback_issues"] == {"binary_sensor.synthetic_curtailment": message}
    assert "FEEDBACK_ARCHIVE_PRIVATE_SENTINEL" not in response.text()
    solar = response.json()
    # Isolate feedback rendering from the unrelated unavailable-weather service status.
    solar["weather_service"] = None
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={"days": [], "next_before": None, "current_models": {"solar": solar}}
        ),
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.evaluate("window.__oepProductReadsIdle()")
    page.get_by_role("button", name="Solar", exact=True).click()
    current = page.locator("#quality-models .quality-current-model")
    expect(current).to_have_count(1)
    current.get_by_text("Current model and learning", exact=True).click()
    expect(current).to_contain_text(message)
    assert "FEEDBACK_ARCHIVE_PRIVATE_SENTINEL" not in page.locator("body").inner_text()
    assert "FEEDBACK_ARCHIVE_PRIVATE_SENTINEL" not in caplog.text
