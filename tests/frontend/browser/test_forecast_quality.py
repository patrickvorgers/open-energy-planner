"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    MOBILE_CHART_LEGEND_BREAKPOINT_PX,
    TOKEN,
    assert_floating_save_bar,
    connect,
    discover,
    show_installation_sources,
)


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_forecast_quality_daily_baseline_missing_data_and_model_decision_separation(
    app_page,
):
    """Keep the fixed 24-kWh day unscored until its missing hour gives 24.5 kWh and -0.5 kWh error.

    Line points stay on bar top centres after resizing and legend toggles. Datasets
    0 and 1 are bars, followed by their matching lines at 2 and 3.
    Day, Device and Refresh evidence align at the bottom on desktop. Mobile
    places refresh below the first field column. The button stays at least
    44 pixels high. Screenshots capture the responsive toolbar alignment.
    The app_site fixture covers direct and ingress access. The app_page fixture
    supplies isolated desktop and touch-sized mobile browser contexts.
    """
    page, url = app_page
    start = datetime(2026, 10, 25, 23, tzinfo=UTC)
    rows = [
        {
            "start": (start + timedelta(hours=interval_index)).isoformat(),
            "end": (start + timedelta(hours=interval_index + 1)).isoformat(),
            "predicted_kwh": 1,
            "measured_kwh": measured_kwh,
            "error_kwh": None,
            "evaluation_issue": "Completed-hour measurement unavailable",
        }
        for interval_index, measured_kwh in enumerate((0.5, None, 2) + (1,) * 21)
    ]
    body = {
        "day": "2026-10-26",
        "start": start.isoformat(),
        "end": (start + timedelta(hours=24)).isoformat(),
        "captured_at": (start + timedelta(seconds=2)).isoformat(),
        "as_of": (start + timedelta(hours=49)).isoformat(),
        "timezone": "Europe/Amsterdam",
        "issues": [],
        "series": [
            {
                "id": "household",
                "title": "Household consumption",
                "model_label": "Original weekday model",
                "forecast_version": "original-version",
                "issued_at": (start - timedelta(minutes=5)).isoformat(),
                "last_received_at": (start + timedelta(hours=4)).isoformat(),
                "rows": rows,
                "metrics": {
                    "mae_kwh": None,
                    "bias_kwh": None,
                    "rmse_kwh": None,
                    "wape_percent": None,
                },
                "daily": {
                    "predicted_kwh": 24,
                    "measured_kwh": None,
                    "error_kwh": None,
                    "relative_error_percent": None,
                    "evaluation_issue": "Completed-hour measurement unavailable",
                },
                "coverage": {"hours": 24, "measured_hours": 23, "paired_hours": 23},
            }
        ],
        "current_models": {
            "household": {"model": "ridge-v2", "status": "active", "issue": None},
            "solar": {"configuration": None},
            "consumers": {"result": None},
        },
    }
    selection = {
        "explanation": "Simulated decision costs are not measured savings.",
        "issue": None,
        "models": [
            {
                "label": "Household",
                "scope": "demand",
                "active_model": "ridge-v2",
                "active_model_label": "Current ML model",
                "preferred": "learned",
                "status": "learned_preferred",
                "completed_windows": 3,
                "required_windows": 3,
                "pending_until": None,
                "issue": None,
                "currency": "EUR",
                "windows": [
                    {
                        "start": start.isoformat(),
                        "end": (start + timedelta(hours=48)).isoformat(),
                        "baseline": {"economic_cost": 1.23456, "corrected_import_kwh": 1.5},
                        "candidate": {"economic_cost": 0.12345, "corrected_import_kwh": 1.2},
                    }
                ],
            }
        ],
    }
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={
                "days": ["2026-10-26"],
                "next_before": None,
            }
        ),
    )
    page.route("**/v1/forecast-quality/days/2026-10-26", lambda route: route.fulfill(json=body))
    page.route("**/v1/models", lambda route: route.fulfill(json=selection))
    page.goto(url + "/quality")
    if not urlsplit(url).path:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#quality-status")).to_contain_text("Day 2026-10-26")
    expect(page.locator("#quality-charts")).to_contain_text("Original weekday model")
    expect(page.locator("#quality-charts .quality-metrics")).to_have_text(
        "Measured hours: 23 of 24: Comparable pairs: 23: "
        "MAE: Unavailable kWh/hour: Bias: Unavailable kWh/hour: RMSE: Unavailable kWh/hour: WAPE: Unavailable"
    )
    expect(page.locator("#quality-charts .quality-current-model")).to_contain_text(
        "Current ML model"
    )
    expect(page.locator("#quality-charts .quality-current-model")).to_contain_text(
        "Simulated baseline cost: 1.23 EUR: Candidate: 0.12 EUR"
    )
    expect(page.locator("#quality-charts .quality-current-model")).to_contain_text(
        "Baseline grid import: 1.5 kWh: Candidate: 1.2 kWh"
    )
    assert page.locator("#quality-charts").locator("canvas").count() == 1
    data = page.locator("#quality-chart-0").evaluate(
        "canvas => {const chart = Chart.getChart(canvas);return {predicted:chart.data.datasets[0].data.map(row=>row.y),measured:chart.data.datasets[1].data.map(row=>row.y),step:chart.options.scales.x.ticks.stepSize};}"
    )
    assert data == {"predicted": [1] * 24, "measured": [0.5, None, 2] + [1] * 21, "step": 3600000}
    overlay = page.locator("#quality-chart-0").evaluate(
        """canvas => {
            const chart = Chart.getChart(canvas);
            return {
                types: chart.data.datasets.map(dataset => dataset.type),
                orders: chart.data.datasets.map(dataset => dataset.order),
                lines: chart.data.datasets.slice(2).map(dataset => dataset.data.map(row => row.y)),
                gaps: chart.data.datasets.slice(2).map(dataset => dataset.spanGaps),
                segments: chart.getDatasetMeta(3).dataset.segments.map(segment => [segment.start, segment.end]),
                hiddenCanvasLegend: chart.options.plugins.legend.display === false
            };
        }"""
    )
    assert overlay == {
        "types": ["bar", "bar", "line", "line"],
        "orders": [1, 1, 0, 0],
        "lines": [[1] * 24, [0.5, None, 2] + [1] * 21],
        "gaps": [False, False],
        "segments": [[0, 0], [2, 23]],
        "hiddenCanvasLegend": True,
    }
    expect(page.locator(".quality-evidence-controls input")).to_have_count(2)
    expect(page.locator(".quality-evidence-controls .series-swatch")).to_have_count(2)
    original_size = page.viewport_size
    line_alignment = """canvas => {
        const chart = Chart.getChart(canvas);
        return [0, 1].every(datasetIndex => !chart.isDatasetVisible(datasetIndex) ||
            chart.getDatasetMeta(datasetIndex + 2).data.every((point, pointIndex) => {
                const bar = chart.getDatasetMeta(datasetIndex).data[pointIndex];
                return point.skip || (Math.abs(point.x - bar.x) < 0.01 && Math.abs(point.y - bar.y) < 0.01);
            }));
    }"""
    alternate_size = {"width": 1000 if original_size["width"] <= 640 else 375, "height": 900}
    for viewport in (original_size, alternate_size, original_size):
        page.set_viewport_size(viewport)
        page.wait_for_function(
            """({breakpoint, width}) => {
                const canvas = document.getElementById('quality-chart-0');
                const plot = canvas.getBoundingClientRect();
                const legend = document.querySelector('.quality-evidence-controls').getBoundingClientRect();
                const positioned = window.innerWidth > breakpoint ? legend.left >= plot.right : legend.top >= plot.bottom;
                const resized = Math.abs(Chart.getChart(canvas).width - canvas.parentElement.clientWidth) <= 1;
                return window.innerWidth === width && resized && positioned && document.documentElement.scrollWidth <= width;
            }""",
            arg={"breakpoint": MOBILE_CHART_LEGEND_BREAKPOINT_PX, "width": viewport["width"]},
        )
        assert page.locator("#quality-chart-0").evaluate(line_alignment)
        day_control = page.get_by_label("Day", exact=True).bounding_box()
        device_control = page.get_by_label("Device", exact=True).bounding_box()
        refresh_button = page.get_by_role("button", name="Refresh evidence", exact=True)
        refresh_box = refresh_button.bounding_box()
        assert (
            abs(
                day_control["y"]
                + day_control["height"]
                - device_control["y"]
                - device_control["height"]
            )
            < 1
        )
        if viewport["width"] > MOBILE_CHART_LEGEND_BREAKPOINT_PX:
            assert (
                abs(
                    refresh_box["y"]
                    + refresh_box["height"]
                    - day_control["y"]
                    - day_control["height"]
                )
                < 1
            )
        else:
            assert abs(refresh_box["x"] - day_control["x"]) < 1
            assert refresh_box["y"] >= day_control["y"] + day_control["height"]
        assert refresh_box["height"] >= 44
    page.screenshot(
        path=f"results/ci/quality-refresh-alignment-{original_size['width']}-{'ingress' in url}.png",
        full_page=True,
    )
    tooltip = page.locator("#quality-chart-0").evaluate(
        """canvas => {
            const chart = Chart.getChart(canvas);
            chart.tooltip.setActiveElements(chart.data.datasets.map((_, datasetIndex) => ({datasetIndex, index: 1})), {x: 0, y: 0});
            chart.update();
            return chart.tooltip.body.map(row => row.lines.join(' '));
        }"""
    )
    assert tooltip == ["Predicted     1.000 kWh", "Measured  Unavailable"]
    body["series"][0]["rows"] = [
        {
            **row,
            "measured_kwh": 1 if row["measured_kwh"] is None else row["measured_kwh"],
            "error_kwh": 0 if row["measured_kwh"] is None else 1 - row["measured_kwh"],
            "evaluation_issue": None,
        }
        for row in rows
    ]
    body["series"][0]["coverage"] = {
        "hours": 24,
        "measured_hours": 24,
        "paired_hours": 24,
    }
    body["series"][0]["daily"] = {
        "predicted_kwh": 24,
        "measured_kwh": 24.5,
        "error_kwh": -0.5,
        "relative_error_percent": 0.5 / 24.5 * 100,
        "evaluation_issue": None,
    }
    body["series"][0]["metrics"] = {
        "mae_kwh": 1.5 / 24,
        "bias_kwh": -0.5 / 24,
        "rmse_kwh": (1.25 / 24) ** 0.5,
        "wape_percent": 1.5 / 24.5 * 100,
    }
    # New days cannot replace the explicitly selected daily baseline during refresh.
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={
                "days": ["2026-10-27"],
                "next_before": "2026-10-27",
            }
        ),
    )
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(page.locator("#quality-status")).to_contain_text("Day 2026-10-26")
    expect(page.locator("#quality-day")).to_have_value("2026-10-26")
    expect(page.locator("#quality-charts")).to_contain_text("24 of 24 hours measured.")
    expect(page.locator("#quality-charts .quality-daily")).to_contain_text("Difference-0.5 kWh")
    assert (
        page.locator("#quality-chart-0").evaluate(
            "canvas => Chart.getChart(canvas).data.datasets[0].data.length"
        )
        == 24
    )
    ticks = page.locator("#quality-chart-0").evaluate(
        "canvas => Chart.getChart(canvas).scales.x.ticks.map(tick => tick.value)"
    )
    assert ticks
    assert all(value % 3600000 == 0 for value in ticks)
    page.get_by_role("checkbox", name="Predicted", exact=True).uncheck()
    assert page.locator("#quality-chart-0").evaluate(line_alignment)
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => [0, 2].every(index => !Chart.getChart(canvas).isDatasetVisible(index))"
    )
    page.get_by_role("checkbox", name="Predicted", exact=True).check()
    assert page.locator("#quality-chart-0").evaluate(line_alignment)
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => [0, 2].every(index => Chart.getChart(canvas).isDatasetVisible(index))"
    )
    measured = page.get_by_role("checkbox", name="Measured", exact=True)
    measured.focus()
    measured.press("Space")
    assert page.locator("#quality-chart-0").evaluate(line_alignment)
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => { const chart = Chart.getChart(canvas); return !chart.isDatasetVisible(1) && !chart.isDatasetVisible(3) && chart.isDatasetVisible(0) && chart.isDatasetVisible(2); }"
    )
    measured.press("Space")
    assert page.locator("#quality-chart-0").evaluate(line_alignment)
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => [1, 3].every(index => Chart.getChart(canvas).isDatasetVisible(index))"
    )
    page.get_by_text("Hourly evidence and provenance", exact=True).click()
    expect(page.locator("#quality-charts")).to_contain_text("Relative daily error: 2.0%")
    expect(page.locator("#quality-charts table")).to_contain_text("Eligible")
    expected_hour = page.evaluate(
        "value => new Date(value).toLocaleString(undefined, {year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'Europe/Amsterdam', timeZoneName: 'short'})",
        start.isoformat(),
    )
    expect(page.locator("#quality-charts table")).to_contain_text(expected_hour)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.route(
        "**/v1/forecast-quality/days?before=2026-10-27",
        lambda route: route.fulfill(
            json={"days": ["2026-10-26", "2026-10-25"], "next_before": None}
        ),
    )
    page.get_by_role("button", name="Load older days", exact=True).click()
    expect(page.locator("#quality-day option")).to_have_count(3)
    assert page.locator("#quality-day option").all_text_contents() == [
        "2026-10-27",
        "2026-10-26",
        "2026-10-25",
    ]
    expect(page.locator("#quality-day")).to_have_value("2026-10-26")
    expect(page.locator("#quality-charts table")).to_have_count(1)
    page.route(
        "**/v1/forecast-quality/days/2026-10-26",
        lambda route: route.fulfill(status=503, body="Unavailable"),
    )
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(page.locator("#quality-status")).to_contain_text("Cannot read forecast quality (503)")
    expect(page.locator("#quality-charts canvas")).to_have_count(0)
    expect(page.locator(".quality-current-model")).to_have_count(0)
    expect(page.locator("#quality-models article")).to_have_count(0)


