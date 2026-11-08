"""Persist explicit unrestricted solar-history periods without inferring physical operating state."""

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


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_solar_history_period_is_explicit_utc_persisted_and_keeps_invalid_drafts(app_page):
    """Desktop/mobile save a past declaration, reload it unchanged and reject a partial replacement without writing."""
    page, url = app_page
    body = discover(page, url)
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
    source.get_by_label("Panel capacity (kWp)", exact=True).fill("1.2")
    source.get_by_label("Azimuth (° clockwise from north)", exact=True).fill("180")
    source.get_by_label("Tilt (°)", exact=True).fill("25")
    source.get_by_label("Inverter AC rating (kW)", exact=True).fill("1.5")
    history = source.get_by_role(
        "group", name="Historical production for learning", exact=True, include_hidden=True
    )
    expect(history).to_be_hidden()
    source.get_by_label("Allow proposed solar on/off commands", exact=True).check()
    expect(history).to_be_visible()
    expect(history).to_contain_text("your declaration, not automatic detection")
    expect(history).to_contain_text("Times are UTC and the end is exclusive")
    start = history.get_by_label("Confirmed unrestricted production from (UTC)", exact=True)
    end = history.get_by_label("Confirmed unrestricted production until (UTC)", exact=True)
    start.fill("2026-01-01T00:00")
    end.fill("2026-06-01T00:00")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    mode = "ingress" if "/api/hassio_ingress/" in url else "standalone"
    page.evaluate("window.scrollTo(0, 0)")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    page.screenshot(
        path=f"results/ci/solar-history-confirmation-{page.viewport_size['width']}-{mode}.png",
        full_page=True,
    )
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]
    assert saved["sources"][0]["unrestricted_history_start"] == "2026-01-01T00:00:00+00:00"
    assert saved["sources"][0]["unrestricted_history_end"] == "2026-06-01T00:00:00+00:00"
    assert saved["sources"][0]["can_curtail"] is True
    page.reload()
    connect_solar_setup(page, url)
    installation_area(page, 1, edit=True)
    expect(start).to_have_value("2026-01-01T00:00")
    expect(end).to_have_value("2026-06-01T00:00")
    writes = []
    page.on(
        "request",
        lambda request: (
            writes.append(request.url)
            if request.method == "PUT" and "/v1/solar" in request.url
            else None
        ),
    )
    end.fill("")
    expect(end).to_have_attribute("required", "")
    save = page.get_by_role("button", name="Save installation", exact=True)
    if save.is_enabled():
        save.click()
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    expect(start).to_have_value("2026-01-01T00:00")
    expect(end).to_have_value("")
    assert not end.evaluate("input => input.validity.valid")
    assert writes == []
    assert read_settings(page, url)["solar"] == saved
    curtailment = source.get_by_label("Allow proposed solar on/off commands", exact=True)
    curtailment.uncheck()
    expect(history).to_be_hidden()
    curtailment.check()
    expect(start).to_have_value("2026-01-01T00:00")
    expect(end).to_have_value("")
    assert not end.evaluate("input => input.validity.valid")
    curtailment.uncheck()
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("button", name="Edit installation", exact=True).click()
    expect(curtailment).to_be_checked()
    expect(end).to_have_value("2026-06-01T00:00")
    end.fill("")
    curtailment.uncheck()
    with page.expect_response(
        lambda response: response.url.endswith("/v1/solar") and response.request.method == "PUT"
    ) as saved_inactive:
        save.click()
    assert saved_inactive.value.ok
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    retained = read_settings(page, url)["solar"]["sources"][0]
    assert retained["can_curtail"] is False
    assert retained["unrestricted_history_start"] == "2026-01-01T00:00:00+00:00"
    assert retained["unrestricted_history_end"] == "2026-06-01T00:00:00+00:00"
    page.reload()
    connect_solar_setup(page, url)
    installation_area(page, 1, edit=True)
    curtailment.check()
    expect(start).to_have_value("2026-01-01T00:00")
    expect(end).to_have_value("2026-06-01T00:00")


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_inactive_history_draft_does_not_block_wizard_or_publish_partial_declaration(app_page):
    """Turning commands off retains local draft values, permits Next and saves no hidden partial declaration."""
    page, url = app_page
    body = discover(page, url)
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
    for label, value in (
        ("Panel capacity (kWp)", "1.2"),
        ("Azimuth (° clockwise from north)", "180"),
        ("Tilt (°)", "25"),
        ("Inverter AC rating (kW)", "1.5"),
    ):
        source.get_by_label(label, exact=True).fill(value)
    curtailment = source.get_by_label("Allow proposed solar on/off commands", exact=True)
    history = source.get_by_role(
        "group", name="Historical production for learning", exact=True, include_hidden=True
    )
    start = history.get_by_label("Confirmed unrestricted production from (UTC)", exact=True)
    end = history.get_by_label("Confirmed unrestricted production until (UTC)", exact=True)
    curtailment.check()
    start.fill("2026-01-01T00:00")
    assert not end.evaluate("input => input.validity.valid")
    curtailment.uncheck()
    expect(history).to_be_hidden()
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.locator("#progress")).to_have_text("Stage 4 of 5")
    page.get_by_role("button", name="Back", exact=True).click()
    curtailment.check()
    expect(start).to_have_value("2026-01-01T00:00")
    expect(end).to_have_value("")
    assert not end.evaluate("input => input.validity.valid")
    curtailment.uncheck()
    assert not end.evaluate("input => input.willValidate")
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]["sources"][0]
    assert saved["can_curtail"] is False
    assert saved.get("unrestricted_history_start") is None
    assert saved.get("unrestricted_history_end") is None
