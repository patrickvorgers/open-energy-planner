"""Inspect daily stored capacity on desktop/mobile using real authenticated history routes."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from open_energy_planner.storage.adapters.lifecycle.schema import STORAGE_SAMPLES
from open_energy_planner.storage.adapters.usage import StorageUsage
from open_energy_planner.storage.adapters.usage_history import StorageHistory
from tests.frontend.browser.helpers import TOKEN, discover, wait_for_product_reads


@pytest.fixture
def daily_storage_history(monkeypatch):
    """Seed an independent varying year with two recent gaps before real app composition."""
    original = StorageHistory.__init__

    def seeded(self, engine):
        """Populate only the fixture's newly created isolated app engine."""
        original(self, engine)
        today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
        rows = []
        for offset in range(366):
            day = today - timedelta(days=365 - offset)
            if (today - day).days in (8, 9):
                continue
            observed = datetime(
                day.year, day.month, day.day, 4, tzinfo=ZoneInfo("Europe/Amsterdam")
            )
            rows.append(
                {
                    "day": day.isoformat(),
                    "observed_at": observed.astimezone(UTC).isoformat(timespec="microseconds"),
                    "timezone": "Europe/Amsterdam",
                    "backend": "sqlite",
                    "database_bytes": (300 + offset + (offset % 6) * 3) * 1024**2,
                    "database_estimated": False,
                }
            )
        with engine.begin() as connection:
            connection.execute(STORAGE_SAMPLES.insert(), rows)

    monkeypatch.setattr(StorageHistory, "__init__", seeded)
    monkeypatch.setattr(
        "open_energy_planner.storage.adapters.usage.storage_usage",
        lambda _engine: StorageUsage(
            "sqlite",
            database_bytes=680 * 1024**2,
            disk_total_bytes=100 * 1024**3,
            disk_free_bytes=15 * 1024**3,
        ),
    )


def test_daily_storage_chart_periods_keyboard_gaps_and_recovery(daily_storage_history, app_page):
    """Stored points drive periods and keyboard details while failures clear old evidence and recover."""
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    discover(page, url)
    page.goto(url + "/data-storage")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#storage-period")).to_have_value("month")
    expect(page.locator("#storage-history-summary")).to_contain_text("Average:")
    canvas = page.locator("#storage-history-chart")
    expect(canvas).to_be_visible()
    state = page.evaluate(
        """() => {const chart=Chart.getChart(document.getElementById('storage-history-chart'));return {points:chart.data.datasets[0].data,spanGaps:chart.data.datasets[0].spanGaps};}"""
    )
    assert 28 <= len(state["points"]) <= 32
    assert len([point for point in state["points"] if point["y"] is None]) == 2
    assert state["spanGaps"] is False
    canvas.focus()
    canvas.press("Home")
    first = page.locator("#storage-history-selected").inner_text()
    canvas.press("ArrowRight")
    assert page.locator("#storage-history-selected").inner_text() != first
    canvas.press("End")
    expect(page.locator("#storage-history-selected")).to_contain_text("SQLite")
    page.locator("#storage-history-title").click()
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/storage-history-{page.viewport_size['width']}.png", full_page=True
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for period in ("week", "quarter", "year", "all"):
        with page.expect_response(
            lambda response, period=period: "/v1/storage/history?period=" + period in response.url
        ) as loaded:
            page.locator("#storage-period").select_option(period)
        rows = loaded.value.json()
        if period == "quarter":
            quarter_rows = rows
        expect(canvas).to_be_visible()
        if period == "week":
            assert len(rows["samples"]) == 7
        if period == "all":
            assert len(rows["samples"]) == 364
    delayed = []
    page.route("**/v1/storage/history?period=quarter", lambda route: delayed.append(route))
    with page.expect_request("**/v1/storage/history?period=quarter"):
        page.locator("#storage-period").select_option("quarter")
    page.locator("#storage-period").select_option("week")
    expect(page.locator("#storage-history-summary")).to_contain_text("Measurements: 7")
    delayed[0].fulfill(json=quarter_rows)
    wait_for_product_reads(page)
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.length"
        )
        == 7
    )
    page.unroute("**/v1/storage/history?period=quarter")
    page.route(
        "**/v1/storage/history?*", lambda route: route.fulfill(status=503, body="private-password")
    )
    page.locator("#storage-period").select_option("month")
    expect(page.locator("#storage-history-summary")).to_contain_text("Database history unavailable")
    expect(canvas).to_be_hidden()
    expect(page.locator("#storage")).to_be_visible()
    assert "private-password" not in page.locator("main").inner_text()
    page.unroute("**/v1/storage/history?*")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(canvas).to_be_visible()
    assert not errors