def test_model_status_distinguishes_actual_forecast_preference_and_decision_evidence(app_page):
    """Desktop/mobile see the active model, pending evidence and simulated costs without treating preference as execution."""
    page, url = app_page
    page.clock.install()
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            json={
                "policy": "financial-shadow-v1",
                "issue": None,
                "explanation": "Three nonoverlapping windows. Simulated decision costs, not measured EMS savings.",
                "models": [
                    {
                        "label": "Household consumption",
                        "scope": "demand",
                        "currency": "EUR",
                        "active_model": "weekday-hour-v2",
                        "active_model_label": "Learned weekday/hour consumption profile",
                        "preferred": "learned",
                        "completed_windows": 3,
                        "required_windows": 3,
                        "status": "learned_preferred",
                        "pending_until": None,
                        "evaluated_at": "2026-10-24T00:00:00+00:00",
                        "issue": None,
                        "windows": [
                            {
                                "start": "2026-10-20T00:00:00+00:00",
                                "end": "2026-10-22T00:00:00+00:00",
                                "baseline": {"economic_cost": 0.5, "corrected_import_kwh": 1},
                                "candidate": {"economic_cost": 0.1, "corrected_import_kwh": 1},
                            }
                        ],
                    }
                ],
            }
        ),
    )
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={
                "days": [],
                "next_before": None,
                "current_models": {
                    "household": {"model": "weekday-hour-v2", "status": "active", "issue": None}
                },
            }
        ),
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_role("heading", name="Forecast quality", exact=True)).to_be_visible()
    expect(page.locator("#quality-models dd")).to_have_text(
        ["Learned weekday/hour consumption profile", "Active", "Learned", "3 of 3 required"]
    )
    expect(
        page.get_by_text("Learned model has consistent decision benefit", exact=True)
    ).to_have_count(1)
    expect(page.locator("#quality-policy")).to_contain_text("not measured EMS savings")
    page.get_by_text("Current model and learning", exact=True).click()
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(
        page.locator('#quality-models details[data-model-source="household"]')
    ).to_have_attribute("open", "")
    expect(page.locator("#quality-models")).to_contain_text(
        "Simulated baseline cost: 0.50 EUR: Candidate: 0.10 EUR"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1280, 390])
