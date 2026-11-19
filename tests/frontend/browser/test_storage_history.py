"""Inspect daily stored capacity on desktop/mobile using real authenticated history routes."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from open_energy_planner.storage.adapters.lifecycle.schema import STORAGE_SAMPLES
from open_energy_planner.storage.adapters.usage import StorageUsage
from open_energy_planner.storage.adapters.usage_history import StorageHistory
from tests.frontend.browser.helpers import TOKEN, choose_period, discover, wait_for_product_reads


@pytest.fixture
def daily_storage_history(monkeypatch):
    """Seed an independent varying year with two recent gaps before real app composition."""
    original = StorageHistory.__init__
    instant = datetime(2026, 11, 18, 12, tzinfo=UTC)

    class Clock(datetime):
        """Keep the seeded dates and real storage routes aligned across test-run midnight."""

        @classmethod
        def now(cls, tz=None):
            """Use an independent fixed instant for both history and live capacity requests."""
            return instant

    monkeypatch.setattr("open_energy_planner.storage.api.datetime", Clock)

    def seeded(self, engine):
        """Populate only the fixture's newly created isolated app engine."""
        original(self, engine)
        today = instant.astimezone(ZoneInfo("Europe/Amsterdam")).date()
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
    usage = StorageUsage(
        "sqlite",
        database_bytes=710 * 1024**2,
        disk_total_bytes=100 * 1024**3,
        disk_free_bytes=15 * 1024**3,
    )
    monkeypatch.setattr(
        "open_energy_planner.storage.adapters.usage.storage_usage",
        lambda _engine: usage,
    )
    return usage


def test_daily_storage_chart_periods_keyboard_gaps_and_recovery(daily_storage_history, app_page):
    """Stored points drive periods and keyboard details while failures clear old evidence and recover."""
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    discover(page, url)
    page.goto(url + "/data-storage")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#storage-period").locator(".period-trigger")).to_be_enabled()
    expect(page.locator("#storage-history-summary")).to_contain_text("Average:")
    canvas = page.locator("#storage-history-chart")
    expect(canvas).to_be_visible()
    state = page.evaluate(
        """() => {const chart=Chart.getChart(document.getElementById('storage-history-chart'));return {points:chart.data.datasets[0].data,spanGaps:chart.data.datasets[0].spanGaps};}"""
    )
    assert 28 <= len(state["points"]) <= 32
    assert len([point for point in state["points"] if point["y"] is None]) == 2
    assert state["spanGaps"] is False
    assert state["points"][-1]["y"] == 680
    expect(page.locator("#database-size")).to_have_text("680 MiB")
    expect(page.locator("#storage-history-selected")).not_to_contain_text("Current:")
    expect(page.locator("#capacity-note")).to_contain_text("Low space means less than 1 GiB")
    assert "Historical evidence is retained" not in page.locator("main").inner_text()
    assert "Late starts record" not in page.locator("main").inner_text()
    canvas.focus()
    canvas.press("Home")
    first = page.locator("#storage-history-selected").inner_text()
    canvas.press("ArrowRight")
    assert page.locator("#storage-history-selected").inner_text() != first
    canvas.press("End")
    expect(page.locator("#storage-history-selected")).to_contain_text("SQLite")
    expect(canvas).to_have_accessible_description(
        page.locator("#storage-history-selected").text_content()
    )
    tooltip = page.evaluate(
        "() => {const tooltip=Chart.getChart(document.getElementById('storage-history-chart')).tooltip;return {opacity:tooltip.opacity,title:tooltip.title,label:tooltip.body[0].lines[0]};}"
    )
    assert tooltip["opacity"] == 1 and "680 MiB" in tooltip["label"]
    assert tooltip["title"] == ["11/18/2026, 4:00 AM"]
    selected_bounds = page.locator("#storage-history-selected").bounding_box()
    assert selected_bounds["width"] == 1 and selected_bounds["height"] == 1
    assert "Measured daily after" not in page.locator("main").inner_text()
    page.locator("#storage-history-title").click()
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/storage-history-{page.viewport_size['width']}.png", full_page=True
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("710 MiB")
    expect(page.locator("#storage-history-selected")).to_contain_text("Current:")
    expect(page.locator("#storage-history-selected")).to_contain_text("710 MiB")
    ranges = {
        "week": ("2026-11-12", "2026-11-18"),
        "quarter": ("2026-08-21", "2026-11-18"),
        "year": ("2025-11-19", "2026-11-18"),
        "all": ("2025-11-18", "2026-11-18"),
    }
    for period in ("week", "quarter", "year", "all"):
        with page.expect_response(
            lambda response, period=period: (
                "/v1/storage/history?start=" + ranges[period][0] in response.url
            )
        ) as loaded:
            choose_period(page, *ranges[period])
        rows = loaded.value.json()
        assert rows["samples"][-1]["database_bytes"] == 680 * 1024**2
        if period == "quarter":
            quarter_rows = rows
        expect(canvas).to_be_visible()
        if period == "week":
            assert len(rows["samples"]) == 7
        if period == "all":
            assert len(rows["samples"]) == 364
    delayed = []
    page.route("**/v1/storage/history?start=2026-08-21*", lambda route: delayed.append(route))
    with page.expect_request("**/v1/storage/history?start=2026-08-21*"):
        choose_period(page, "2026-08-21", "2026-11-18")
    choose_period(page, "2026-11-12", "2026-11-18")
    expect(page.locator("#storage-history-summary")).to_contain_text("Measurements: 7")
    delayed[0].fulfill(json=quarter_rows)
    wait_for_product_reads(page)
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.length"
        )
        == 7
    )
    page.unroute("**/v1/storage/history?start=2026-08-21*")
    page.route(
        "**/v1/storage/history?*", lambda route: route.fulfill(status=503, body="private-password")
    )
    choose_period(page, "2026-10-20", "2026-11-18")
    expect(page.locator("#storage-history-summary")).to_contain_text("Database history unavailable")
    expect(canvas).to_be_hidden()
    expect(page.locator("#storage")).to_be_visible()
    assert "private-password" not in page.locator("main").inner_text()
    page.unroute("**/v1/storage/history?*")
    daily_storage_history.database_bytes = int(724.5 * 1024**2)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(canvas).to_be_visible()
    expect(page.locator("#database-size")).to_have_text("724.5 MiB")
    expect(page.locator("#storage-history-selected")).to_contain_text("724.5 MiB")
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.at(-1).y"
        )
        == 724.5
    )
    page.reload()
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("680 MiB")
    expect(page.locator("#storage-history-selected")).not_to_contain_text("Current:")
    expect(page.locator("#storage-history-selected")).to_contain_text("680 MiB")
    assert not errors


