"""Daily chart bounds retain installation-local days and original numeric evidence."""

import re
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
@pytest.mark.parametrize(
    "day,hours,timezone",
    [
        ("2026-02-12", 24, "Europe/Amsterdam"),
        ("2026-03-29", 23, "Europe/Amsterdam"),
        ("2026-10-25", 25, "Europe/Amsterdam"),
        ("2026-09-05", 24, "America/Santiago"),
        ("2026-09-06", 23, "America/Santiago"),
        ("2026-04-05", 24.5, "Australia/Lord_Howe"),
    ],
)
def test_daily_bounds_and_optional_calculations(app_page, day, hours, timezone):
    """Both plots fill an HA day across DST while optional runs preserve their values.

    Discovery defaults to Europe/Amsterdam. Explicit overrides cover a missing
    local midnight and half-hour DST. Three calculation rows and synthetic
    UTC-hourly quality rows preserve their original timestamps and values.
    """
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    discover(page, url, timezone=timezone)
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    end = start + timedelta(hours=hours)
    fixture_day = datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
    evidence = page.request.get(
        url + f"/v1/planning-performance?day={fixture_day}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    evidence["day"] = day
    evidence["timezone"] = timezone
    for row, minutes in zip(evidence["calculations"], (480, 485, 495), strict=True):
        row["started_at"] = (start + timedelta(minutes=minutes)).isoformat()
    page.route("**/v1/planning-performance**", lambda route: route.fulfill(json=evidence))
    page.goto(url + "/planning-performance")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator(".performance-row")).to_have_count(3)
    expect(page.locator("#performance-status")).to_have_text("")
    extra = page.get_by_label("Show additional calculations between quarter hours")
    expect(extra).not_to_be_checked()
    canvas = page.locator("#performance-chart")
    assert_bounds(canvas, start, end)
    values = canvas.evaluate(
        "canvas => Chart.getChart(canvas).data.datasets[0].data.map(point=>point.y)"
    )
    assert values == [4, None]
    geometry = canvas.evaluate("""canvas => {const chart=Chart.getChart(canvas);return {
        width:chart.getDatasetMeta(0).data[0].width,
        expected:(chart.chartArea.right-chart.chartArea.left)*0.25/((chart.scales.x.max-chart.scales.x.min)/3600000)*0.9,
        colors:chart.data.datasets.slice(0,3).map(dataset=>dataset.backgroundColor)
    }}""")
    assert geometry["width"] == pytest.approx(geometry["expected"])
    assert len(set(geometry["colors"])) == 3
    for color in geometry["colors"]:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        assert (
            1.05
            / (
                sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))
                + 0.05
            )
            >= 3
        )
    extra.check()
    assert canvas.evaluate(
        "canvas => Chart.getChart(canvas).data.datasets[0].data.map(point=>point.y)"
    ) == [4, 12, None]
    page.get_by_label("First candidate observed", exact=True).uncheck()
    assert canvas.evaluate("canvas => Chart.getChart(canvas).isDatasetVisible(0)") is False
    assert_bounds(canvas, start, end)
    capture = Path("results/ci/daily-charts")
    capture.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    page.screenshot(path=str(capture / f"performance-extra-{day}-{width}.png"), full_page=True)
    extra.uncheck()
    page.screenshot(path=str(capture / f"performance-default-{day}-{width}.png"), full_page=True)

    rows = [
        {
            "start": (start + timedelta(hours=index)).isoformat(),
            "end": (start + timedelta(hours=index + 1)).isoformat(),
            "predicted_kwh": 1,
            "measured_kwh": None,
            "error_kwh": None,
            "evaluation_issue": None,
        }
        for index in range(ceil(hours))
    ]
    source = {
        "id": "household",
        "title": "Synthetic household",
        "daily": {
            "predicted_kwh": hours,
            "measured_kwh": None,
            "error_kwh": None,
            "evaluation_issue": None,
        },
        "coverage": {"hours": ceil(hours), "measured_hours": 0, "paired_hours": 0},
        "rows": rows,
        "metrics": {},
        "issued_at": start.isoformat(),
        "model_label": "Synthetic",
        "forecast_version": "synthetic",
    }
    body = {
        "day": day,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "as_of": end.isoformat(),
        "captured_at": start.isoformat(),
        "timezone": timezone,
        "issues": [],
        "series": [source],
        "current_models": {},
    }
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(json={"days": [day], "next_before": None}),
    )
    page.route("**/v1/forecast-quality/days/*", lambda route: route.fulfill(json=body))
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(json={"models": [], "explanation": "Synthetic", "issue": None}),
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#quality-chart-0")).to_be_visible()
    canvas = page.locator("#quality-chart-0")
    assert_bounds(canvas, start, end)
    points = canvas.evaluate("canvas => Chart.getChart(canvas).data.datasets[0].data")
    assert points[0] == {"x": (start + timedelta(minutes=30)).timestamp() * 1000, "y": 1}
    last_start = start + timedelta(hours=ceil(hours) - 1)
    last_end = last_start + timedelta(hours=1)
    assert points[-1]["x"] == (last_start + timedelta(minutes=30)).timestamp() * 1000
    assert len(points) == ceil(hours)
    geometry = canvas.evaluate("""canvas => {
        const chart=Chart.getChart(canvas);
        return chart.getDatasetMeta(0).data.map(bar=>({
            left:bar.x-bar.width/2,right:bar.x+bar.width/2,width:bar.width,
            minimum:chart.chartArea.left,maximum:chart.chartArea.right
        }));
    }""")
    hourly_width = (geometry[0]["maximum"] - geometry[0]["minimum"]) / hours * 0.8 * 0.9 / 2
    for index, bar in enumerate(geometry):
        visible_hours = min(1, hours - index)
        assert bar["width"] == pytest.approx(hourly_width * visible_hours)
        assert bar["left"] >= bar["minimum"] - 0.01
        assert bar["right"] <= bar["maximum"] + 0.01
    if hours != int(hours):
        expected_title = page.evaluate(
            """async ({start,end}) => {
                const {when}=await import('/setup/display.js');
                return `${when(start)} to ${when(end,{hour:'2-digit',minute:'2-digit'})}`;
            }""",
            {"start": last_start.isoformat(), "end": last_end.isoformat()},
        )
        canvas.evaluate("""canvas => {
            const chart=Chart.getChart(canvas),index=chart.data.datasets[0].data.length-1;
            chart.tooltip.setActiveElements([{datasetIndex:0,index}],{x:0,y:0});chart.update();
        }""")
        assert canvas.evaluate("canvas => Chart.getChart(canvas).tooltip.title") == [expected_title]
    assert not errors
    page.screenshot(path=str(capture / f"quality-{day}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def assert_bounds(canvas, start, end):
    """Check timestamps and actual edge pixels, including Chart.js bar offsets."""
    bounds = canvas.evaluate("""canvas => {const chart=Chart.getChart(canvas), x=chart.scales.x;return {
        min:x.min,max:x.max,left:chart.chartArea.left,right:chart.chartArea.right,
        first:x.getPixelForValue(x.min),last:x.getPixelForValue(x.max)
    }}""")
    assert bounds["min"] == start.timestamp() * 1000
    assert bounds["max"] == end.timestamp() * 1000
    assert bounds["first"] == pytest.approx(bounds["left"])
    assert bounds["last"] == pytest.approx(bounds["right"])


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_skipped_calendar_date_remains_responsive(app_page):
    """Apia skipped 2011-12-30 entirely, so no zero-duration chart or tick loop may be created."""
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    discover(page, url, timezone="Pacific/Apia")
    page.route(
        "**/v1/planning-performance**",
        lambda route: route.fulfill(
            json={"day": "2011-12-30", "timezone": "Pacific/Apia", "calculations": []}
        ),
    )
    page.goto(url + "/planning-performance?day=2011-12-30")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    summary = page.locator("#performance-summary")
    expect(summary).to_have_text(
        "The selected calendar day does not exist in the Home Assistant installation timezone."
    )
    expect(page.locator("#performance-status")).to_have_text("")
    page.get_by_label("Show additional calculations between quarter hours").check()
    expect(summary).to_have_text(
        "The selected calendar day does not exist in the Home Assistant installation timezone."
    )
    assert page.locator("#performance-chart").evaluate("canvas => !Chart.getChart(canvas)")
    expect(page.locator(".performance-row")).to_have_count(0)
    assert not errors


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_repeated_local_calculation_times_are_distinct(app_page):
    """Amsterdam's two 02:00 minutes stay distinguishable in list, actions, dialog and chart tooltip."""
    page, url = app_page
    discover(page, url)
    evidence = page.request.get(
        url + "/v1/planning-performance", headers={"Authorization": f"Bearer {TOKEN}"}
    ).json()
    evidence["day"] = "2026-10-25"
    starts = ["2026-10-25T00:00:00Z", "2026-10-25T01:00:00Z", "2026-10-25T08:00:00Z"]
    for row, started in zip(evidence["calculations"], starts, strict=True):
        row["started_at"] = started
    details = {}
    for row in evidence["calculations"]:
        detail = page.request.get(
            url + "/v1/planning-performance/" + row["id"],
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).json()
        detail["started_at"] = row["started_at"]
        details[row["id"]] = detail

    def respond(route):
        """Keep the synthetic list and detail instants consistent."""
        route.fulfill(json=details.get(route.request.url.rsplit("/", 1)[-1], evidence))

    page.route("**/v1/planning-performance**", respond)
    page.goto(url + "/planning-performance")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator(".performance-row")).to_have_count(3)
    first = page.get_by_role("button", name=re.compile(r"^View calculation at .*\(1 of 2\)$"))
    second = page.get_by_role("button", name=re.compile(r"^View calculation at .*\(2 of 2\)$"))
    expect(first).to_be_visible()
    expect(second).to_be_visible()
    expect(page.locator(".performance-row").nth(0)).to_contain_text("(1 of 2)")
    expect(page.locator(".performance-row").nth(1)).to_contain_text("(2 of 2)")
    canvas = page.locator("#performance-chart")
    for index, suffix in ((0, "(1 of 2)"), (1, "(2 of 2)")):
        canvas.evaluate(
            """(canvas,index) => {
            const chart=Chart.getChart(canvas);
            chart.tooltip.setActiveElements([{datasetIndex:0,index}],{x:0,y:0});chart.update();
        }""",
            index,
        )
        assert canvas.evaluate("canvas => Chart.getChart(canvas).tooltip.title[0]").endswith(suffix)
    first.click()
    expect(page.locator("#calculation-time")).to_contain_text("(1 of 2)")
    page.get_by_role("button", name="Close", exact=True).click()
    second.click()
    expect(page.locator("#calculation-time")).to_contain_text("(2 of 2)")
