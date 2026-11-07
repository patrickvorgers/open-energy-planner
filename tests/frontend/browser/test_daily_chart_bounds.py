"""Daily chart bounds retain installation-local days and original numeric evidence."""

from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
@pytest.mark.parametrize(
    "day,hours,timezone,solar_reference",
    [
        ("2026-02-12", 24, "Europe/Amsterdam", False),
        ("2026-03-29", 23, "Europe/Amsterdam", False),
        ("2026-10-25", 25, "Europe/Amsterdam", False),
        ("2026-09-05", 24, "America/Santiago", False),
        ("2026-09-06", 23, "America/Santiago", False),
        ("2026-04-05", 24.5, "Australia/Lord_Howe", False),
        ("2026-04-05", 24.5, "Australia/Lord_Howe", True),
    ],
)
def test_daily_bounds_and_optional_calculations(app_page, day, hours, timezone, solar_reference):
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
    expect(page.locator("#performance-summary")).to_contain_text("3 completed calculations")
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
            "measured_kwh": None if index == 1 else 0.5,
            "error_kwh": None if index == 1 else 0.5,
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
        "coverage": {
            "hours": ceil(hours),
            "measured_hours": ceil(hours) - 1,
            "paired_hours": ceil(hours) - 1,
        },
        "rows": rows,
        "metrics": {},
        "issued_at": start.isoformat(),
        "model_label": "Synthetic",
        "forecast_version": "synthetic",
    }
    if solar_reference:
        source.update(id="roof", title="Synthetic roof", scope="solar")
        source["alternatives"] = [
            {
                **source,
                "model_label": "Alternative",
                "forecast_version": "alternative",
                "daily": {**source["daily"], "predicted_kwh": 35.25},
                "coverage": {"hours": 24, "measured_hours": 23, "paired_hours": 23},
                "rows": [
                    {
                        **row,
                        "predicted_kwh": 1.5,
                        "error_kwh": None if row["measured_kwh"] is None else 1.0,
                    }
                    for row in rows[1:]
                ],
            }
        ]
        # Exercise independently supplied render intervals. Provider hour admission has separate contracts.
        radiation_start = start + timedelta(hours=2)
        source["radiation"] = {
            "rows": [
                {
                    "start": (radiation_start + timedelta(hours=index)).isoformat(),
                    "end": (radiation_start + timedelta(hours=index + 1)).isoformat(),
                    "forecast_w_m2": 200 + index * 10,
                    "satellite_w_m2": None if index == 1 else 150 + index * 10,
                }
                for index in range(23)
            ],
            "complete_until": None,
            "observed_hours": 22,
            "hours": 23,
            "source": "Synthetic independent reference",
            "received_at": end.isoformat(),
            "issue": None,
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
    if solar_reference:
        page.get_by_role("button", name="Solar", exact=True).click()
        page.get_by_label("Solar source", exact=True).select_option("roof")
    expect(page.locator("#quality-chart-0")).to_be_visible()
    canvas = page.locator("#quality-chart-0")
    assert_bounds(canvas, start, end)
    points = canvas.evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.find(dataset => dataset.type === 'line' && dataset.label === 'Synthetic: Original choice').data"
    )
    assert points[0] == {"x": (start + timedelta(minutes=30)).timestamp() * 1000, "y": 1}
    last_start = start + timedelta(hours=ceil(hours) - 1)
    last_end = last_start + timedelta(hours=1)
    assert points[-1]["x"] == (last_start + timedelta(minutes=30)).timestamp() * 1000
    assert len(points) == ceil(hours)
    assert [point["y"] for point in points] == [1] * ceil(hours)
    measured = canvas.evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.find(dataset => dataset.type === 'bar' && dataset.label === 'Measured').data"
    )
    assert measured == [
        {
            "x": (start + timedelta(hours=index, minutes=30)).timestamp() * 1000,
            "y": None if index == 1 else 0.5,
        }
        for index in range(ceil(hours))
    ]
    if solar_reference:
        for label in ("Forecast irradiance", "Satellite estimate"):
            control = page.get_by_role("checkbox", name=label, exact=True)
            expect(control).not_to_be_checked()
            control.check()

    def check_intervals():
        """Check independent single-bar widths and clipped centres without changing the original evidence."""
        geometry = canvas.evaluate("""canvas => {
            const chart=Chart.getChart(canvas);
            const measured=chart.data.datasets.findIndex(dataset=>dataset.type==='bar' && dataset.label==='Measured');
            const forecast=chart.data.datasets.findIndex(dataset=>dataset.type==='line' && dataset.label==='Synthetic: Original choice');
            return chart.getDatasetMeta(measured).data.map((bar,index)=>({
                left:bar.x-bar.width/2,right:bar.x+bar.width/2,width:bar.width,
                centre:bar.x,forecastCentre:chart.getDatasetMeta(forecast).data[index].x,
                minimum:chart.chartArea.left,maximum:chart.chartArea.right
            }));
        }""")
        hourly_width = (geometry[0]["maximum"] - geometry[0]["minimum"]) / hours * 0.8 * 0.9
        for index, bar in enumerate(geometry):
            visible_hours = min(1, hours - index)
            assert bar["width"] == pytest.approx(hourly_width * visible_hours)
            assert bar["left"] >= bar["minimum"] - 0.01
            assert bar["right"] <= bar["maximum"] + 0.01
            expected_centre = (
                bar["minimum"]
                + (bar["maximum"] - bar["minimum"]) * (index + visible_hours / 2) / hours
            )
            assert bar["centre"] == pytest.approx(expected_centre)
            assert bar["forecastCentre"] == pytest.approx(expected_centre)
        if solar_reference:
            references = canvas.evaluate("""canvas => {
                const chart=Chart.getChart(canvas);
                return {unit:chart.options.scales.radiation.title.text,
                    series:chart.data.datasets.filter(dataset=>['Alternative','Forecast irradiance','Satellite estimate'].includes(dataset.label)).map(dataset=>({
                        label:dataset.label, axis:dataset.yAxisID, points:dataset.data,
                        centres:chart.getDatasetMeta(chart.data.datasets.indexOf(dataset)).data.map(point=>point.x)
                    }))};
            }""")
            assert references["unit"] == "Global horizontal irradiance (W/m²)"
            assert {reference["label"] for reference in references["series"]} == {
                "Alternative",
                "Forecast irradiance",
                "Satellite estimate",
            }
            for reference in references["series"]:
                alternative = reference["label"] == "Alternative"
                assert reference["axis"] == ("y" if alternative else "radiation")
                assert len(reference["points"]) == (24 if alternative else 23)
                offset_hours = 1 if alternative else 2
                for index, point in enumerate(reference["points"]):
                    instant = start + timedelta(hours=index + offset_hours + 0.5)
                    assert point["x"] == instant.timestamp() * 1000
                    if alternative:
                        expected_value = 1.5
                    elif reference["label"] == "Forecast irradiance":
                        expected_value = 200 + index * 10
                    else:
                        expected_value = None if index == 1 else 150 + index * 10
                    assert point["y"] == expected_value
                    centre_hours = index + offset_hours + min(1, hours - index - offset_hours) / 2
                    expected_centre = (
                        geometry[0]["minimum"]
                        + (geometry[0]["maximum"] - geometry[0]["minimum"]) * centre_hours / hours
                    )
                    assert reference["centres"][index] == pytest.approx(expected_centre)

    check_intervals()
    if hours != int(hours):
        measured_control = page.get_by_role("checkbox", name="Measured", exact=True)
        measured_control.uncheck()
        measured_control.check()
        check_intervals()
        original_viewport = page.viewport_size
        for viewport in (
            {**original_viewport, "width": original_viewport["width"] - 24},
            original_viewport,
        ):
            page.set_viewport_size(viewport)
            page.wait_for_function(
                "width => {const canvas=document.getElementById('quality-chart-0');return window.innerWidth===width && Math.abs(Chart.getChart(canvas).width-canvas.parentElement.clientWidth)<=1;}",
                arg=viewport["width"],
            )
            assert_bounds(canvas, start, end)
            check_intervals()
        expected_title = page.evaluate(
            """async ({start,end}) => {
                const {when}=await import('/setup/display.js');
                return `${when(start)} to ${when(end,{hour:'2-digit',minute:'2-digit'})}`;
            }""",
            {"start": last_start.isoformat(), "end": last_end.isoformat()},
        )
        canvas.evaluate("""canvas => {
            const chart=Chart.getChart(canvas),datasetIndex=chart.data.datasets.findIndex(dataset=>dataset.type==='bar' && dataset.label==='Measured'),index=chart.data.datasets[datasetIndex].data.length-1;
            chart.tooltip.setActiveElements([{datasetIndex,index}],{x:0,y:0});chart.update();
        }""")
        assert canvas.evaluate("canvas => Chart.getChart(canvas).tooltip.title") == [expected_title]
        check_intervals()
    assert not errors
    suffix = "-solar-reference" if solar_reference else ""
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(capture / f"quality-{day}-{width}{suffix}.png"), full_page=True)
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
    expect(page.locator("#performance-chart")).to_have_attribute("aria-disabled", "true")
    assert not errors


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_repeated_local_calculation_times_are_distinct(app_page):
    """Amsterdam's two 02:00 minutes stay distinguishable in keyboard access, dialog and chart tooltip."""
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
        """Keep the synthetic chart summaries and detail instants consistent."""
        route.fulfill(json=details.get(route.request.url.rsplit("/", 1)[-1], evidence))

    page.route("**/v1/planning-performance**", respond)
    page.goto(url + "/planning-performance")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#performance-summary")).to_contain_text("3 completed calculations")
    canvas = page.locator("#performance-chart")
    canvas.focus()
    for index, suffix in ((0, "(1 of 2)"), (1, "(2 of 2)")):
        canvas.press("Home")
        if index:
            canvas.press("ArrowRight")
        expect(page.locator("#performance-selected")).to_contain_text(suffix)
        assert canvas.evaluate("canvas => Chart.getChart(canvas).tooltip.title[0]").endswith(suffix)
        canvas.press("Enter")
        expect(page.locator("#calculation-time")).to_contain_text(suffix)
        page.keyboard.press("Escape")
        expect(canvas).to_be_focused()
