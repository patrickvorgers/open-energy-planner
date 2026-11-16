"""Interval forecasts fill actual coverage without moving evidence or concealing missing hours."""

from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize(
    "day,hours,timezone",
    [
        ("2026-02-12", 24, "Europe/Amsterdam"),
        ("2026-03-29", 23, "Europe/Amsterdam"),
        ("2026-10-25", 25, "Europe/Amsterdam"),
        ("2026-04-05", 24.5, "Australia/Lord_Howe"),
    ],
)
def test_forecast_strokes_cover_intervals_and_preserve_real_gaps(app_page, day, hours, timezone):
    """Actual canvas strokes reach both day edges, including a clipped half hour, but never span a gap."""
    page, url = app_page
    discover(page, url, timezone=timezone)
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    end = start + timedelta(hours=hours)
    rows = [
        {
            "start": (start + timedelta(hours=index)).isoformat(),
            "end": (start + timedelta(hours=index + 1)).isoformat(),
            "predicted_kwh": None if index == 8 else round(0.3 + (index % 6) * 0.08, 2),
            "measured_kwh": None,
            "error_kwh": None,
            "evaluation_issue": "Synthetic missing measurement",
        }
        for index in range(ceil(hours))
        if index != 12
    ]
    source = {
        "id": "household",
        "title": "Synthetic household consumption",
        "model": "synthetic",
        "model_label": "Original hourly forecast",
        "forecast_version": "synthetic",
        "issued_at": start.isoformat(),
        "rows": rows,
        "daily": {"predicted_kwh": 11.2, "measured_kwh": None, "error_kwh": None},
        "coverage": {"hours": ceil(hours), "measured_hours": 0, "paired_hours": 0},
        "metrics": {},
    }
    body = {
        "day": day,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "as_of": end.isoformat(),
        "timezone": timezone,
        "issues": [],
        "series": [source],
        "current_models": {},
    }
    page.route("**/v1/forecast-quality/days", lambda route: route.fulfill(json={"days": [day]}))
    page.route("**/v1/forecast-quality/days/*", lambda route: route.fulfill(json=body))
    page.route("**/v1/models", lambda route: route.fulfill(json={"models": [], "issue": None}))
    page.goto(url + "/quality")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    canvas = page.locator("#quality-chart-0")
    expect(canvas).to_be_visible()
    samples = [
        (0.03, rows[0]["predicted_kwh"], True),
        (hours - 0.03, rows[-1]["predicted_kwh"], True),
        (8.5, None, False),
        (12.5, None, False),
    ]
    for hour, value, expected in samples:
        assert forecast_ink(canvas, start + timedelta(hours=hour), value) is expected
    points = canvas.evaluate("canvas => Chart.getChart(canvas).data.datasets[1].data")
    assert points == [
        {
            "x": (
                datetime.fromisoformat(row["start"]).timestamp()
                + datetime.fromisoformat(row["end"]).timestamp()
            )
            * 500,
            "y": row["predicted_kwh"],
        }
        for row in rows
    ]
    forecast = page.get_by_role(
        "checkbox", name="Original hourly forecast: Original choice", exact=True
    )
    forecast.uncheck()
    assert not forecast_ink(canvas, start + timedelta(minutes=2), rows[0]["predicted_kwh"])
    forecast.check()
    assert forecast_ink(canvas, start + timedelta(minutes=2), rows[0]["predicted_kwh"])
    capture = Path("results/ci/chart-edges")
    capture.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(capture / f"quality-{day}-{page.viewport_size['width']}.png"), full_page=True
    )
    plot = page.locator("#quality-charts .chart-plot")
    plot.evaluate("element => element.scrollIntoView({block: 'center'})")
    plot.screenshot(path=str(capture / f"quality-plot-{day}-{page.viewport_size['width']}.png"))
    source["rows"] = [
        {**row, "predicted_kwh": None if index in (0, len(rows) - 1) else row["predicted_kwh"]}
        for index, row in enumerate(rows)
    ]
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    page.wait_for_function(
        "() => Chart.getChart('quality-chart-0')?.data.datasets[1].data[0].y === null"
    )
    assert not forecast_ink(canvas, start + timedelta(minutes=2), None)
    assert not forecast_ink(canvas, end - timedelta(minutes=2), None)
    source["rows"] = [rows[2]]
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    page.wait_for_function(
        "() => Chart.getChart('quality-chart-0')?.data.datasets[1].data.length === 1"
    )
    assert forecast_ink(canvas, start + timedelta(hours=2, minutes=2), rows[2]["predicted_kwh"])
    assert forecast_ink(canvas, start + timedelta(hours=2, minutes=58), rows[2]["predicted_kwh"])
    assert not forecast_ink(canvas, start + timedelta(hours=1, minutes=30), None)
    assert not forecast_ink(canvas, start + timedelta(hours=3, minutes=30), None)
    source["alternatives"] = [
        {
            "model": "pvwatts",
            "model_label": "Independent alternative",
            "metrics": {},
            "daily": {},
            "rows": [
                {
                    "start": (end - timedelta(minutes=30)).isoformat(),
                    "end": (end + timedelta(minutes=30)).isoformat(),
                    "predicted_kwh": 0.6,
                }
            ],
        }
    ]
    source["radiation"] = {
        "rows": [
            {
                "start": (start - timedelta(minutes=30)).isoformat(),
                "end": (start + timedelta(minutes=30)).isoformat(),
                "forecast_w_m2": 200,
                "satellite_w_m2": None,
            }
        ],
        "observed_hours": 0,
        "hours": 1,
        "source": "Synthetic independent reference",
    }
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    page.wait_for_function("() => Chart.getChart('quality-chart-0')?.data.datasets.length === 5")
    page.get_by_role("checkbox", name="Forecast irradiance", exact=True).check()
    assert any(
        forecast_ink(canvas, end - timedelta(minutes=minutes), 0.6, color=(181, 136, 0))
        for minutes in (2, 8, 15, 22)
    )
    assert not forecast_ink(canvas, start + timedelta(hours=1), None, color=(181, 136, 0))
    assert any(
        forecast_ink(
            canvas, start + timedelta(minutes=minutes), 200, color=(183, 92, 0), axis="radiation"
        )
        for minutes in (2, 8, 15, 22)
    )
    assert not forecast_ink(canvas, end - timedelta(hours=1), None, color=(183, 92, 0))
    source.pop("alternatives")
    source.pop("radiation")
    source["rows"] = [
        {
            **rows[0],
            "start": (start - timedelta(minutes=45)).isoformat(),
            "end": (start + timedelta(minutes=15)).isoformat(),
            "predicted_kwh": 6,
        }
    ]
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    page.wait_for_function(
        "() => Chart.getChart('quality-chart-0')?.data.datasets[1].data[0].y === 6"
    )
    assert forecast_ink(canvas, start + timedelta(minutes=5), 6)
    assert not forecast_ink(canvas, start + timedelta(minutes=45), None)


def forecast_ink(canvas, instant, value, *, color=(0, 109, 166), axis="y"):
    """Inspect native canvas pixels near an independently chosen timestamp for the forecast's blue stroke."""
    return canvas.evaluate(
        """(canvas, {time, value, color, axis}) => {
        const chart = Chart.getChart(canvas), ratio = chart.currentDevicePixelRatio;
        const x = Math.round(chart.scales.x.getPixelForValue(time) * ratio);
        const y = value == null ? Math.ceil(chart.chartArea.top * ratio)
            : Math.round(chart.scales[axis].getPixelForValue(value) * ratio) - 3;
        const height = value == null ? Math.floor((chart.chartArea.bottom - chart.chartArea.top) * ratio) : 7;
        const pixels = chart.ctx.getImageData(x - 1, y, 3, height).data;
        for (let index = 0; index < pixels.length; index += 4) {
            if (color.every((channel, offset) => Math.abs(pixels[index + offset] - channel) < 30)
                && pixels[index + 3] > 150) return true;
        }
        return false;
    }""",
        {"time": instant.timestamp() * 1000, "value": value, "color": color, "axis": axis},
    )
