"""Keep per-source learning cutoffs in Home Assistant time without discarding history or concurrent edits."""

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
def test_learning_start_uses_ha_midnight_and_preserves_future_or_stale_drafts(app_page):
    """Desktop/mobile save HA midnight across DST, reload it and reject future or stale replacement without overwriting."""
    page, url = app_page
    body = discover(page, url, timezone="Europe/Amsterdam")
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
    cutoff = source.get_by_label("Learning data from", exact=True)
    expect(cutoff).to_be_visible()
    expect(cutoff).to_have_value("")
    expect(
        source.get_by_role("group", name="Historical production for learning", exact=True)
    ).to_be_hidden()
    expect(source).to_contain_text(
        "Older history is kept but does not train or validate this source"
    )
    cutoff.fill("2025-03-30")
    page.evaluate("window.scrollTo(0, 0)")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    mode = "ingress" if "/api/hassio_ingress/" in url else "standalone"
    page.screenshot(
        path=f"results/ci/solar-learning-start-{page.viewport_size['width']}-{mode}.png",
        full_page=True,
    )
    page.get_by_role("button", name="Continue", exact=True).click()
    finish(page)
    saved = read_settings(page, url)["solar"]
    assert saved["sources"][0]["learning_start"] == "2025-03-29T23:00:00+00:00"
    page.reload()
    connect_solar_setup(page, url)
    installation_area(page, 1, edit=True)
    expect(cutoff).to_have_value("2025-03-30")
    writes = []
    page.on(
        "request",
        lambda request: (
            writes.append(request.url)
            if request.method == "PUT" and "/v1/solar" in request.url
            else None
        ),
    )
    cutoff.fill("2100-01-01")
    save = page.get_by_role("button", name="Save installation", exact=True)
    if save.is_enabled():
        save.click()
    assert cutoff.evaluate("input => input.validity.rangeOverflow")
    expect(cutoff).to_have_value("2100-01-01")
    assert writes == []
    assert read_settings(page, url)["solar"] == saved
    cutoff.fill("2025-04-01")
    external = {
        "sources": [{**saved["sources"][0], "learning_start": "2025-05-01T00:00:00+00:00"}],
        "inverters": saved["inverters"],
        "expected_version": saved["version"],
    }
    response = page.request.put(
        url + "/v1/solar", headers={"Authorization": f"Bearer {TOKEN}"}, data=external
    )
    assert response.ok, response.text()
    writes.clear()
    with page.expect_response(
        lambda response: (
            response.url.endswith("/v1/setup/preview") and response.request.method == "POST"
        )
    ) as stale:
        save.click()
    assert stale.value.status == 409
    assert writes == []
    expect(cutoff).to_have_value("2025-04-01")
    expect(page.locator("#installation-save-state")).to_contain_text("Unsaved changes")
    assert (
        read_settings(page, url)["solar"]["sources"][0]["learning_start"]
        == "2025-05-01T00:00:00+00:00"
    )
    page.reload()
    connect_solar_setup(page, url)
    installation_area(page, 1, edit=True)
    expect(cutoff).to_have_value("2025-05-01")
    cutoff.fill("")
    page.get_by_role("button", name="Save installation", exact=True).click()
    expect(page.locator("#installation-save-state")).to_contain_text("All changes saved")
    assert read_settings(page, url)["solar"]["sources"][0].get("learning_start") is None
