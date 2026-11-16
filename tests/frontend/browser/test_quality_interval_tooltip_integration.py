"""Independent interval geometry must select matching tooltip evidence, never matching array offsets."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize(
    "app_page", [{"width": 1280, "height": 1000}, {"width": 390, "height": 844}], indirect=True
)
def test_independent_intervals_keep_native_indices_and_select_matching_evidence(app_page):
    """A one-hour hole, shifted alternative and independent radiation interval cannot borrow another hour."""
    page, url = app_page
    discover(page, url, timezone="UTC")
    start = datetime(2026, 10, 26, tzinfo=UTC)

    def row(hour, prediction, measured=None, error=None):
        """Provide independently specified interval values and original boundaries."""
        return {
            "start": (start + timedelta(hours=hour)).isoformat(),
            "end": (start + timedelta(hours=hour + 1)).isoformat(),
            "predicted_kwh": prediction,
            "measured_kwh": measured,
            "error_kwh": error,
            "evaluation_issue": None,
        }

    source = {
        "id": "household",
        "title": "Synthetic independent intervals",
        "model": "synthetic",
        "model_label": "Original hourly forecast",
        "forecast_version": "original",
        "issued_at": (start - timedelta(hours=1)).isoformat(),
        "rows": [row(0, 1, 0.4, 0.6), row(2, 3, 0.7, 2.3)],
        "coverage": {"hours": 24, "measured_hours": 2, "paired_hours": 2},
        "daily": {"predicted_kwh": None, "measured_kwh": None, "error_kwh": None},
        "metrics": {},
        "alternatives": [
            {
                "model": "pvwatts",
                "model_label": "Independent alternative",
                "forecast_version": "alternative",
                "issued_at": (start - timedelta(hours=1)).isoformat(),
                "rows": [row(1, 9, 0.5, 8.5), row(2, 4, 0.7, 3.3)],
                "daily": {},
                "metrics": {},
            }
        ],
        "radiation": {
            "rows": [{**row(3, None), "forecast_w_m2": 400, "satellite_w_m2": 380}],
            "observed_hours": 1,
            "hours": 24,
            "source": "Synthetic independent reference",
        },
    }
    original = deepcopy(source)
    body = {
        "day": "2026-10-26",
        "start": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
        "as_of": (start + timedelta(days=2)).isoformat(),
        "timezone": "UTC",
        "issues": [],
        "series": [source],
        "current_models": {},
    }
    page.route(
        "**/v1/forecast-quality/days", lambda route: route.fulfill(json={"days": [body["day"]]})
    )
    page.route("**/v1/forecast-quality/days/*", lambda route: route.fulfill(json=body))
    page.route("**/v1/models", lambda route: route.fulfill(json={"models": [], "issue": None}))
    page.goto(url + "/quality")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    canvas = page.locator("#quality-chart-0")
    evidence = page.get_by_role("region", name="Hourly forecast evidence", exact=True)

    def keyboard_focus(target, direction="Tab"):
        """Reach a chart or legend control through actual keyboard navigation."""
        for _ in range(40):
            if target.evaluate("element => element === document.activeElement"):
                return
            page.keyboard.press(direction)
        raise AssertionError("Keyboard did not reach the requested control")

    def toggle_series(name, checked):
        """Toggle one legend checkbox without pointer events refreshing the tooltip."""
        control = page.get_by_role("checkbox", name=name, exact=True)
        keyboard_focus(control)
        page.keyboard.press("Space")
        expect(control).to_be_checked(checked=checked)

    canvas.focus()
    canvas.press("Home")
    expect(evidence).to_contain_text("Original hourly forecast: Original choice: 1.000 kWh")
    canvas.press("End")
    expect(evidence).to_contain_text("Independent alternative: 4.000 kWh")
    assert (
        canvas.evaluate("canvas => Chart.getChart(canvas).tooltip.getActiveElements().length") == 0
    )
    toggle_series("Independent alternative", False)
    expect(evidence).not_to_contain_text("Independent alternative:")
    expect(evidence).to_contain_text("Original hourly forecast: Original choice: 3.000 kWh")
    toggle_series("Independent alternative", True)
    expect(evidence).to_contain_text("Independent alternative: 4.000 kWh")
    toggle_series("Forecast irradiance", True)
    expect(evidence).to_contain_text("Forecast irradiance: Unavailable W/m²")
    expect(evidence).not_to_contain_text("Forecast irradiance: 400.000 W/m²")
    keyboard_focus(canvas, "Shift+Tab")
    canvas.press("End")
    expect(evidence).to_contain_text("Forecast irradiance: 400.000 W/m²")
    toggle_series("Forecast irradiance", False)
    expect(evidence).to_be_hidden()
    toggle_series("Forecast irradiance", True)
    expect(evidence).to_contain_text("Forecast irradiance: 400.000 W/m²")
    for name in (
        "Measured",
        "Original hourly forecast: Original choice",
        "Independent alternative",
        "Forecast irradiance",
    ):
        toggle_series(name, False)
    expect(evidence).to_be_hidden()
    toggle_series("Independent alternative", True)
    expect(evidence).to_be_hidden()
    toggle_series("Forecast irradiance", True)
    expect(evidence).to_contain_text("Forecast irradiance: 400.000 W/m²")
    toggle_series("Measured", True)
    toggle_series("Original hourly forecast: Original choice", True)
    toggle_series("Forecast irradiance", False)
    keyboard_focus(canvas, "Shift+Tab")
    canvas.press("Escape")
    expect(evidence).to_be_hidden()

    def select_point(dataset, index):
        """Use actual mouse or touch input at an original dataset's rendered point."""
        point = canvas.evaluate(
            "(canvas, point) => Chart.getChart(canvas).getDatasetMeta(point.dataset).data[point.index].getCenterPoint()",
            {"dataset": dataset, "index": index},
        )
        if page.viewport_size["width"] < 500:
            canvas.tap(position={"x": point["x"], "y": point["y"]})
        else:
            canvas.click(position={"x": point["x"], "y": point["y"]})
        expect(evidence).to_be_visible()

    select_point(2, 0)
    expect(evidence.locator("p").first).to_contain_text("1:00 AM to 02:00 AM")
    expect(evidence).to_contain_text("Independent alternative: 9.000 kWh")
    expect(evidence).to_contain_text("Original hourly forecast: Original choice: Unavailable kWh")
    expect(evidence).to_contain_text("Measured: Unavailable kWh")
    expect(evidence).not_to_contain_text("Measured: 0.400 kWh")
    evidence.get_by_role("button", name="Close hourly evidence", exact=True).click()
    page.get_by_role("checkbox", name="Forecast irradiance", exact=True).check()
    select_point(3, 0)
    expect(evidence.locator("p").first).to_contain_text("3:00 AM to 04:00 AM")
    expect(evidence).to_contain_text("Forecast irradiance: 400.000 W/m²")
    expect(evidence).to_contain_text("Independent alternative: Unavailable kWh")
    evidence.get_by_role("button", name="Close hourly evidence", exact=True).click()
    canvas.press("Home")
    expect(evidence).to_contain_text("Original hourly forecast: Original choice: 1.000 kWh")
    expect(evidence).to_contain_text("Independent alternative: Unavailable kWh")
    canvas.press("End")
    expect(evidence.locator("p").first).to_contain_text("3:00 AM to 04:00 AM")
    expect(evidence).to_contain_text("Forecast irradiance: 400.000 W/m²")
    canvas.press("ArrowLeft")
    expect(evidence.locator("p").first).to_contain_text("2:00 AM to 03:00 AM")
    expect(evidence).to_contain_text("Original hourly forecast: Original choice: 3.000 kWh")
    expect(evidence).to_contain_text("Independent alternative: 4.000 kWh")
    expect(evidence).to_contain_text("Forecast irradiance: Unavailable W/m²")
    points = canvas.evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.map(dataset => dataset.data)"
    )
    for actual, rows, field in zip(
        points,
        [
            original["rows"],
            original["rows"],
            original["alternatives"][0]["rows"],
            original["radiation"]["rows"],
            original["radiation"]["rows"],
        ],
        ["measured_kwh", "predicted_kwh", "predicted_kwh", "forecast_w_m2", "satellite_w_m2"],
        strict=True,
    ):
        assert actual == [
            {
                "x": (
                    datetime.fromisoformat(row["start"]).timestamp()
                    + datetime.fromisoformat(row["end"]).timestamp()
                )
                * 500,
                "y": row[field],
            }
            for row in rows
        ]
    evidence.get_by_role("button", name="Close hourly evidence", exact=True).click()
    unavailable = row(4, None)
    unavailable["evaluation_issue"] = "Original forecast does not cover this complete hour"
    source["rows"].append(unavailable)
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    page.wait_for_function(
        "() => Chart.getChart('quality-chart-0')?.data.datasets[0].data.length === 3"
    )
    canvas.press("End")
    expect(evidence.locator("p").first).to_contain_text("4:00 AM to 05:00 AM")
    expect(evidence).to_contain_text("Measured: Unavailable kWh")
    expect(evidence).to_contain_text("Original hourly forecast: Original choice: Unavailable kWh")
    expect(evidence).to_contain_text("Original forecast does not cover this complete hour")
    expect(evidence).to_contain_text("Independent alternative: Unavailable kWh")
