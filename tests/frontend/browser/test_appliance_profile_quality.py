"""Representative complete cycles distinguish statistical estimates from followed measured profiles."""

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
    page.goto(url + "/appliances")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
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
    page.wait_for_function("""() => ['cycle-chart', 'cycle-energy-chart'].every(id => {
        const chart = Chart.getChart(id), meta = chart.getDatasetMeta(0);
        return meta.data.every((element, index) => Math.abs(element.y - chart.scales.y.getPixelForValue(meta._parsed[index].y)) < .1);
    })""")
    page.screenshot(
        path=f"results/ci/cycle-quality-{'statistics' if statistical else 'observed'}-{page.viewport_size['width']}.png",
        full_page=True,
    )