def test_storage_history_empty_unknown_and_backend_changes(app_page):
    """Explain empty and unknown history without zeroes, then avoid comparing different size scopes."""
    page, url = app_page
    page.route(
        "**/v1/storage",
        lambda route: route.fulfill(
            json={
                "backend": "sqlite",
                "database_bytes": 8192,
                "observed_at": "2026-11-18T12:00:00Z",
            }
        ),
    )
    body = {
        "period": "month",
        "start": "2026-10-17",
        "end": "2026-11-17",
        "timezone": "Europe/Amsterdam",
        "sample_hour": 4,
        "samples": [],
    }
    body["timezone"] = None
    page.route(
        "**/v1/storage/history?*",
        lambda route: route.fulfill(
            json={**body, "latest": body["samples"][-1] if body["samples"] else None}
        ),
    )
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
    expect(page.locator("#storage-history-summary")).to_contain_text("Database size is unavailable")
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
    expect(page.locator("#storage-history-summary")).to_be_empty()
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
    body["samples"][0]["database_bytes"] = None
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-history-summary")).to_be_empty()
    page.locator("#storage-history-chart").focus()
    page.locator("#storage-history-chart").press("Home")
    expect(page.locator("#storage-history-chart")).to_have_accessible_description(
        page.locator("#storage-history-selected").text_content()
    )
    tooltip = page.evaluate(
        "() => {const chart=Chart.getChart(document.getElementById('storage-history-chart')),tooltip=chart.tooltip,index=chart.data.datasets[0].data.findIndex(point=>point.row?.database_bytes===null),point=chart.getDatasetMeta(0).data[index];return {opacity:tooltip.opacity,title:tooltip.title,label:tooltip.body[0].lines[0],finite:Number.isFinite(tooltip.x)&&Number.isFinite(tooltip.y),width:tooltip.width,height:tooltip.height,correctAnchor:tooltip.caretX===point.x&&tooltip.caretY===chart.chartArea.bottom,rawY:chart.data.datasets[0].data[index].y,hasValue:point.hasValue()};}"
    )
    assert "Unknown" in tooltip["label"]
    assert tooltip["title"] == ["11/15/2026, 4:00 AM"]
    assert tooltip["opacity"] == 1 and tooltip["finite"]
    assert tooltip["width"] > 0 and tooltip["height"] > 0
    assert tooltip["correctAnchor"]
    assert tooltip["rawY"] is None and tooltip["hasValue"]
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/storage-unknown-tooltip-{page.viewport_size['width']}.png", full_page=True
    )