def test_quality_ready_device_is_independent_of_an_untrained_selected_device(app_page, width):
    """A ready boiler shows its own shadow status while a new dryer still reports missing history."""
    page, url = app_page
    page.set_viewport_size({"width": width, "height": 900})
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(json={"models": [], "issue": None}),
    )
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={
                "days": [],
                "next_before": None,
                "current_models": {
                    "consumers": {
                        "options": [
                            {"entity_id": "sensor.boiler", "name": "Boiler"},
                            {"entity_id": "sensor.dryer", "name": "New dryer"},
                        ],
                        "selected": [
                            {"entity_id": "sensor.boiler", "weather_sensitive": False},
                            {"entity_id": "sensor.dryer", "weather_sensitive": False},
                        ],
                        "result": {
                            "status": "history_or_weather_not_ready",
                            "detail": "New dryer needs more history",
                            "devices": [
                                {
                                    "entity_id": "sensor.boiler",
                                    "forecast": {"model": "weekday-hour-v2"},
                                    "weather_features_used": False,
                                    "status": "shadow_waiting_for_decision_evidence",
                                }
                            ],
                        },
                    }
                },
            }
        ),
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    rows = page.locator("#quality-models article")
    expect(rows).to_have_count(2)
    boiler = rows.filter(has=page.get_by_role("heading", name="Boiler", exact=True))
    boiler.get_by_text("Current model and learning", exact=True).click()
    expect(boiler.locator("dd")).to_have_text(
        ["weekday-hour-v2", "Shadow waiting for decision evidence", "Not used"]
    )
    expect(boiler).not_to_contain_text("History or weather not ready")
    dryer = rows.filter(has=page.get_by_role("heading", name="New dryer", exact=True))
    dryer.get_by_text("Current model and learning", exact=True).click()
    expect(dryer).to_contain_text("History or weather not ready")
    expect(dryer).to_contain_text("New dryer needs more history")


