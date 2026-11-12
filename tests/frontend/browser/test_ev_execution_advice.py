"""Show real EV execution-deviation projection and recovery on desktop and mobile."""

from datetime import timedelta
from pathlib import Path

from playwright.sync_api import expect

from open_energy_planner.appliances.ev.service import EVService
from tests.app.ev.support.devices import CAR, POINT, SESSION, catalogue, observations, settings
from tests.frontend.browser.helpers import TOKEN, discover


def test_ev_execution_advice_names_the_device_and_preserves_measured_soc(app_page, monkeypatch):
    """Feed synthetic refused charging through the domain service and render its real API response."""
    page, url = app_page
    original_document = EVService.document
    recovered = [False]

    def observed_document(service, now, timezone, catalogue=None, plan=None):
        """Supply a confirmed stopped charge without mocking HTTP or the rendered explanation."""
        if service.state["points"]:
            before = now - timedelta(seconds=61)
            expected = {
                "valid_until": (now + timedelta(hours=1)).isoformat(),
                "steps": [
                    {
                        "start": before.isoformat(),
                        "end": (now + timedelta(hours=1)).isoformat(),
                        "devices": {POINT: {"power_w": 2300}},
                    }
                ],
            }
            if service.report is None:
                service.receive(observations(service, before), before)
                service.assign(POINT, CAR, SESSION, before)
                service.execution_deviated(expected, before)
            body = observations(service, now)
            body["points"][0]["power_w"] = 2300 if recovered[0] else 0
            service.receive(body, now)
            service.execution_deviated(expected, now)
        return original_document(service, now, timezone, catalogue, plan)

    monkeypatch.setattr(EVService, "document", observed_document)
    discovery = discover(page, url)
    charging = catalogue()
    discovery["entities"].extend(
        {"state_class": "", "device_id": "", **entity} for entity in charging["entities"]
    )
    discovery["suggestions"]["consumers"] = charging["suggestions"]["consumers"]
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = page.request.post(url + "/v1/ha/catalogue", headers=headers, data=discovery)
    assert response.ok, response.text()
    version = page.request.get(url + "/v1/ev", headers=headers).json()["configuration_version"]
    response = page.request.post(
        url + "/v1/ev/configuration",
        headers=headers,
        data={"expected_version": version, "configuration": settings()},
    )
    assert response.ok, response.text()
    page.goto(url + "/ev")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    sessions = page.locator("#sessions")
    expect(sessions).to_contain_text("Wallbox")
    expect(sessions).to_contain_text("Measured SoC: 50.0%")
    expect(sessions).to_contain_text("Check the vehicle charge limit")
    expect(sessions).to_contain_text("cannot determine the physical cause")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    Path("results/ci").mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=f"results/ci/ev-execution-advice-{page.viewport_size['width']}.png", full_page=True
    )
    recovered[0] = True
    page.locator("#refresh-evidence").click()
    expect(sessions).not_to_contain_text("Check the vehicle charge limit")
