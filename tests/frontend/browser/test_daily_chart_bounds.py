"""Daily chart bounds retain installation-local days and original numeric evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
@pytest.mark.parametrize("day,hours", [("2026-02-12", 24), ("2026-03-29", 23), ("2026-10-25", 25)])
def test_daily_bounds_and_optional_calculations(app_page, day, hours):
    """Both plots fill a complete HA day across DST while optional runs preserve their values."""
    page, url = app_page
    discover(page, url)
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Europe/Amsterdam")).astimezone(UTC)
    end = start + timedelta(hours=hours)
    evidence = page.request.get(
        url + "/v1/planning-performance", headers={"Authorization": f"Bearer {TOKEN}"}
    ).json()
    evidence["day"] = day
    for row, minutes in zip(evidence["calculations"], (480, 485, 495), strict=True):
        row["started_at"] = (start + timedelta(minutes=minutes)).isoformat()
    page.route("**/v1/planning-performance**", lambda route: route.fulfill(json=evidence))
    page.goto(url + "/planning-performance")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator(".performance-row")).to_have_count(3)
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
        for index in range(hours)
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
        "coverage": {"hours": hours, "measured_hours": 0, "paired_hours": 0},
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
        "timezone": "Europe/Amsterdam",
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
    assert points[-1]["x"] == (end - timedelta(minutes=30)).timestamp() * 1000
    assert len(points) == hours
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
