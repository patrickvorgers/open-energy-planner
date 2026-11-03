"""Protect shared panel separation without changing forms or their contents."""

from itertools import pairwise

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize(
    "route",
    [
        "/overview",
        "/ev",
        "/thermal",
        "/appliances",
        "/appliance-settings",
        "/appliance-settings/ev",
        "/appliance-settings/thermal",
        "/contract",
        "/quality",
        "/data-storage",
        "/setup",
    ],
)
@pytest.mark.parametrize("app_site", ["ingress"], indirect=True)
def test_visible_peer_panels_do_not_touch_on_any_workspace_page(app_page, route):
    """Every route separates peer panels without horizontal overflow on desktop and mobile."""
    page, url = app_page
    discover(page, url)
    page.goto(url + route)
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    groups = page.locator(".chart-card, .panel-stack > .settings-layout").evaluate_all("""panels => {
        const groups = new Map();
        for (const panel of panels) {
            const box = panel.getBoundingClientRect();
            if (!box.width || !box.height) continue;
            if (!groups.has(panel.parentElement)) groups.set(panel.parentElement, []);
            groups.get(panel.parentElement).push({left:box.left, top:box.top, right:box.right, bottom:box.bottom});
        }
        return [...groups.values()];
    }""")
    expected_gap = 16 if page.viewport_size["width"] <= 640 else 24
    for group in groups:
        for previous, following in pairwise(group):
            horizontal_gap = following["left"] - previous["right"]
            vertical_gap = following["top"] - previous["bottom"]
            assert max(horizontal_gap, vertical_gap) >= expected_gap, (route, previous, following)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize(
    "app_page",
    [{"width": 1440, "height": 900}, {"width": 768, "height": 900}, {"width": 390, "height": 844}],
    indirect=True,
)
def test_ev_panels_keep_the_same_gap_when_evidence_is_empty_or_unavailable(app_page):
    """Separate operational panels at every width, including hidden siblings and refresh errors."""
    page, url = app_page
    discover(page, url)
    page.goto(url + "/ev")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#sessions")).to_contain_text("No configured charge points")
    panels = page.locator("main > .panel-stack > .chart-card, main > .chart-card")
    expect(panels).to_have_count(3)
    page.screenshot(
        path=f"results/ci/panel-spacing-ev-{page.viewport_size['width']}.png", full_page=True
    )
    bounds = [panel.bounding_box() for panel in panels.all()]
    expected_gap = 16 if page.viewport_size["width"] <= 640 else 24
    for previous, following in pairwise(bounds):
        assert following["y"] - previous["y"] - previous["height"] == pytest.approx(expected_gap)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.route("**/v1/ev", lambda route: route.fulfill(status=503, body="Unavailable"))
    page.get_by_role("button", name="Refresh charging evidence", exact=True).click()
    expect(page.locator("#status")).to_have_text("Unavailable")
    expect(panels).to_have_count(3)
    bounds = [panel.bounding_box() for panel in panels.all()]
    for previous, following in pairwise(bounds):
        assert following["y"] - previous["y"] - previous["height"] == pytest.approx(expected_gap)
    page.screenshot(
        path=f"results/ci/panel-spacing-error-{page.viewport_size['width']}.png", full_page=True
    )


def test_generated_vehicle_panels_are_separated_after_refresh(app_page):
    """Dynamic vehicle cards retain the shared gap and can return to the genuine empty state."""
    page, url = app_page
    vehicles = [
        {
            "id": f"vehicle-{index}",
            "name": name,
            "actual": {"soc": 40},
            "target_soc": 80,
            "departure_time": "07:00",
            "departure_override": None,
            "next_departure": None,
        }
        for index, name in enumerate(("Household car", "Visitor car"))
    ]
    page.route(
        "**/v1/ev",
        lambda route: route.fulfill(
            json={
                "points": [],
                "vehicles": vehicles,
                "timezone": "Europe/Amsterdam",
                "issue": None,
            }
        ),
    )
    page.goto(url + "/ev")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    panels = page.locator("#vehicle-controls > .chart-card")
    expect(panels).to_have_count(2)
    page.evaluate("document.fonts.ready")
    first, second = [panel.bounding_box() for panel in panels.all()]
    expected_gap = 16 if page.viewport_size["width"] <= 640 else 24
    assert second["y"] - first["y"] - first["height"] == pytest.approx(expected_gap)
    vehicles.clear()
    page.get_by_role("button", name="Refresh charging evidence", exact=True).click()
    expect(panels).to_have_count(0)
    expect(page.locator("#vehicle-controls")).to_contain_text("Add vehicles")
