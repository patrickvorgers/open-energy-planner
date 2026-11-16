"""Representative complete cycles distinguish statistical estimates from followed measured profiles."""

from itertools import pairwise

import pytest
from playwright.sync_api import expect

from tests.app.appliance.support import ENTITY, POWER
from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize(
    "statistical,app_site",
    [(False, "appliance_quality_observed"), (True, "appliance_quality_statistics")],
    indirect=["app_site"],
)
@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_profile_quality_explains_full_cycle_on_desktop_and_mobile(app_page, app_site, statistical):
    """Actual charts show the complete wash and pause with honest provenance after storage restart."""
    page, url = app_page
    connect_profile(page, url)
    expect(page.locator("#cycle-charts")).to_be_visible()
    page.wait_for_function("""() => ['cycle-chart', 'cycle-energy-chart'].every(id => {
        const chart = Chart.getChart(id), meta = chart?.getDatasetMeta(0);
        return meta?.data.length && meta.data.every((element, index) => Math.abs(element.y - chart.scales.y.getPixelForValue(meta._parsed[index].y)) < .1);
    })""")
    page.screenshot(
        path=f"results/ci/cycle-layout-{'statistics' if statistical else 'observed'}-{page.viewport_size['width']}.png",
        full_page=True,
    )
    page.get_by_text("Recorded cycle measurements", exact=True).click()
    expect(page.locator("#profile-history")).to_contain_text(
        "Provisional statistical profile" if statistical else "Continuously followed profile"
    )
    expect(page.locator("#recorded-cycle-status")).to_contain_text(
        "not continuously followed" if statistical else "Power and energy agree"
    )
    page.wait_for_function(
        "() => Chart.getChart('recorded-power-chart')?.data.datasets[0].data.length > 18"
    )
    assert page.evaluate(
        "Chart.getChart('recorded-power-chart').data.datasets[0].data.some(point => point.y === 3000)"
    )
    if not statistical:
        # Eighteen five-minute phases total 11800 W, plus a 900-W lift lasting twenty seconds.
        energy_total = page.evaluate(
            "Chart.getChart('cycle-energy-chart').data.datasets[0].data.reduce((total, value) => total + value, 0)"
        )
        assert energy_total == pytest.approx(593 / 600, abs=0.001)
        expect(page.locator("#profile-evidence")).to_contain_text("Cycle time: 90 minutes")
        learned_power = page.evaluate("Chart.getChart('cycle-chart').data.datasets[0].data")
        assert learned_power[-1]["x"] == 90
        pauses = [
            (phase["x"], following["x"])
            for phase, following in pairwise(learned_power)
            if phase["y"] == 0
        ]
        assert pauses == [(40, 45), (45, 50), (50, 55)]
        assert sum(end - start for start, end in pauses) == 15
    page.wait_for_function("""() => ['cycle-chart', 'cycle-energy-chart'].every(id => {
        const chart = Chart.getChart(id), meta = chart.getDatasetMeta(0);
        return meta.data.every((element, index) => Math.abs(element.y - chart.scales.y.getPixelForValue(meta._parsed[index].y)) < .1);
    })""")
    page.screenshot(
        path=f"results/ci/cycle-quality-{'statistics' if statistical else 'observed'}-{page.viewport_size['width']}.png",
        full_page=True,
    )


@pytest.mark.parametrize("app_site", ["appliance_quality_legacy"], indirect=True)
@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_legacy_counter_only_profile_does_not_claim_interval_mean_power(app_page, app_site):
    """A retained quarter-hour counter archive reaches the actual UI after refinement and restart."""
    page, url = app_page
    connect_profile(page, url)
    response = page.request.get(
        url + "/v1/appliances", headers={"Authorization": "Bearer " + TOKEN}
    )
    assert response.ok
    device = next(row for row in response.json()["devices"] if row["entity_id"] == ENTITY)
    assert device["profile_basis"] == "statistics"
    assert device["profile"]["phase_seconds"] == 900
    expect(page.locator("#profile-evidence")).to_contain_text("Energy: 0.50 kWh")
    expect(page.locator("#profile-evidence")).to_contain_text("Cycle time: 60 minutes")
    expect(page.locator("#profile-history")).to_contain_text("Provisional statistical profile")
    expect(page.locator("#profile-history")).not_to_contain_text("Five-minute mean power")


def connect_profile(page, url):
    """Publish synthetic Energy entities and connect the real appliance page."""
    catalogue = discover(page, url, publish=False)
    catalogue["entities"].extend(
        {
            "entity_id": entity,
            "name": name,
            "unit": unit,
            "device_class": kind,
            "state_class": state_class,
            "device_id": "",
        }
        for entity, name, unit, kind, state_class in (
            (ENTITY, "Synthetic washer", "kWh", "energy", "total_increasing"),
            (POWER, "Synthetic washer power", "W", "power", "measurement"),
        )
    )
    catalogue["suggestions"]["consumers"] = [
        {"entity_id": ENTITY, "upstream": None, "power_entity": POWER}
    ]
    assert page.request.post(
        url + "/v1/ha/catalogue", headers={"Authorization": "Bearer " + TOKEN}, data=catalogue
    ).ok
    page.goto(url + "/appliance-cycles")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
