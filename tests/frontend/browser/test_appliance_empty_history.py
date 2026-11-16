"""Empty recorded sources never erase a retained learned profile on the simplified cycle page."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from open_energy_planner.appliances.cycles.model import instant
from tests.app.appliance.raw_support import raw_trace
from tests.app.appliance.support import ENTITY, POWER
from tests.frontend.browser.helpers import TOKEN
from tests.ha_contract.support.metadata import setup_metadata


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
@pytest.mark.parametrize("app_site", ["appliance_followed"], indirect=True)
@pytest.mark.parametrize(
    "missing,claimed_complete",
    [
        ("both", False),
        ("power", False),
        ("energy", False),
        ("unknown", False),
        pytest.param("power", True, id="claimed-complete-power"),
        pytest.param("energy", True, id="claimed-complete-energy"),
    ],
)
def test_empty_state_sources_keep_learned_profile_without_requesting_raw_curves(
    app_page, missing, claimed_complete, app_site
):
    """Missing source curves and contradictory metadata cannot erase retained profiles or trigger raw UI reads."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    other = ENTITY + "_available"
    catalogue = setup_metadata()
    catalogue["entities"].extend(
        {
            "entity_id": entity,
            "name": name,
            "unit": unit,
            "device_id": "",
            "device_class": kind,
            "state_class": state_class,
        }
        for entity, name, unit, kind, state_class in (
            (ENTITY, "Retained washer", "kWh", "energy", "total_increasing"),
            (other, "Available washer", "kWh", "energy", "total_increasing"),
            (POWER, "Washer power", "W", "power", "measurement"),
        )
    )
    catalogue["suggestions"]["consumers"] = [
        {"entity_id": entity, "upstream": None, "power_entity": POWER} for entity in (ENTITY, other)
    ]
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=catalogue).ok
    consumers = page.request.get(url + "/v1/consumers", headers=headers).json()
    assert page.request.put(
        url + "/v1/consumers",
        headers=headers,
        data={
            "expected_version": consumers["version"],
            "selected": [
                {"entity_id": entity, "weather_sensitive": False} for entity in (ENTITY, other)
            ],
        },
    ).ok
    body = raw_trace()
    offset = datetime.now(UTC) - instant(body["end"]) - timedelta(seconds=1)
    for field in ("start", "end", "power_reported_at", "energy_reported_at"):
        body[field] = (instant(body[field]) + offset).isoformat()
    for field in ("power_states", "energy_states"):
        for row in body[field]:
            row["at"] = (instant(row["at"]) + offset).isoformat()
    for entity in (ENTITY, other):
        assert page.request.post(
            url + "/v1/appliances/raw_history", headers=headers, data={**body, "entity_id": entity}
        ).ok
    degraded = deepcopy(body)
    if missing in ("both", "power"):
        degraded["power_states"] = []
    if missing in ("both", "energy"):
        degraded["energy_states"] = []
    if missing == "unknown":
        degraded["power_states"][2]["state"] = "unavailable"
        degraded["energy_states"][2]["state"] = "unknown"
    assert page.request.post(url + "/v1/appliances/raw_history", headers=headers, data=degraded).ok
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    if claimed_complete:

        def claim_complete(route):
            """Inject inconsistent raw-status metadata while retaining the real learned profile."""
            response = route.fetch()
            data = response.json()
            for device in data["devices"]:
                if device.get("raw_history"):
                    device["raw_history"]["status"] = "complete"
            route.fulfill(response=response, json=data)

        page.route("**/v1/appliances", claim_complete)
    page.goto(url + "/appliance-cycles")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#profile-evidence")).to_contain_text("Energy: 0.51 kWh")
    expect(page.locator("#cycle-charts")).to_be_visible()
    expect(page.get_by_text("Recorded cycle measurements", exact=True)).to_have_count(0)
    page.wait_for_function(
        "() => Chart.getChart('cycle-chart')?.data.datasets[0].data.some(point => point.y === 3000)"
    )
    assert sum(
        page.evaluate("Chart.getChart('cycle-energy-chart').data.datasets[0].data")
    ) == pytest.approx(23 / 45)
    page.locator("#profile-device").select_option(other)
    expect(page.locator("#profile-title")).to_have_text("Available washer")
    expect(page.locator("#cycle-charts")).to_be_visible()
    page.locator("#profile-device").select_option(ENTITY)
    expect(page.locator("#profile-title")).to_have_text("Retained washer")
    expect(page.locator("#profile-evidence")).to_contain_text("Energy: 0.51 kWh")
    assert not any("raw_entity=" in address for address in requests)
