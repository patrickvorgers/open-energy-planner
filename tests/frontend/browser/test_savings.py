"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover, open_primary_navigation


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("initially_hidden", [False, True])
def test_archived_plan_uses_current_energy_name_without_rewriting_archive(
    app_page, initially_hidden
):
    """Desktop/mobile update a selected historical plan automatically while raw plan content and chart data stay frozen."""
    page, url = app_page
    metadata = discover(page, url, devices=True)
    metadata["suggestions"]["batteries"][0]["energy_entity"] = "sensor.discharge"
    for term in metadata["suggestions"]["terms"]:
        if term["entity_id"] == "sensor.discharge0":
            term["entity_id"] = "sensor.discharge"
    for row in metadata["entities"]:
        if row["entity_id"] == "sensor.discharge0":
            row["entity_id"] = "sensor.discharge"
            row.update(energy_name="Garage battery", device_name="Deye")
    headers = {"Authorization": "Bearer " + TOKEN}
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=metadata).ok
    path = url + "/v1/plans/browser-original-plan"
    original = page.request.get(path, headers=headers).json()
    page.clock.install()
    page.add_init_script(
        f"window.forceHidden = {str(initially_hidden).lower()}; Object.defineProperty(document, 'hidden', {{get: () => window.forceHidden}});"
    )
    page.goto(url + "/overview#planning")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#plan-description")).to_contain_text("Minimize cost")
    expect(page.locator("#plan-battery-rows")).to_contain_text("Garage battery")
    selected = page.locator("#plan-version").input_value()
    before = page.evaluate(
        "Chart.getChart(document.getElementById('energy-chart')).data.datasets.map(row => row.data)"
    )
    for row in metadata["entities"]:
        if row["entity_id"] == "sensor.discharge":
            row["energy_name"] = "Yixiang MB56 32kWh"
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=metadata).ok
    page.evaluate("window.forceHidden = false")
    page.clock.fast_forward(31000)
    expect(page.locator("#plan-battery-rows")).to_contain_text("Yixiang MB56 32kWh")
    expect(page.locator("#plan-version")).to_have_value(selected)
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('energy-chart')).data.datasets.map(row => row.data)"
        )
        == before
    )
    assert page.request.get(path, headers=headers).json() == original
    metadata["suggestions"]["batteries"] = metadata["suggestions"]["batteries"][1:]
    metadata["suggestions"]["terms"] = [
        row
        for row in metadata["suggestions"]["terms"]
        if row["entity_id"] not in ("sensor.discharge", "sensor.charge0")
    ]
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=metadata).ok
    page.clock.fast_forward(31000)
    expect(page.locator("#plan-battery-rows")).to_contain_text("Original battery")
    assert page.request.get(path, headers=headers).json() == original


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_savings_overview_compares_realized_costs_and_original_plan(app_page):
    """Desktop/mobile show -.20 measured-versus-simulated benefit and open original expectations without command changes."""
    page, url = app_page
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    seeded = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yesterday = (
        seeded.date().isoformat()
    )  # This fixture installation is UTC. The browser is New York.
    open_primary_navigation(page)
    page.get_by_role("link", name="Savings", exact=True).click()
    page.locator("#period-start").fill(yesterday)
    page.locator("#period-end").fill(yesterday)
    page.get_by_role("button", name="Show period").click()
    # The local day can have 92 or 100 quarters across daylight-saving transitions.
    expect(page.locator("#coverage")).to_contain_text("2 of ")
    expect(page.locator("#savings-total")).to_contain_text("-0.20 EUR")
    expect(page.locator("#evaluation-rows tr")).to_have_count(2)
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('energy-chart')).data.datasets.length"
        )
        == 3
    )
    page.locator("summary").filter(has_text="Advice, realization and evaluation evidence").click()
    page.get_by_role("button", name="View original plan").first.click()
    expect(page.locator("#plan-description")).to_contain_text("Minimize cost")
    expect(page.locator("#plan-rows tr")).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("aggregated", [False, True])
def test_savings_chart_breaks_missing_coverage_without_timezone_labels(app_page, aggregated):
    """Renderer-only sparse intervals/bins draw separate segments. Aggregated dates keep UTC binning without timezone labels on desktop/mobile."""
    page, url = app_page
    first = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    response = page.request.get(
        url + "/v1/savings",
        params={"start": first.isoformat(), "end": (first + timedelta(days=1)).isoformat()},
        headers={"Authorization": "Bearer " + TOKEN},
    )
    assert response.ok
    body = response.json()
    body["details_truncated"] = aggregated
    if aggregated:
        body["chart"] = [
            {"start": "2026-10-20", "end": "2026-10-21", "savings": 1},
            {"start": "2026-10-22", "end": "2026-10-23", "savings": -1},
        ]
    else:
        body["intervals"][1].update(
            start=(first + timedelta(minutes=30)).isoformat(),
            end=(first + timedelta(minutes=45)).isoformat(),
        )
    # Isolate chart rendering. The production evaluator itself never skips a reference gap.
    page.route("**/v1/savings?**", lambda route: route.fulfill(json=body))
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    open_primary_navigation(page)
    page.get_by_role("link", name="Savings", exact=True).click()
    expect(page.locator("#savings-chart-legend")).to_contain_text("Benefit")
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('savings-chart')).data.datasets[0].data.filter(p => p.y === null).length"
        )
        == 1
    )
    if aggregated:
        expect(page.locator("#savings-chart-range")).to_have_text("From 2026-10-20 to 2026-10-23")
        assert (
            page.locator("#savings-chart").evaluate(
                "canvas => Chart.getChart(canvas).options.scales.x.title.text"
            )
            == "Calendar date"
        )