def test_storage_history_empty_unknown_and_backend_changes(app_page):
    """Explain empty and unknown history without zeroes, then avoid comparing different size scopes."""
    page, url = app_page
    body = {
        "period": "month",
        "start": "2026-10-17",
        "end": "2026-11-17",
        "timezone": "Europe/Amsterdam",
        "sample_hour": 4,
        "samples": [],
    }
    body["timezone"] = None
    page.route("**/v1/storage/history?*", lambda route: route.fulfill(json=body))
    page.goto(url + "/data-storage")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#storage-history-summary")).to_contain_text(
        "Waiting for the Home Assistant installation timezone"
    )
    body["timezone"] = "Europe/Amsterdam"
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-history-summary")).to_contain_text(
        "No database size measurements"
    )
    body["samples"] = [
        {
            "day": "2026-11-14",
            "observed_at": "2026-11-14T03:00:00Z",
            "timezone": "Europe/Amsterdam",
            "backend": "sqlite",
            "database_bytes": None,
            "database_estimated": False,
        },
        {
            "day": "2026-11-15",
            "observed_at": "2026-11-15T03:00:00Z",
            "timezone": "Europe/Amsterdam",
            "backend": "mysql",
            "database_bytes": None,
            "database_estimated": True,
        },
    ]
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-history-summary")).to_contain_text(
        "database size was unavailable"
    )
    expect(page.locator("#storage-history-chart")).to_be_hidden()
    observation = page.get_by_label("Observation", exact=True)
    expect(observation).to_be_visible()
    expect(page.locator("#storage-history-selected")).to_contain_text("Unknown (estimated): MySQL")
    observation.focus()
    observation.press("ArrowUp")
    expect(page.locator("#storage-history-selected")).to_contain_text("Unknown: SQLite")
    expect(page.locator("#storage-history-selected")).to_contain_text("11/14/2026, 4:00 AM")
    observation.press("ArrowDown")
    expect(page.locator("#storage-history-selected")).to_contain_text("Unknown (estimated): MySQL")
    page.locator("#storage-history-title").click()
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/storage-history-unknown-{page.viewport_size['width']}.png", full_page=True
    )
    body["samples"] = [body["samples"][-1]]
    body["samples"][0].update(
        database_bytes=512 * 1024**2, backend="sqlite", database_estimated=False
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-history-summary")).to_contain_text("At least two measurements")
    expect(observation).to_be_hidden()
    body["samples"].append(
        {
            **body["samples"][0],
            "day": "2026-11-16",
            "observed_at": "2026-11-16T03:00:00Z",
            "backend": "sqlite",
            "database_bytes": 400 * 1024**2,
            "database_estimated": False,
        }
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-history-summary")).to_contain_text("Decrease: 112 MiB")
    expect(page.locator("#storage-history-summary")).to_contain_text("Average: −112 MiB per day")
    expect(page.locator("#storage-history-summary")).to_have_text(
        "10/17/2026 – 11/17/2026: Measurements: 2: Decrease: 112 MiB: Average: −112 MiB per day"
    )
    body["samples"][-1].update(backend="mysql", database_estimated=True)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-history-summary")).to_contain_text(
        "not compared across different"
    )
    expect(page.locator("#storage-history-selected")).to_contain_text("estimated")
    expect(page.locator("#storage-history-chart")).to_be_visible()
    page.locator("#storage-history-chart").focus()
    expect(page.locator("#storage-history-chart")).to_be_focused()
    page.locator("#storage-history-chart").press("Home")
    expect(page.locator("#storage-history-selected")).to_contain_text("SQLite")
    assert page.evaluate(
        "Chart.getChart(document.getElementById('storage-history-chart')).getDatasetMeta(0).dataset.segments.some(segment => segment.style?.borderColor === 'transparent')"
    )
