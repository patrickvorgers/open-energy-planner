"""Show autonomous arrival, original SoC provenance and editable recognition sources in real browsers."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import expect

from tests.app.ev.support.devices import CAR, catalogue, observations, settings
from tests.frontend.browser.helpers import TOKEN, discover


def test_autonomous_arrival_uses_old_soc_and_exposes_estimate_without_prompting(app_page):
    """A three-day-old real 50% sample remains usable with one automatic default request."""
    page, url = app_page
    discovery = discover(page, url)
    charging = catalogue()
    for entity in charging["entities"]:
        discovery["entities"].append({"state_class": "", "device_id": "", **entity})
    discovery["suggestions"]["consumers"] = charging["suggestions"]["consumers"]
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=discovery).ok
    version = page.request.get(url + "/v1/ev", headers=headers).json()["configuration_version"]
    configuration = settings()
    configuration["points"][0]["vehicle_ids"] = [CAR]
    response = page.request.post(
        url + "/v1/ev/configuration",
        headers=headers,
        data={"expected_version": version, "configuration": configuration},
    )
    assert response.ok, response.text()
    service = SimpleNamespace(configuration_version=response.json()["configuration_version"])
    now = datetime.now(UTC)
    body = observations(service, now)
    old_sample = now - timedelta(days=3)
    body["vehicles"][0].update(
        observed_at=old_sample.isoformat(),
        reported_at=now.isoformat(),
        timestamp_kind="device_sample",
        source_timestamp_attribute="Last updated",
    )
    body["points"][0]["energy_kwh"] = 100
    response = page.request.post(url + "/v1/ev/observations", headers=headers, data=body)
    assert response.ok, response.text()
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    session = page.locator("#sessions article").first
    expect(session).to_contain_text("Estimated SoC: 50.0%")
    expect(session).to_contain_text("Only allowed vehicle")
    expect(session).to_contain_text("Driving since that measurement is not known")
    expect(page.locator("#vehicle-controls article").first).to_contain_text(
        "Planned charging request"
    )
    assert page.get_by_role("combobox", name="Connected vehicle").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=f"results/ci/ev-autonomous-{page.viewport_size['width']}.png", full_page=True
    )