def test_current_storage_point_uses_shared_measurement_and_installation_date(app_page):
    """Project live known and unknown bytes on the HA date without changing durable observations."""
    page, url = app_page
    usage = {
        "backend": "mysql",
        "database_bytes": 7 * 1024**2,
        "database_estimated": True,
        "observed_at": "2026-11-17T23:15:00Z",
        "disk_total_bytes": None,
        "disk_free_bytes": None,
    }
    daily = {
        "period": "month",
        "start": "2026-11-17",
        "end": "2026-11-18",
        "timezone": "Europe/Amsterdam",
        "sample_hour": 4,
        "samples": [
            {
                "day": "2026-11-17",
                "observed_at": "2026-11-17T03:00:00Z",
                "backend": "mysql",
                "database_bytes": 4 * 1024**2,
                "database_estimated": True,
            },
            {
                "day": "2026-11-18",
                "observed_at": "2026-11-18T03:00:00Z",
                "backend": "mysql",
                "database_bytes": 5 * 1024**2,
                "database_estimated": True,
            },
        ],
    }
    capacity_reads = []
    history_reads = []
    page.route(
        "**/v1/storage",
        lambda route: (capacity_reads.append(route.request), route.fulfill(json=usage)),
    )
    page.route(
        "**/v1/storage/history?*",
        lambda route: (
            history_reads.append(route.request.url),
            route.fulfill(
                json={
                    **daily,
                    "calendar": {"min_day": "2026-11-12", "max_day": daily["end"]},
                    "latest": daily["samples"][-1] if daily["samples"] else None,
                }
            ),
        ),
    )
    page.goto(url + "/data-storage")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("5 MiB (estimated)")
    expect(page.locator("#storage-history-selected")).not_to_contain_text("Current:")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("7 MiB (estimated)")
    expect(page.locator("#storage-history-selected")).to_have_text(
        "Current: 11/18/2026, 12:15 AM: 7 MiB (estimated): MySQL"
    )
    assert page.evaluate(
        "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.map(point => point.y)"
    ) == [4, 7]
    assert len(history_reads) >= 2
    assert all("period=month" in address for address in history_reads)
    daily["end"] = "2026-11-19"
    daily["samples"].append(
        {
            **daily["samples"][-1],
            "day": "2026-11-19",
            "observed_at": "2026-11-19T03:00:00Z",
            "database_bytes": 6 * 1024**2,
        }
    )
    choose_period(page, "2026-11-12", "2026-11-18")
    expect(page.locator("#storage-history-selected")).to_contain_text("7 MiB (estimated)")
    wait_for_product_reads(page)
    assert len(capacity_reads) == 2
    assert page.evaluate(
        "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.map(point => point.y)"
    ) == [4, 7, None]
    usage.update(database_bytes=8 * 1024**2, observed_at="2026-11-19T11:15:00Z")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("8 MiB (estimated)")
    expect(page.locator("#storage-history-selected")).to_have_text(
        "Current: 11/19/2026, 12:15 PM: 8 MiB (estimated): MySQL"
    )
    assert page.evaluate(
        "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.map(point => point.y)"
    ) == [4, 5, 8]
    usage["database_bytes"] = None
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("Unknown (estimated)")
    expect(page.locator("#storage-history-selected")).to_contain_text(
        "Current: 11/19/2026, 12:15 PM: Unknown (estimated): MySQL"
    )
    assert page.evaluate(
        "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.map(point => point.y)"
    ) == [4, 5, None]
    daily["samples"] = []
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-history-summary")).to_contain_text("Database size is unavailable")
    expect(page.get_by_label("Observation", exact=True)).to_be_visible()
    expect(page.locator("#storage-history-chart")).to_be_hidden()
    usage["database_bytes"] = 7 * 1024**2
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#storage-history-chart")).to_be_visible()
    expect(page.locator("#storage-history-summary")).to_be_empty()
    expect(page.locator("#storage-history-selected")).to_contain_text("7 MiB (estimated)")
    page.reload()
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("Unknown (estimated)")
    expect(page.locator("#storage-history-chart")).to_be_hidden()
    expect(page.locator("#storage-history-summary")).to_contain_text(
        "No database size measurements"
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("7 MiB (estimated)")
    daily.update(period="all", start="2026-11-20", end="2026-11-20")
    choose_period(page, "2026-11-18", "2026-11-18")
    expect(page.locator("#storage-history-selected")).to_contain_text(
        "Current: 11/19/2026, 12:15 PM: 7 MiB"
    )
    assert page.evaluate(
        "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.map(point => point.y)"
    ) == [7, None]
    daily.update(
        period="week",
        start="2026-11-21",
        end="2026-11-27",
        samples=[
            {
                "day": "2026-11-27",
                "observed_at": "2026-11-27T03:00:00Z",
                "backend": "mysql",
                "database_bytes": 6 * 1024**2,
                "database_estimated": True,
            }
        ],
    )
    reads_before_period_change = len(capacity_reads)
    choose_period(page, "2026-11-12", "2026-11-18")
    expect(page.locator("#storage-history-summary")).to_contain_text(
        "No database size measurements"
    )
    expect(page.locator("#storage-history-chart")).to_be_hidden()
    expect(page.locator("#database-size")).to_have_text("7 MiB (estimated)")
    assert len(capacity_reads) == reads_before_period_change
    usage.update(database_bytes=9 * 1024**2, observed_at="2026-11-27T12:00:00Z")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#database-size")).to_have_text("9 MiB (estimated)")
    expect(page.locator("#storage-history-selected")).to_have_text(
        "Current: 11/27/2026, 1:00 PM: 9 MiB (estimated): MySQL"
    )
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('storage-history-chart')).data.datasets[0].data.at(-1).y"
        )
        == 9
    )
