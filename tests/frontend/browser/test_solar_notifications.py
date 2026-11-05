"""Show clear solar recovery advice without technical disclosures on desktop and mobile."""

from datetime import UTC, datetime

import pytest
from playwright.sync_api import expect

from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.notifications.collection import NotificationCenter
from open_energy_planner.solar.calculation.model import (
    SolarConfiguration,
    SolarInverter,
    SolarSource,
)
from open_energy_planner.solar.notifications import (
    explain_solar_problem,
    solar_forecast_notifications,
)
from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_solar_forecast_notice_explains_automatic_recovery_and_clears(app_page, app_site):
    """Both notification views retain unknown status and clear confirmed recovery without a dismiss or technical details."""
    page, url = app_page
    page.clock.install()
    configuration = SolarConfiguration(
        (SolarSource("pv", "sensor.pv", "inverter", 1000, "south"),),
        (SolarInverter("inverter", 5000),),
    )
    current = [
        solar_forecast_notifications(
            configuration,
            None,
            SiteLocation(52, 6, "Europe/Amsterdam"),
            datetime(2026, 6, 20, tzinfo=UTC),
            "solar_weather_expired",
        )
    ]
    center = NotificationCenter()
    center.register("solar", lambda: current[0], explain=explain_solar_problem)
    page.route("**/v1/notifications", lambda route: route.fulfill(json=center.document()))
    page.goto(url + "/quality")
    if "/api/hassio_ingress/" not in url:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    banner = page.get_by_role("region", name="Notifications", exact=True)
    expect(banner.get_by_role("heading", name="Solar forecast is unavailable")).to_be_visible()
    expect(banner.locator("article > p")).to_have_text(
        "Weather data for the solar forecast is too old. Weather retrieval and solar forecast "
        "updates are retried automatically. This notification clears when a valid, up-to-date "
        "solar forecast is available. If it persists, review the solar forecast status."
    )
    expect(banner.get_by_text("Technical details", exact=True)).to_have_count(0)
    expect(banner).not_to_contain_text("solar_weather_expired")
    expect(banner).not_to_contain_text("solar_forecast_unavailable")
    page.locator(".notification-toggle").click()
    popup = page.get_by_role("dialog", name="Notifications", exact=True)
    expect(popup).to_contain_text("Solar forecast is unavailable")
    expect(popup.get_by_text("Technical details", exact=True)).to_have_count(0)
    expect(popup).not_to_contain_text("solar_weather_expired")
    popup.get_by_role("link", name="Solar forecast is unavailable").click()
    expect(page).to_have_url(url + "/quality#planning-notifications")
    banner = page.get_by_role("region", name="Notifications", exact=True)
    expect(banner.get_by_role("heading", name="Solar forecast is unavailable")).to_be_visible()
    expect(banner).to_be_focused()
    if page.viewport_size["width"] < 500:
        heading = banner.get_by_role("heading", name="Solar forecast is unavailable").bounding_box()
        header = page.locator(".settings-mobilebar").bounding_box()
        assert heading["y"] >= header["y"] + header["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    mode = "ingress" if "/api/hassio_ingress/" in app_site else "standalone"
    banner.screenshot(
        path=f"results/ci/solar-forecast-notification-{page.viewport_size['width']}-{mode}.png"
    )
    page.wait_for_function(
        "document.getElementById('integration-version-status').hasAttribute('title')"
    )
    current[0] = None
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(banner).to_contain_text("last known notifications")
    expect(banner.get_by_role("heading", name="Solar forecast is unavailable")).to_be_visible()
    current[0] = ()
    with page.expect_response("**/v1/notifications"):
        page.clock.fast_forward(16000)
    expect(banner).to_be_hidden()
    expect(page.locator(".notification-toggle")).to_be_hidden()
