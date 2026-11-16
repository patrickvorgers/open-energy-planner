"""Keep daily cycle planning separate from retained read-only appliance insights."""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_planning_links_select_device_and_insights_retain_unshiftable_history(app_page):
    """Device identity, pagination, retained models and narrow screens survive the page split."""
    page, url = app_page
    page.clock.install()
    start = datetime(2026, 11, 1, tzinfo=UTC)
    cycles = [
        {
            "started_at": (start + timedelta(hours=index * 4)).isoformat(),
            "completed_at": (start + timedelta(hours=index * 4 + 2)).isoformat(),
            "energy_kwh": 0.83,
            "synthetic": True,
        }
        for index in range(27)
    ]
    profile = {
        "power_w": [1800, 80, 80, 0, 80, 1200, 80, 0],
        "samples": 27,
        "evidence": "synthetic",
    }
    devices = [
        {
            "entity_id": entity,
            "name": name,
            "shiftable": shiftable,
            "profile": profile,
            "learned_profile": profile,
            "recent_cycles": cycles if shiftable else [],
            "task": None,
            "power_entity": entity + "_power",
            "history_import": {"phase": "complete"},
        }
        for entity, name, shiftable in (
            ("sensor.washer", "Example washer", False),
            ("sensor.dishwasher", "Example dishwasher", True),
        )
    ]
    sources = {
        "charging_meters": [],
        "thermal_meters": [],
        "selected": [{"entity_id": device["entity_id"]} for device in devices],
        "options": [
            {"entity_id": device["entity_id"], "name": device["name"]} for device in devices
        ],
    }
    page.route("**/v1/appliances", lambda route: route.fulfill(json={"devices": devices}))
    page.route("**/v1/consumers?view=settings", lambda route: route.fulfill(json=sources))
    page.goto(url + "/appliances")
    authenticate(page, url)
    expect(page.get_by_role("heading", name="Appliance planning", exact=True)).to_be_visible()
    expect(page.locator("#appliance-list > section")).to_have_count(1)
    expect(page.locator("#appliance-profiles")).to_have_count(0)
    expect(page.locator("#appliance-list")).to_contain_text("0.83 kWh, 120 minutes")
    page.screenshot(
        path=f"results/ci/appliance-planning-{page.viewport_size['width']}.png", full_page=True
    )
    page.get_by_role("link", name="View cycle insights", exact=True).click()
    authenticate(page, url)
    expect(page.locator("#profile-device")).to_have_value("sensor.dishwasher")
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_have_count(0)
    expect(page.locator("#detected-cycles > li")).to_have_count(25)
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.get_by_role("button", name="Previous", exact=True)).to_be_focused()
    expect(page.locator("#detected-cycles > li")).to_have_count(2)
    expect(page.get_by_text("Page 2", exact=True)).to_be_visible()
    first = page.locator("#detected-cycles details").first
    first.locator("summary").click()
    expect(first).to_have_attribute("open", "")
    with page.expect_response("**/v1/appliances"):
        page.clock.fast_forward(10000)
    expect(page.get_by_text("Page 2", exact=True)).to_be_visible()
    expect(first).to_have_attribute("open", "")
    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.get_by_role("button", name="Next", exact=True)).to_be_focused()
    expect(page.locator("#detected-cycles > li")).to_have_count(25)
    page.locator("#profile-device").select_option("sensor.washer")
    expect(page.locator("#profile-history")).to_contain_text(
        "This does not discard the retained model"
    )
    expect(page.locator("#profile-history")).to_contain_text("Fine cycles in model")
    expect(page.locator("#cycle-charts")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(
        "document.querySelectorAll('canvas').forEach(canvas => { const chart = Chart.getChart(canvas); chart?.stop(); chart?.update('none'); })"
    )
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/appliance-cycles-{page.viewport_size['width']}.png", full_page=True
    )
    page.go_back()
    expect(page.locator("#profile-device")).to_have_value("sensor.dishwasher")
    page.go_forward()
    expect(page.locator("#profile-device")).to_have_value("sensor.washer")
    page.reload()
    authenticate(page, url)
    expect(page.locator("#profile-device")).to_have_value("sensor.washer")
    page.goto(url + "/?page=" + quote("/appliance-cycles#entity=sensor.dishwasher", safe=""))
    content = page.frame_locator("#workspace-content")
    authenticate(content, url)
    expect(content.locator("#profile-device")).to_have_value("sensor.dishwasher")
    expect(page.locator('.sidebar a[aria-current="page"]')).to_contain_text("Appliance cycles")
    sources["charging_meters"] = ["sensor.dishwasher"]
    content.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
    expect(content.locator("#profile-device option")).to_have_text(["Example washer"])
    sources["selected"] = []
    content.get_by_role("button", name="Refresh cycle evidence", exact=True).click()
    expect(content.locator("#appliance-profiles")).to_be_hidden()
    expect(content.get_by_role("heading", name="No followed cycle appliances")).to_be_visible()
    assert content.locator("#cycle-chart").evaluate("canvas => !Chart.getChart(canvas)")


def authenticate(page, url):
    """Standalone pages accept a token while trusted ingress connects automatically."""
    if "/api/hassio_ingress/" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Connected")
