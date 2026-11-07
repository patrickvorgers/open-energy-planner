"""Preserve native number editing state across read-only source updates."""

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("raw,next_key,expected", [("180", "5", "1805"), ("1e", "2", "1e2")])
def test_boiler_refresh_keeps_native_editing_and_next_key(app_page, raw, next_key, expected):
    """Choice updates must not clear raw invalid numbers or select an unchanged value for replacement."""
    page, url = app_page
    body = discover(page, url)
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
