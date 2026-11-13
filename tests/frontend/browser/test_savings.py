"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    discover,
    open_primary_navigation,
    wait_for_product_reads,
)


def named_archive_battery(page, url):
    """Match the synthetic catalogue to the archived battery's frozen metering binding."""
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
    return metadata


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("initially_hidden", [False, True])
def test_archived_plan_uses_current_energy_name_without_rewriting_archive(
    app_page, initially_hidden
):
    """Desktop/mobile update a selected historical plan automatically while raw plan content and chart data stay frozen."""
    page, url = app_page
    metadata = named_archive_battery(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
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
    expect(page.locator("#plan-chart-legend")).to_contain_text("Garage battery")
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
    expect(page.locator("#plan-chart-legend")).to_contain_text("Yixiang MB56 32kWh")
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
    expect(page.locator("#plan-chart-legend")).to_contain_text("Original battery")
    assert page.request.get(path, headers=headers).json() == original


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
@pytest.mark.parametrize("failed_refresh", [False, True])
@pytest.mark.parametrize("entry_view", ["planning", "savings"])
@pytest.mark.parametrize(
    "savings_now",
    [datetime(2026, 11, 12, 12, 30, tzinfo=UTC), datetime(2026, 11, 12, 23, 30, tzinfo=UTC)],
    ids=["normal-utc", "late-utc"],
)
def test_source_rename_on_savings_refreshes_the_pinned_plan_when_reopened(
    app_page, failed_refresh, entry_view, savings_now
):
    """An explicit seeded day preserves pinned-plan names and chart choices across the HA midnight boundary."""
    page, url = app_page
    metadata = named_archive_battery(page, url)
    headers = {"Authorization": "Bearer " + TOKEN}
    path = url + "/v1/plans/browser-original-plan"
    original = page.request.get(path, headers=headers).json()
    reads = []
    page.on(
        "request",
        lambda request: reads.append(request.url) if "/v1/savings?" in request.url else None,
    )
    page.clock.install(time=savings_now)
    page.goto(url + "/overview#" + entry_view)
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    if entry_view == "planning":
        expect(page.locator("#overview-status")).to_contain_text("Connected")
        open_primary_navigation(page)
        page.get_by_role("link", name="Savings", exact=True).click()
    seeded_day = (
        datetime.fromisoformat(original["input_snapshot"]["problem"]["intervals"][0]["start"])
        .date()
        .isoformat()
    )
    page.locator("#period-start").fill(seeded_day)
    page.locator("#period-end").fill(seeded_day)
    page.get_by_role("button", name="Show period").click()
    expect(page.locator("#evaluation-rows tr")).to_have_count(2)
    page.locator("summary").filter(has_text="Advice, realization and evaluation evidence").click()
    page.get_by_role("button", name="View original plan").first.click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    expect(page.locator("#plan-chart-legend")).to_contain_text("Garage battery")
    page.locator("#plan-chart-legend").get_by_label("Grid import: AC", exact=True).uncheck()
    page.locator("#plan-chart-legend").get_by_label("Garage battery: SoC", exact=True).uncheck()
    for chart in ("reference-chart", "self-consumption-chart"):
        page.locator(f"#{chart}-legend").get_by_label("Grid import: AC", exact=True).uncheck()
    open_primary_navigation(page)
    with page.expect_response("**/v1/savings?*") as savings_read:
        page.get_by_role("link", name="Savings", exact=True).click()
    savings_read.value.finished()
    wait_for_product_reads(page)
    for row in metadata["entities"]:
        if row["entity_id"] == "sensor.discharge":
            row["energy_name"] = "Renamed archive battery"
    assert page.request.post(url + "/v1/ha/catalogue", headers=headers, data=metadata).ok
    page.clock.fast_forward(31000)
    wait_for_product_reads(page)
    before_return = len(reads)
    if failed_refresh:
        page.route("**/comparison?view=plan", lambda route: route.fulfill(status=503))
    open_primary_navigation(page)
    page.get_by_role("link", name="Planning", exact=True).click()
    if failed_refresh:
        expect(page.locator("#overview-status")).to_contain_text("503")
        expect(page.locator("#plan-chart-legend")).to_contain_text("Garage battery")
        assert len(reads) == before_return
        page.unroute("**/comparison?view=plan")
        open_primary_navigation(page)
        with page.expect_response("**/v1/savings?*") as retry_savings_read:
            page.get_by_role("link", name="Savings", exact=True).click()
        retry_savings_read.value.finished()
        wait_for_product_reads(page)
        before_return = len(reads)
        open_primary_navigation(page)
        page.get_by_role("link", name="Planning", exact=True).click()
    expect(page.locator("#plan-chart-legend")).to_contain_text("Renamed archive battery")
    wait_for_product_reads(page)
    expect(page.locator("#plan-version")).to_have_value("browser-original-plan")
    expect(
        page.locator("#plan-chart-legend").get_by_label("Grid import: AC", exact=True)
    ).not_to_be_checked()
    expect(
        page.locator("#plan-chart-legend").get_by_label("Renamed archive battery: SoC", exact=True)
    ).not_to_be_checked()
    for chart in ("reference-chart", "self-consumption-chart"):
        expect(
            page.locator(f"#{chart}-legend").get_by_label("Grid import: AC", exact=True)
        ).not_to_be_checked()
    assert len(reads) == before_return
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
    expect(page.locator("#cost-rows tr").filter(has_text="Net electricity cost")).to_have_count(1)
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('energy-chart')).data.datasets.length"
        )
        == 3
    )
    page.locator("summary").filter(has_text="Advice, realization and evaluation evidence").click()
    page.get_by_role("button", name="View original plan").first.click()
    expect(page.locator("#plan-description")).to_contain_text("Minimize cost")
    expect(page.locator("#inspect-time")).to_have_attribute("max", "1")
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
        expect(page.locator("#savings-chart-range")).to_have_text("From 10/20/2026 to 10/23/2026")
        assert (
            page.locator("#savings-chart").evaluate(
                "canvas => Chart.getChart(canvas).options.scales.x.title.text"
            )
            == "Calendar date"
        )