def test_quality_current_models_filter_without_midnight_history_and_clear_failures(app_page):
    """Current model rows remain available without midnight history and follow the device filter."""
    page, url = app_page
    page.clock.install()
    requirement = "Available PV needs uncurtailed, independent production evidence"
    models = [
        {
            "scope": scope,
            "label": label,
            "active_model_label": active,
            "preferred": "baseline",
            "status": "training_history_required",
            "completed_windows": 0,
            "required_windows": 3,
            "windows": [],
            "issue": requirement,
        }
        for scope, label, active in [
            ("demand", "Household consumption", "Learned weekday/hour profile"),
            ("solar:roof", "SolarEdge lifetime energy", "Physical solar model"),
            ("solar:garage", "Test garage solar", "Physical solar model"),
        ]
    ]
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            json={
                "issue": None,
                "explanation": "Simulated decisions, not measured EMS savings.",
                "models": models,
            }
        ),
    )
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={
                "days": [],
                "next_before": None,
                "current_models": {
                    "household": {"model": "weekday-hour-v2", "status": "active", "issue": None}
                },
            }
        ),
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    rows = page.locator("#quality-models article")
    expect(rows).to_have_count(3)
    expect(rows.first.locator("dd")).to_have_text(
        ["Learned weekday/hour profile", "Active", "Baseline", "0 of 3 required"]
    )
    page.get_by_label("Device", exact=True).select_option("roof")
    expect(rows).to_have_count(1)
    expect(rows.locator("h2")).to_have_text("SolarEdge lifetime energy")
    page.get_by_text("Current model and learning", exact=True).click()
    expect(rows).to_contain_text("production prevented by curtailment")
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(rows.locator("details")).to_have_attribute("open", "")
    page.get_by_label("Device", exact=True).select_option("")
    for width in (1440, 768, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=f"results/ci/model-insights-{width}.png", full_page=True)
    page.route("**/v1/models", lambda route: route.fulfill(status=503, body="Unavailable"))
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(page.locator("#quality-status")).to_contain_text("Cannot read forecast quality")
    expect(rows).to_have_count(0)
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            json={"issue": None, "explanation": "Waiting for evidence", "models": []}
        ),
    )
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(rows).to_have_count(1)
    expect(rows.first).not_to_contain_text("Selection preference")
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            json={
                "issue": "model_selection_archive_unreadable",
                "explanation": "Evidence unavailable",
                "models": [],
            }
        ),
    )
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(page.locator("#quality-model-issue")).to_contain_text(
        "Model selection archive is unreadable"
    )
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(json={"days": [], "next_before": None, "current_models": {}}),
    )
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(rows).to_have_count(0)
    expect(page.locator("#quality-status")).to_contain_text(
        "No daily forecasts or current model status available yet"
    )
    page.route("**/v1/models", lambda route: route.abort())
    page.goto(url + "/setup")
    model_requests = []
    page.on(
        "request",
        lambda request: (
            model_requests.append(request.url) if request.url.endswith("/v1/models") else None
        ),
    )
    discover(page, url)
    connect(page)
    show_installation_sources(page)
    page.clock.fast_forward(31000)
    assert model_requests == []
    expect(page.locator("#forecast-models")).to_have_count(0)
    expect(page.get_by_label("Installation area")).to_contain_text("Price sources")


