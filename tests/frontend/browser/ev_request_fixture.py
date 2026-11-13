"""Configure independent synthetic cars and an Energy-owned charge point through real APIs."""

from datetime import UTC, datetime
from types import SimpleNamespace

from tests.app.ev.support.devices import CAR, POINT, SESSION, catalogue, observations, settings
from tests.frontend.browser.helpers import TOKEN, discover


def configure_ev(page, url):
    """Create saved defaults and fresh measured evidence without creating a charging request."""
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
    configured = response.json()
    now = datetime.now(UTC)
    body = observations(
        SimpleNamespace(configuration_version=configured["configuration_version"]), now
    )
    response = page.request.post(url + "/v1/ev/observations", headers=headers, data=body)
    assert response.ok, response.text()
    response = page.request.post(
        url + "/v1/ev/assign",
        headers=headers,
        data={"point_id": POINT, "vehicle_id": CAR, "session_id": SESSION},
    )
    assert response.ok, response.text()
    return headers, page.request.get(url + "/v1/ev", headers=headers).json()
