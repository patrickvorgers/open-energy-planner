"""Empty recorded sources are explicit without hiding available evidence or learned profiles."""

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
def test_empty_state_sources_keep_other_curve_and_profile_visible(
    app_page, missing, claimed_complete, app_site
):
    """Actual desktop/mobile detail distinguishes loading, empty sources and unknown gaps, preserving selection and profile."""
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
    page.add_init_script("""(() => {
        const original = window.fetch.bind(window);
        window.fetch = async (input, options) => {
            if (String(input).includes('v1/appliances?raw_entity=')) {
                window.rawDetailRequestedAt = performance.now();
                await new Promise(resolve => setTimeout(resolve, 500));
            }
            return original(input, options);
        };
    })();""")
    pending = []

    def hold_raw_detail(route):
        """Retain the intercepted request before acknowledging it in the browser."""
        pending.append(route)
        page.evaluate("window.rawDetailHeldAfter = performance.now() - window.rawDetailRequestedAt")

    page.route("**/v1/appliances?raw_entity=*", hold_raw_detail)
    page.goto(url + "/appliance-cycles")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_text("Recorded cycle measurements", exact=True).click()
    expect(page.locator("#recorded-cycle-status")).to_have_text("Loading recorded state details…")
    expect(page.locator("#recorded-power-unavailable")).to_be_hidden()
    expect(page.locator("#recorded-energy-unavailable")).to_be_hidden()
    page.wait_for_function("() => window.rawDetailHeldAfter !== undefined", timeout=5000)
    assert page.evaluate("window.rawDetailHeldAfter") > 100
    assert len(pending) == 1
    selected = pending.pop()
    if claimed_complete:
        # Keep actual source curves and profile. Only contradictory status metadata crosses this negative wire boundary.
        response = selected.fetch()
        detail = response.json()
        detail["devices"][0]["raw_history"]["status"] = "complete"
        selected.fulfill(response=response, json=detail)
    else:
        selected.continue_()
    page.unroute("**/v1/appliances?raw_entity=*")
    expect(page.locator("#recorded-cycle-status")).not_to_contain_text("Loading")
    if missing in ("both", "power", "energy"):
        expect(page.locator("#recorded-cycle-status")).not_to_contain_text("Power and energy agree")
        expect(page.locator("#recorded-cycle-status")).not_to_contain_text("completed cycle")
    for source, field in (
        ("power", "power_states"),
        ("energy", "energy_states"),
    ):
        chart_id = "recorded-" + source + "-chart"
        if not degraded[field]:
            expect(page.locator("#recorded-" + source + "-unavailable")).to_be_visible()
            expect(page.locator("#" + chart_id)).to_be_hidden()
        else:
            expect(page.locator("#recorded-" + source + "-unavailable")).to_be_hidden()
            expect(page.locator("#" + chart_id)).to_be_visible()
            actual = page.evaluate("id => Chart.getChart(id).data.datasets[0].data", chart_id)
            assert len(actual) == len(degraded[field])
            if missing == "unknown":
                assert actual[2]["y"] is None
            else:
                assert max(point["y"] for point in actual) == pytest.approx(
                    3000 if source == "power" else 23 / 45
                )
    expect(page.locator("#profile-evidence")).to_contain_text("Energy: 0.51 kWh")
    page.wait_for_function("""() => ['cycle-chart', 'cycle-energy-chart'].every(id => {
        const chart = Chart.getChart(id), meta = chart.getDatasetMeta(0);
        return meta.data.every((element, index) => Math.abs(element.y - chart.scales.y.getPixelForValue(meta._parsed[index].y)) < .1);
    })""")
    if not claimed_complete:
        page.screenshot(
            path=f"results/ci/cycle-recorder-{missing}-unavailable-{page.viewport_size['width']}.png",
            full_page=True,
        )
    page.locator("#profile-device").select_option(other)
    page.wait_for_function(
        "() => Chart.getChart('recorded-power-chart')?.data.datasets[0].data.some(point => point.y === 3000)"
    )
    expect(page.locator("#recorded-power-unavailable")).to_be_hidden()
    expect(page.locator("#recorded-energy-unavailable")).to_be_hidden()
    page.locator("#profile-device").select_option(ENTITY)
    expect(page.locator("#recorded-cycle-status")).not_to_contain_text("Loading")
    for source in ("power", "energy"):
        if missing in ("both", source):
            expect(page.locator("#recorded-" + source + "-unavailable")).to_be_visible()
