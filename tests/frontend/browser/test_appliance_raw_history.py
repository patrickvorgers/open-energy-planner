"""Retained Recorder evidence still supplies learned averages and conservative conflict fallbacks."""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from open_energy_planner.appliances.cycles.model import instant
from tests.app.appliance.raw_support import raw_trace
from tests.app.appliance.support import ENTITY, POWER, readings
from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
@pytest.mark.parametrize(
    "conflict,app_site", [(False, "appliance_followed"), (True, None)], indirect=["app_site"]
)
def test_recorder_evidence_keeps_profile_and_conservation_fallback_without_raw_charts(
    app_page, conflict, app_site
):
    """Desktop/mobile preserve exact learned energy and timing or the retained fallback without raw detail reads."""
    page, url = app_page
    catalogue = discover(page, url, publish=False)
    catalogue["entities"].extend(
        [
            {
                "entity_id": ENTITY,
                "name": "Recorded washer",
                "unit": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                "device_id": "",
            },
            {
                "entity_id": POWER,
                "name": "Recorded washer power",
                "unit": "W",
                "device_class": "power",
                "state_class": "measurement",
                "device_id": "",
            },
        ]
    )
    catalogue["suggestions"]["consumers"] = [
        {"entity_id": ENTITY, "upstream": None, "power_entity": POWER}
    ]
    other = ENTITY + "_other"
    catalogue["entities"].append(
        {**catalogue["entities"][-2], "entity_id": other, "name": "Other recorded washer"}
    )
    catalogue["suggestions"]["consumers"].append(
        {"entity_id": other, "upstream": None, "power_entity": POWER}
    )
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=catalogue).ok
    consumers = page.request.get(url + "/v1/consumers?view=settings", headers=headers).json()
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
    now = datetime.now(UTC)
    body = raw_trace(conflict=conflict)
    offset = now - instant(body["end"]) - timedelta(seconds=1)
    for field in ("start", "end", "power_reported_at", "energy_reported_at"):
        body[field] = (instant(body[field]) + offset).isoformat()
    for field in ("power_states", "energy_states"):
        for row in body[field]:
            row["at"] = (instant(row["at"]) + offset).isoformat()
    if conflict:
        old = readings(cycles=1)
        shift = (
            now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
            - instant(old[-1]["at"])
            - timedelta(hours=1)
        )
        old = [{**row, "at": (instant(row["at"]) + shift).isoformat()} for row in old]
        assert page.request.post(
            url + "/v1/appliances/history",
            headers=headers,
            data={"entity_id": ENTITY, "power_entity": POWER, "rows": old, "synthetic": True},
        ).ok
    result = page.request.post(url + "/v1/appliances/raw_history", headers=headers, data=body)
    assert result.ok, f"Synthetic raw history upload failed with HTTP {result.status}"
    from copy import deepcopy

    second = deepcopy(body)
    second["entity_id"] = other
    for row in second["power_states"]:
        row["state"] = str(float(row["state"]) * 2)
    for row in second["energy_states"]:
        row["state"] = str(10 + (float(row["state"]) - 10) * 2)
    assert page.request.post(url + "/v1/appliances/raw_history", headers=headers, data=second).ok
    assert (
        page.request.get(url + "/v1/appliances?raw_entity=" + POWER, headers=headers).status == 400
    )
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(url + "/appliance-cycles")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#cycle-charts")).to_be_visible()
    expect(page.get_by_text("Recorded cycle measurements", exact=True)).to_have_count(0)
    page.wait_for_function("() => Chart.getChart('cycle-chart')?.data.datasets[0].data.length > 0")
    if conflict:
        assert max(
            page.evaluate("Chart.getChart('cycle-chart').data.datasets[0].data")
        ) == pytest.approx(1000)
        expect(page.locator("#profile-evidence")).to_contain_text("Energy: 0.50 kWh")
        expect(page.locator("#profile-history")).to_contain_text("Provisional statistical profile")
    else:
        learned = page.evaluate("Chart.getChart('cycle-chart').data.datasets[0].data")
        assert max(point["y"] for point in learned) == 3000
        assert sum(
            page.evaluate("Chart.getChart('cycle-energy-chart').data.datasets[0].data")
        ) == pytest.approx(23 / 45)
        expect(page.locator("#profile-history")).to_contain_text("actual irregular phase durations")
        expect(page.locator("#profile-history")).to_contain_text("Continuously followed profile")
    page.screenshot(
        path=f"results/ci/cycle-recorder-{'conflict' if conflict else 'complete'}-{page.viewport_size['width']}.png",
        full_page=True,
    )
    page.locator("#profile-device").select_option(other)
    expect(page.locator("#profile-title")).to_have_text("Other recorded washer")
    expect(page.locator("#profile-kind")).to_have_text("Waiting for learning")
    expect(page.locator("#cycle-charts")).to_be_hidden()
    page.locator("#profile-device").select_option(ENTITY)
    expect(page.locator("#profile-title")).to_have_text("Recorded washer")
    assert not any("raw_entity=" in address for address in requests)
