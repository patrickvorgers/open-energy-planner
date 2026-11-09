"""Preserve native number editing state across read-only source updates."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    add_thermal_appliance,
    discover,
    discover_boiler_sources,
)
from tests.frontend.browser.test_appliance_settings import add_ev_appliance


@pytest.mark.parametrize("raw,next_key,expected", [("180", "5", "1805"), ("1e", "2", "1e2")])
def test_boiler_refresh_keeps_native_editing_and_next_key(app_page, raw, next_key, expected):
    """Choice updates must not clear raw invalid numbers or select an unchanged value for replacement."""
    page, url = app_page
    body = discover_boiler_sources(page, url)
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
        add_thermal_appliance(page)
    volume = page.get_by_label("Water volume (litres)", exact=False)
    volume.click()
    volume.press("ControlOrMeta+A")
    volume.press_sequentially(raw)
    volume.press("End")
    if raw == "1e":
        assert volume.evaluate("input => input.validity.badInput")
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers=headers,
        data={"scope": "thermal", "id": pending.value.json()["id"], "page": body},
    ).ok
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(volume).to_be_focused()
    if raw == "1e":
        assert volume.evaluate("input => input.validity.badInput")
    volume.press_sequentially(next_key)
    expect(volume).to_have_value(expected)


@pytest.mark.parametrize("raw,next_key,expected", [("1e", "2", "1e2"), ("80.", "5", "80.5")])
def test_target_soc_keeps_incomplete_text_and_next_key_during_source_refresh(
    app_page, raw, next_key, expected
):
    """Percentage focus listeners must not clear an exponent or unfinished decimal after read-only rendering."""
    page, url = app_page
    body = discover(page, url)
    page.goto(url + "/appliance-settings/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/v1/ha/source-choices/ev")
        )
    ) as pending:
        add_ev_appliance(page, "vehicle")
    target = page.get_by_label("Target state of charge (%)", exact=True)
    target.click()
    target.press("ControlOrMeta+A")
    target.press_sequentially(raw)
    target.press("End")
    if raw == "1e":
        assert target.evaluate("input => input.validity.badInput")
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers={"Authorization": "Bearer " + TOKEN},
        data={"scope": "ev", "id": pending.value.json()["id"], "page": body},
    ).ok
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(target).to_be_focused()
    if raw == "1e":
        assert target.evaluate("input => input.validity.badInput")
    target.press_sequentially(next_key)
    expect(target).to_have_value(expected)


def test_hourly_profile_stays_open_and_typing_continues_after_source_refresh(app_page):
    """Source choices cannot hide the focused hourly-demand field or interrupt its draft editing."""
    page, url = app_page
    body = discover_boiler_sources(page, url)
    page.goto(url + "/appliance-settings/thermal")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/v1/ha/source-choices/thermal")
        )
    ) as pending:
        add_thermal_appliance(page)
    page.get_by_text("Expected hot-water heat demand by hour", exact=True).click()
    hour = page.get_by_label("07:00 to 08:00 (thermal kWh)", exact=True)
    hour.fill("1.2")
    hour.press("End")
    assert page.request.post(
        url + "/v1/ha/source-choices",
        headers={"Authorization": "Bearer " + TOKEN},
        data={"scope": "thermal", "id": pending.value.json()["id"], "page": body},
    ).ok
    expect(page.locator("#status")).to_contain_text("sources refreshed")
    expect(page.locator("#thermal-demand-profile")).to_have_attribute("open", "")
    expect(hour).to_be_visible()
    expect(hour).to_be_focused()
    expect(hour).to_have_value("1.2")
    hour.press_sequentially("3")
    expect(hour).to_have_value("1.23")