@pytest.mark.parametrize("app_site", ["savings"], indirect=True)
def test_planning_time_edges_fill_plot_and_forecast_colors_are_distinct(app_page):
    """Desktop/mobile draw archived interval boundaries at plot edges for every time window. Solar is yellow and household load green, both with at least 3:1 contrast on white."""
    page, url = app_page
    headers = {"Authorization": "Bearer " + TOKEN}
    identity = page.request.get(url + "/v1/plans?limit=1", headers=headers).json()["plans"][0][
        "plan_id"
    ]
    plan = page.request.get(url + f"/v1/plans/{identity}/comparison", headers=headers).json()[
        "plan"
    ]
    intervals = plan["input_snapshot"]["problem"]["intervals"]
    first = datetime.fromisoformat(intervals[0]["start"])
    last = datetime.fromisoformat(intervals[-1]["start"]) + timedelta(
        hours=intervals[-1]["duration_h"]
    )
    expected_start = first.replace(minute=first.minute // 15 * 15, second=0, microsecond=0)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    for window, hours in (("Full horizon", 0), ("Next 6 hours", 6), ("Next 24 hours", 24)):
        page.get_by_role("button", name=window, exact=True).click()
        expected_end = expected_start + timedelta(hours=hours) if hours else last
        axes = page.evaluate("""['reference-chart','self-consumption-chart','plan-chart','forecast-chart'].map(id => {
            const chart = Chart.getChart(document.getElementById(id)), axis = chart.scales.x;
            return {id, min: axis.min, max: axis.max, left: chart.chartArea.left, right: chart.chartArea.right,
                start: axis.getPixelForValue(axis.min), end: axis.getPixelForValue(axis.max)};
        })""")
        for axis in axes:
            assert axis["min"] == expected_start.timestamp() * 1000, axis["id"]
            assert axis["max"] == expected_end.timestamp() * 1000, axis["id"]
            assert axis["right"] > axis["left"], axis["id"]
            assert axis["start"] == pytest.approx(axis["left"]), axis["id"]
            assert axis["end"] == pytest.approx(axis["right"]), axis["id"]
    colors = page.evaluate(
        """Chart.getChart(document.getElementById('forecast-chart')).data.datasets.map(dataset => dataset.borderColor)"""
    )
    assert colors == ["#23965e", "#b58900"]
    for color in colors:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        luminance = sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))
        assert 1.05 / (luminance + 0.05) >= 3
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_energy_consumer_forecast_choices_use_visual_inputs_without_helpers(app_page):
    """Appliances owns selection/weather, retains hidden EV/thermal membership and saves source version. Forecasts exposes analysis only."""
    page, url = app_page
    state = {
        "version": "fixture",
        "thermal_meters": ["sensor.boiler"],
        "charging_meters": ["sensor.charger"],
        "selected": [
            {"entity_id": "sensor.boiler", "weather_sensitive": False},
            {"entity_id": "sensor.charger", "weather_sensitive": False},
        ],
        "options": [
            {"entity_id": "sensor.heat", "name": "Heat pump", "upstream": None},
            {"entity_id": "sensor.charger", "name": "Wallbox", "upstream": None},
            {"entity_id": "sensor.boiler", "name": "Boiler", "upstream": None},
        ],
        "active_in_planning": False,
        "result": None,
        "issue": None,
    }

    def consumers(route):
        """Persist the displayed checkbox selection so reloading proves the same API-backed settings."""
        if route.request.method == "PUT":
            assert route.request.post_data_json["expected_version"] == "fixture"
            state["selected"] = route.request.post_data_json["selected"]
        route.fulfill(json=state)

    page.route("**/v1/consumers", consumers)
    page.route(
        "**/v1/ev",
        lambda route: route.fulfill(status=409, body="Home Assistant timezone unavailable"),
    )
    page.goto(url + "/appliance-settings")
    if not urlsplit(url).path:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#appliance-empty")).to_be_visible()
    page.get_by_role("button", name="Add appliance", exact=True).click()
    expect(page.locator("#source-results")).not_to_contain_text("Wallbox")
    expect(page.locator("#source-results")).not_to_contain_text("Boiler")
    page.locator(".source-choice").filter(has_text="Heat pump").click()
    expect(page.get_by_role("group", name="Heat pump")).to_be_visible()
    expect(page.get_by_role("button", name="Appliance is ready", exact=True)).to_have_count(0)
    expect(page.get_by_label("Finish before (Home Assistant time)")).to_have_count(0)
    weather = page.get_by_label("Consumption depends on outdoor temperature", exact=True)
    expect(weather).to_be_enabled()
    weather.check()
    assert_floating_save_bar(page)
    with page.expect_request(
        lambda request: request.method == "PUT" and request.url.endswith("/v1/consumers")
    ) as sent:
        page.get_by_role("button", name="Save appliance settings", exact=True).click()
    assert sent.value.post_data_json["selected"] == [
        {"entity_id": "sensor.boiler", "weather_sensitive": False},
        {"entity_id": "sensor.charger", "weather_sensitive": False},
        {"entity_id": "sensor.heat", "weather_sensitive": True},
    ]
    expect(weather).to_be_checked()
    expect(weather).to_be_disabled()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#appliance-more summary").click()
    page.get_by_role("button", name="Remove from OEP…", exact=True).click()
    expect(page.locator("#appliance-empty")).to_be_visible()
    assert state["selected"] == [
        {"entity_id": "sensor.boiler", "weather_sensitive": False},
        {"entity_id": "sensor.charger", "weather_sensitive": False},
    ]
    page.goto(url + "/consumers")
    expect(
        page.get_by_role("main").get_by_role("link", name="Appliances settings", exact=True)
    ).to_have_attribute("href", urlsplit(url).path + "/appliance-settings")
    expect(page.get_by_label("Use in OEP", exact=True)).to_have_count(0)
    expect(page.get_by_label("Forecast separately", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize("score_eligible", [True, False])
def test_device_forecasts_share_daily_quality_without_replacing_midnight_evidence(
    app_page, score_eligible
):
    """Keep midnight evidence and separate complete measurements from comparison eligibility on desktop/mobile and ingress."""
    page, url = app_page
    start = datetime(2026, 10, 25, 23, tzinfo=UTC)
    rows = [
        {
            "start": (start + timedelta(hours=interval_index)).isoformat(),
            "end": (start + timedelta(hours=interval_index + 1)).isoformat(),
            "predicted_kwh": 0.25,
            "measured_kwh": 0.2 if interval_index == 0 else None,
            "error_kwh": None,
            "evaluation_issue": "Day has not ended",
        }
        for interval_index in range(24)
    ]
    source = {
        "id": "heat",
        "title": "Heat pump",
        "model_label": "Midnight heat model",
        "forecast_version": "frozen-heat",
        "issued_at": start.isoformat(),
        "last_received_at": None,
        "rows": rows,
        "metrics": dict.fromkeys(("mae_kwh", "bias_kwh", "rmse_kwh", "wape_percent")),
        "daily": {
            "predicted_kwh": 6,
            "measured_kwh": None,
            "error_kwh": None,
            "relative_error_percent": None,
            "evaluation_issue": "Day has not ended",
        },
        "coverage": {"hours": 24, "measured_hours": 1, "paired_hours": 1},
        "weather_sensitive": True,
        "weather_features_used": True,
        "composition_selected": False,
    }
    body = {
        "day": "2026-10-26",
        "start": start.isoformat(),
        "end": (start + timedelta(hours=24)).isoformat(),
        "captured_at": start.isoformat(),
        "as_of": (start + timedelta(hours=2)).isoformat(),
        "timezone": "Europe/Amsterdam",
        "issues": [],
        "series": [source, {**deepcopy(source), "id": "other", "title": "Other device"}],
        "current_models": {
            "household": {"model": "current", "status": "active", "issue": None},
            "solar": {},
            "consumers": {
                "options": [{"entity_id": "heat", "name": "Heat pump"}],
                "result": {
                    "status": "ready",
                    "devices": [
                        {
                            "entity_id": "heat",
                            "forecast": {"model": "current-heat-model"},
                            "weather_features_used": True,
                        }
                    ],
                },
            },
        },
    }
    yesterday = deepcopy(body)
    prior_day_start = start - timedelta(hours=25)
    yesterday.update(
        day="2026-10-25",
        start=prior_day_start.isoformat(),
        end=start.isoformat(),
        captured_at=prior_day_start.isoformat(),
    )
    for device in yesterday["series"]:
        device["issued_at"] = prior_day_start.isoformat()
        device["coverage"].update(hours=25, measured_hours=25, paired_hours=25)
        device["daily"].update(
            predicted_kwh=6.25,
            measured_kwh=5,
            error_kwh=1.25 if score_eligible else None,
            relative_error_percent=25 if score_eligible else None,
            evaluation_issue=None if score_eligible else "Controlled-load timing is not comparable",
        )
        device["rows"] = [
            {
                "start": (start - timedelta(hours=25 - interval_index)).isoformat(),
                "end": (start - timedelta(hours=24 - interval_index)).isoformat(),
                "predicted_kwh": 0.25,
                "measured_kwh": 0.2,
                "error_kwh": 0.05 if score_eligible else None,
                "evaluation_issue": None
                if score_eligible
                else "Controlled-load timing is not comparable",
            }
            for interval_index in range(25)
        ]
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={"days": ["2026-10-26", "2026-10-25"], "next_before": None}
        ),
    )
    page.route("**/v1/forecast-quality/days/2026-10-26", lambda route: route.fulfill(json=body))
    page.route(
        "**/v1/forecast-quality/days/2026-10-25", lambda route: route.fulfill(json=yesterday)
    )
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            json={"explanation": "Independent model selection", "issue": None, "models": []}
        ),
    )
    page.goto(url + "/consumers")
    if not urlsplit(url).path:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Forecast quality")
    expect(
        page.get_by_role("navigation").get_by_role("link", name="Device forecasts", exact=True)
    ).to_have_count(0)
    expect(page.get_by_label("Day", exact=True)).to_have_value("2026-10-26")
    expect(page.locator("#quality-charts canvas")).to_have_count(2)
    page.get_by_label("Device", exact=True).select_option("heat")
    expect(page.locator("#quality-charts canvas")).to_have_count(1)
    expect(page.locator("#quality-charts h2")).to_have_text("Heat pump")
    expect(page.locator("#quality-charts .quality-current-model")).to_contain_text(
        "current-heat-model"
    )
    expect(page.locator("#quality-models article")).to_have_count(0)
    page.get_by_label("Device", exact=True).select_option("other")
    expect(page.locator("#quality-charts canvas")).to_have_count(1)
    expect(page.locator("#quality-charts .quality-current-model")).to_have_count(0)
    page.get_by_label("Device", exact=True).select_option("heat")
    expect(page.locator(".quality-coverage")).to_contain_text("Day in progress")
    expect(page.locator(".quality-daily")).to_contain_text("Not scored yet")
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('quality-chart-0')).data.datasets[1].data[1].y"
        )
        is None
    )
    source["rows"][0]["measured_kwh"] = 0
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    page.wait_for_function(
        "() => Chart.getChart(document.getElementById('quality-chart-0'))?.data.datasets[3]?.data[0].y === 0"
    )
    assert page.locator("#quality-chart-0").evaluate(
        """canvas => {
            const chart = Chart.getChart(canvas);
            return chart.data.datasets[1].data[0].y === 0
                && chart.data.datasets[3].data[1].y === null
                && !chart.getDatasetMeta(3).data[0].skip;
        }"""
    )
    capture = Path("results/ci")
    capture.mkdir(parents=True, exist_ok=True)
    suffix = f"{page.viewport_size['width']}-{'ingress' if urlsplit(url).path else 'standalone'}"
    page.screenshot(path=str(capture / f"unified-forecasts-{suffix}-today.png"), full_page=True)
    page.get_by_text("Hourly evidence and provenance", exact=True).click()
    expect(page.locator("#quality-charts")).to_contain_text("Midnight heat model")
    expect(page.locator("#quality-charts")).to_contain_text("Temperature features: Used")
    page.get_by_label("Day", exact=True).select_option("2026-10-25")
    expect(page.get_by_label("Device", exact=True)).to_have_value("heat")
    expect(page.locator(".quality-daily")).to_contain_text(
        "Difference1.25 kWh" if score_eligible else "DifferenceNot scored yet"
    )
    expect(page.locator(".quality-coverage")).to_contain_text("Complete day")
    expect(page.locator(".quality-coverage")).to_contain_text("25 of 25 hours measured")
    if not score_eligible:
        expect(page.locator("#quality-charts > article > .balance-warning")).to_have_text(
            "Controlled-load timing is not comparable"
        )
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('quality-chart-0')).data.datasets[0].data.map(row => row.y)"
        )
        == [0.25] * 25
    )
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.slice(2).map(dataset => dataset.data.map(row => row.y))"
    ) == [[0.25] * 25, [0.2] * 25]
    assert (
        page.locator("#quality-charts").bounding_box()["y"]
        < page.locator("#model-selection-help").bounding_box()["y"]
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.screenshot(path=str(capture / f"unified-forecasts-{suffix}-yesterday.png"), full_page=True)
