"""Responsive app-owned setup against real authenticated API/storage, without physical devices."""

import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import (
    TOKEN,
    assert_floating_save_bar,
    choose_day,
    connect,
    day_trigger,
    discover,
    expect_day,
    show_installation_sources,
)
from tests.frontend.browser.quality_helpers import show_hour


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
def test_forecast_quality_daily_baseline_missing_data_and_model_decision_separation(
    app_page,
):
    """Keep the fixed 24-kWh day unscored until its missing hour gives 24.5 kWh and -0.5 kWh error.

    Dataset 0 contains measured bars and dataset 1 the original forecast line.
    Chart resizing, legend placement and independent visibility toggles are checked.
    The day selector precedes the Household, Solar and Devices tabs.
    The refresh button stays at least 44 pixels high. Screenshots capture the
    responsive toolbar alignment.
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
    expect(page.locator("#quality-status")).to_contain_text("Day 10/26/2026")
    expect(page.locator("#quality-charts")).to_contain_text("Original weekday model")
    evidence = show_hour(page)
    expect(evidence).to_contain_text("Measured hours: 23 of 24: Comparable pairs: 23")
    expect(evidence).to_contain_text("MAE: Unavailable kWh/hour")
    page.locator("#quality-chart-0").press("Escape")
    expect(page.locator("#quality-charts .quality-current-summary")).to_contain_text(
        "Current ML model"
    )
    current_details = page.locator("#quality-charts .quality-current-model")
    expect(current_details.locator("summary")).to_have_text("Current model and learning")
    expect(current_details).to_contain_text("The graph retains the original model")
    expect(current_details).to_contain_text(
        re.compile(r"Simulated baseline cost: 1[.,]23 EUR: Candidate: 0[.,]12 EUR")
    )
    expect(page.locator("#quality-charts .quality-current-model")).to_contain_text(
        re.compile(r"Baseline grid import: 1[.,]5 kWh: Candidate: 1[.,]2 kWh")
    )
    assert page.locator("#quality-charts").locator("canvas").count() == 1
    data = page.locator("#quality-chart-0").evaluate(
        "canvas => {const chart = Chart.getChart(canvas);return {predicted:chart.data.datasets[1].data.map(row=>row.y),measured:chart.data.datasets[0].data.map(row=>row.y),step:chart.options.scales.x.ticks.stepSize};}"
    )
    assert data == {"predicted": [1] * 24, "measured": [0.5, None, 2] + [1] * 21, "step": 3600000}
    overlay = page.locator("#quality-chart-0").evaluate("""canvas => {
        const chart = Chart.getChart(canvas);
        return {types: chart.data.datasets.map(dataset => dataset.type),
                measuredOutline: chart.data.datasets[0].borderWidth,
                activeDash: chart.data.datasets[1].borderDash,
                hiddenCanvasLegend: chart.options.plugins.legend.display === false};
    }""")
    assert overlay == {
        "types": ["bar", "line"],
        "measuredOutline": 0,
        "activeDash": [],
        "hiddenCanvasLegend": True,
    }
    expect(page.locator(".quality-evidence-controls input")).to_have_count(2)
    original_size = page.viewport_size
    alternate_size = {"width": 1000 if original_size["width"] <= 640 else 375, "height": 900}
    for viewport in (original_size, alternate_size, original_size):
        page.set_viewport_size(viewport)
        page.wait_for_function(
            """({width}) => {
                const canvas = document.getElementById('quality-chart-0');
                const plot = canvas.getBoundingClientRect();
                const legend = document.querySelector('.quality-evidence-controls').getBoundingClientRect();
                const positioned = legend.top >= plot.bottom && Math.abs(legend.width - plot.width) <= 1;
                const resized = Math.abs(Chart.getChart(canvas).width - canvas.parentElement.clientWidth) <= 1;
                return window.innerWidth === width && resized && positioned && document.documentElement.scrollWidth <= width;
            }""",
            arg={"width": viewport["width"]},
        )
        day_control = day_trigger(page).bounding_box()
        refresh_button = page.get_by_role("button", name="Refresh evidence", exact=True)
        refresh_box = refresh_button.bounding_box()
        assert day_control["y"] < page.locator("#quality-subjects").bounding_box()["y"]
        assert refresh_box["height"] >= 44
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
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
    assert tooltip == [
        "Measured: Unavailable kWh",
        "Original weekday model: Original choice: 1.000 kWh",
    ]
    expect(show_hour(page, 1)).to_contain_text("Measured: Unavailable kWh")
    page.get_by_role("button", name="Close hourly evidence", exact=True).click()
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
    expect(page.locator("#quality-status")).to_contain_text("Day 10/26/2026")
    expect_day(page, "2026-10-26")
    expect(page.locator("#quality-charts")).to_contain_text("24 of 24 hours measured.")
    expect(page.locator("#quality-charts .quality-daily")).to_contain_text(
        re.compile(r"Difference-0[.,]5 kWh")
    )
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
    page.get_by_role(
        "checkbox", name="Original weekday model: Original choice", exact=True
    ).uncheck()
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => !Chart.getChart(canvas).isDatasetVisible(1)"
    )
    page.get_by_role("checkbox", name="Original weekday model: Original choice", exact=True).check()
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => Chart.getChart(canvas).isDatasetVisible(1)"
    )
    measured = page.get_by_role("checkbox", name="Measured", exact=True)
    measured.focus()
    measured.press("Space")
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => { const chart = Chart.getChart(canvas); return !chart.isDatasetVisible(0) && chart.isDatasetVisible(1); }"
    )
    measured.press("Space")
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => Chart.getChart(canvas).isDatasetVisible(0)"
    )
    evidence = show_hour(page)
    expect(evidence).to_contain_text(re.compile(r"Relative daily error: 2[.,]0%"))
    expect(evidence).to_contain_text("Eligible")
    expected_hour = page.evaluate(
        "value => new Date(value).toLocaleString(undefined, {year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'Europe/Amsterdam'})",
        start.isoformat(),
    )
    expect(evidence).to_contain_text(expected_hour)
    page.locator("#quality-chart-0").press("Escape")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.route(
        "**/v1/forecast-quality/days?before=2026-10-27",
        lambda route: route.fulfill(
            json={"days": ["2026-10-26", "2026-10-25"], "next_before": None}
        ),
    )
    page.get_by_role("button", name="Load older days", exact=True).click()
    day_trigger(page).click()
    calendar = page.get_by_role("dialog", name="Choose day").get_by_role(
        "group", name="Calendar dates"
    )
    expect(calendar.locator("button:enabled")).to_have_count(3)
    assert calendar.locator("button:enabled").evaluate_all(
        "buttons => buttons.map(button => button.getAttribute('aria-label'))"
    ) == ["10/25/2026", "10/26/2026", "10/27/2026"]
    page.get_by_role("button", name="Close date selector").click()
    expect_day(page, "2026-10-26")
    expect(show_hour(page)).to_be_visible()
    page.locator("#quality-chart-0").press("Escape")
    # Repeated local hours keep independent rows without exposing timezone names.
    fold_body = deepcopy(body)
    fold_body.update(day="2026-10-25", start="2026-10-24T22:00:00Z", end="2026-10-25T23:00:00Z")
    fold_body["series"][0]["rows"] = [
        dict(rows[0], start=f"2026-10-25T0{hour}:00:00Z", end=f"2026-10-25T0{hour + 1}:00:00Z")
        for hour in range(3)
    ]
    page.route(
        "**/v1/forecast-quality/days/2026-10-25", lambda route: route.fulfill(json=fold_body)
    )
    choose_day(page, "2026-10-25")
    local_labels = page.evaluate("""() => {
        const options = {timeZone: 'Europe/Amsterdam', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit'};
        return [0, 1, 2].map(hour => new Date(`2026-10-25T0${hour}:00:00Z`).toLocaleString(undefined, options));
    }""")
    for index, label in enumerate(
        [local_labels[0] + " (1 of 2)", local_labels[1] + " (2 of 2)", local_labels[2]]
    ):
        expect(show_hour(page, index)).to_contain_text(label)
    page.locator("#quality-chart-0").press("Escape")
    canvas = page.locator("#quality-chart-0")
    point = canvas.evaluate(
        "canvas => Chart.getChart(canvas).getDatasetMeta(0).data[0].getCenterPoint()"
    )
    canvas.hover(position={"x": point["x"], "y": point["y"]})
    end_label = page.evaluate(
        "new Date('2026-10-25T01:00:00Z').toLocaleString(undefined, {timeZone: 'Europe/Amsterdam', hour: '2-digit', minute: '2-digit'})"
    )
    expected_title = [f"{local_labels[0]} (1 of 2) to {end_label} (2 of 2)"]
    page.wait_for_function(
        "title => JSON.stringify(Chart.getChart(document.querySelector('#quality-chart-0')).tooltip.title) === JSON.stringify(title)",
        arg=expected_title,
    )
    assert canvas.evaluate("canvas => Chart.getChart(canvas).tooltip.title") == expected_title
    choose_day(page, "2026-10-26")
    expect(page.locator("#quality-status")).to_contain_text("Day 10/26/2026")
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
        ["Active", "Learned", "3 of 3 required"]
    )
    expect(
        page.get_by_text("Learned model has consistent decision benefit", exact=True)
    ).to_have_count(1)
    expect(page.locator("#quality-policy")).to_have_count(0)
    expect(page.locator(".quality-current-summary")).to_contain_text(
        "Learned weekday/hour consumption profile"
    )
    page.get_by_text("Current model and learning", exact=True).click()
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(
        page.locator('#quality-models details[data-model-source="household"]')
    ).to_have_attribute("open", "")
    expect(page.locator("#quality-models")).to_contain_text(
        re.compile(r"Simulated baseline cost: 0[.,]50 EUR: Candidate: 0[.,]10 EUR")
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
    page.get_by_role("button", name="Devices", exact=True).click()
    expect(rows).to_have_count(1)
    boiler = rows.filter(has=page.get_by_role("heading", name="Boiler", exact=True))
    boiler.get_by_text("Current model and learning", exact=True).click()
    expect(boiler.locator("dd")).to_have_text(["Shadow waiting for decision evidence", "Not used"])
    expect(boiler.locator(".quality-current-summary")).to_contain_text("weekday-hour-v2")
    expect(boiler).not_to_contain_text("History or weather not ready")
    page.get_by_label("Device", exact=True).select_option("sensor.dryer")
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
    expect(rows).to_have_count(1)
    expect(rows.first.locator("dd")).to_have_text(["Active", "Baseline", "0 of 3 required"])
    expect(rows.first.locator(".quality-current-summary")).to_contain_text(
        "Learned weekday/hour profile"
    )
    page.get_by_role("button", name="Solar", exact=True).click()
    page.get_by_label("Solar source", exact=True).select_option("roof")
    expect(rows).to_have_count(1)
    expect(rows.locator("h2")).to_have_text("SolarEdge lifetime energy")
    page.get_by_text("Current model and learning", exact=True).click()
    expect(rows).to_contain_text("production prevented by curtailment")
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(rows.locator("details")).to_have_attribute("open", "")
    page.get_by_role("button", name="Household", exact=True).click()
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
    page.clock.set_fixed_time(first)
    page.goto(url + "/overview")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#overview-status")).to_contain_text("Connected")
    for hours in (0, 6, 24):
        page.locator(f'[data-hours="{hours}"]').click()
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
    page.route("**/v1/consumers?view=settings", consumers)
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
    page.locator("#appliance-more summary").click()
    page.get_by_role("button", name="Remove appliance…", exact=True).click()
    page.get_by_role("alertdialog", name="Remove appliance?", exact=True).get_by_role(
        "button", name="Remove appliance", exact=True
    ).click()
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
    page.clock.set_fixed_time(start + timedelta(minutes=30))
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
            json={
                "days": ["2026-10-26", "2026-10-25"],
                "next_before": None,
                "timezone": "Europe/Amsterdam",
            }
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
    expect_day(page, "2026-10-25")
    choose_day(page, "2026-10-26")
    page.get_by_role("button", name="Devices", exact=True).click()
    expect(page.locator("#quality-charts canvas")).to_have_count(1)
    page.get_by_label("Device", exact=True).select_option("heat")
    expect(page.locator("#quality-charts canvas")).to_have_count(1)
    expect(page.locator("#quality-charts h2")).to_have_text("Heat pump")
    expect(page.locator("#quality-charts .quality-current-summary")).to_contain_text(
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
            "Chart.getChart(document.getElementById('quality-chart-0')).data.datasets[0].data[1].y"
        )
        is None
    )
    source["rows"][0]["measured_kwh"] = 0
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    page.wait_for_function(
        "() => Chart.getChart(document.getElementById('quality-chart-0'))?.data.datasets[0]?.data[0].y === 0"
    )
    assert page.locator("#quality-chart-0").evaluate(
        """canvas => {
            const chart = Chart.getChart(canvas);
            return chart.data.datasets[0].data[0].y === 0
                && chart.data.datasets[0].data[1].y === null
                && !chart.getDatasetMeta(0).data[0].skip;
        }"""
    )
    capture = Path("results/ci")
    capture.mkdir(parents=True, exist_ok=True)
    suffix = f"{page.viewport_size['width']}-{'ingress' if urlsplit(url).path else 'standalone'}"
    page.screenshot(path=str(capture / f"unified-forecasts-{suffix}-today.png"), full_page=True)
    evidence = show_hour(page)
    expect(evidence).to_contain_text("Midnight heat model")
    expect(evidence).to_contain_text("Temperature features: Used")
    page.locator("#quality-chart-0").press("Escape")
    choose_day(page, "2026-10-25")
    expect(page.get_by_label("Device", exact=True)).to_have_value("heat")
    expect(page.locator(".quality-daily")).to_contain_text(
        re.compile(r"Difference1[.,]25 kWh") if score_eligible else "DifferenceNot scored yet"
    )
    expect(page.locator(".quality-coverage")).to_contain_text("Complete day")
    expect(page.locator(".quality-coverage")).to_contain_text("25 of 25 hours measured")
    if not score_eligible:
        expect(page.locator("#quality-charts > article > .balance-warning")).to_have_text(
            "Controlled-load timing is not comparable"
        )
    assert (
        page.evaluate(
            "Chart.getChart(document.getElementById('quality-chart-0')).data.datasets[1].data.map(row => row.y)"
        )
        == [0.25] * 25
    )
    assert page.locator("#quality-chart-0").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.map(dataset => dataset.data.map(row => row.y))"
    ) == [[0.2] * 25, [0.25] * 25]
    expect(page.get_by_text("How model selection works", exact=True)).to_have_count(0)
    expect(page.get_by_text("How forecasts and accuracy work", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.screenshot(path=str(capture / f"unified-forecasts-{suffix}-yesterday.png"), full_page=True)


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize(
    "app_page",
    [
        {"width": 1440, "height": 1000},
        {"width": 768, "height": 1024},
        {"width": 390, "height": 844},
    ],
    indirect=True,
)
def test_solar_satellite_reference_units_coverage_and_shared_subject_selection(app_page):
    """One source at a time retains its day, gaps and original choice while satellite visibility changes no scores."""
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    start = datetime(2026, 10, 25, 23, tzinfo=UTC)
    page.clock.set_fixed_time(start + timedelta(hours=28))
    predicted = (
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0.04,
        0.18,
        0.42,
        0.68,
        0.82,
        0.90,
        0.80,
        0.62,
        0.38,
        0.15,
        0.02,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    measured = (
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0.03,
        0.17,
        0.36,
        0.61,
        0.77,
        None,
        0.66,
        0.53,
        0.31,
        0.11,
        0.01,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    forecast_ghi = (
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        20,
        85,
        180,
        295,
        370,
        405,
        360,
        270,
        165,
        65,
        8,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    satellite_ghi = (
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        15,
        80,
        150,
        260,
        345,
        None,
        285,
        235,
        135,
        45,
        4,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    rows = [
        {
            "start": (start + timedelta(hours=index)).isoformat(),
            "end": (start + timedelta(hours=index + 1)).isoformat(),
            "predicted_kwh": predicted[index],
            "measured_kwh": measured[index],
            "error_kwh": None,
            "evaluation_issue": "Incomplete measurements",
        }
        for index in range(24)
    ]
    metrics = dict.fromkeys(("mae_kwh", "bias_kwh", "rmse_kwh", "wape_percent"))
    original = {
        "id": "roof",
        "scope": "solar",
        "title": "South roof",
        "model": "ridge-v2",
        "model_label": "ML forecast",
        "forecast_version": "a" * 64,
        "issued_at": (start - timedelta(minutes=5)).isoformat(),
        "last_received_at": None,
        "measurement_boundary": "AC",
        "rows": rows,
        "metrics": metrics,
        "coverage": {"hours": 24, "measured_hours": 23, "paired_hours": 23},
        "daily": {
            "predicted_kwh": 5.01,
            "measured_kwh": None,
            "error_kwh": None,
            "evaluation_issue": "Incomplete measurements",
        },
    }
    alternative = {
        **original,
        "model": "pvwatts-v2",
        "model_label": "PVWatts",
        "forecast_version": "physical",
        "rows": [
            {**row, "predicted_kwh": round(predicted[index] * 1.18, 4)}
            for index, row in enumerate(rows)
        ],
        "daily": {**original["daily"], "predicted_kwh": 5.9118},
    }
    original["alternatives"] = [alternative]
    original["radiation"] = {
        "source": "Open-Meteo: DWD / EUMETSAT MTG",
        "complete_until": (start + timedelta(hours=12)).isoformat(),
        "received_at": (start + timedelta(hours=24, minutes=20)).isoformat(),
        "observed_hours": 23,
        "hours": 24,
        "issue": None,
        "rows": [
            {
                "start": row["start"],
                "end": row["end"],
                "forecast_w_m2": forecast_ghi[index],
                "satellite_w_m2": satellite_ghi[index],
            }
            for index, row in enumerate(rows)
        ],
    }
    household = {
        **original,
        "id": "household",
        "scope": "household",
        "title": "Household consumption",
        "radiation": None,
        "alternatives": [],
    }
    body = {
        "day": "2026-10-26",
        "start": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
        "captured_at": start.isoformat(),
        "as_of": (start + timedelta(days=2)).isoformat(),
        "timezone": "Europe/Amsterdam",
        "issues": [],
        "series": [household, original],
        "current_models": {
            "solar": {
                "configuration": {"sources": [{"source_id": "roof", "entity_id": "sensor.solar"}]},
                "source_model_labels": {"roof": "Current HA forecast"},
                "source_selection": {
                    "roof": {
                        "requested": "ha",
                        "active": "ha",
                        "complete_days": 1,
                        "required_days": 3,
                        "issue": None,
                        "comparison_days": ["2026-10-24"],
                        "comparison_exclusions": [
                            {"day": "2026-10-25", "reason": "missing_measurements"}
                        ],
                        "daily_total_comparison": {
                            "days": ["2026-10-24"],
                            "required_days": 7,
                            "measured_kwh": 10,
                            "models": {
                                "ml": {
                                    "bias_kwh": -3,
                                    "absolute_error_kwh": 3,
                                    "materially_underestimated_days": 1,
                                },
                                "pvwatts": {
                                    "bias_kwh": 1,
                                    "absolute_error_kwh": 1,
                                    "materially_underestimated_days": 0,
                                },
                                "ha": {
                                    "bias_kwh": 2,
                                    "absolute_error_kwh": 2,
                                    "materially_underestimated_days": 0,
                                },
                            },
                        },
                    }
                },
            }
        },
    }
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(json={"days": ["2026-10-26"], "next_before": None}),
    )
    page.route("**/v1/forecast-quality/days/2026-10-26", lambda route: route.fulfill(json=body))
    page.route(
        "**/v1/models",
        lambda route: route.fulfill(
            json={"models": [], "issue": None, "explanation": "Independent energy evaluation"}
        ),
    )
    page.goto(url + "/quality")
    if not urlsplit(url).path:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#quality-charts h2")).to_have_text("Household consumption")
    solar = page.get_by_role("button", name="Solar", exact=True)
    solar.focus()
    solar.press("Space")
    expect(page.locator("#quality-charts h2")).to_have_text("South roof")
    expect_day(page, "2026-10-26")
    expect(page.get_by_label("Solar source", exact=True)).to_have_value("roof")
    expect(page.locator("#quality-charts canvas")).to_have_count(1)
    expect(page.get_by_role("link", name="Appliances settings", exact=True)).to_be_hidden()
    expect(page.get_by_text("Hourly evidence and provenance", exact=True)).to_have_count(0)
    expect(page.get_by_text("How model selection works", exact=True)).to_have_count(0)
    expect(page.get_by_text("How forecasts and accuracy work", exact=True)).to_have_count(0)
    expect(
        page.get_by_role("table", name="Model comparison for this day", exact=True)
    ).to_be_visible()
    # The current choice is deliberately different from the frozen day's ML forecast.
    current = page.locator(".quality-current-summary")
    expect(page.locator(".quality-original-model")).to_have_text(
        "Forecast used on 10/26/2026: ML forecast"
    )
    expect(current).to_contain_text("Active forecast model now: Current HA forecast")
    expect(current).to_contain_text("Manually selected forecast source.")
    assert current.bounding_box()["y"] < page.locator(".quality-daily").bounding_box()["y"]
    expect(page.locator(".quality-current-model")).not_to_have_attribute("open", "")
    page.get_by_text("Current model and learning", exact=True).click()
    comparison = page.get_by_role("region", name="Complete forecast comparison", exact=True)
    expect(comparison).to_contain_text("Daily totals: 1 of 7 comparable days.")
    expect(comparison).to_contain_text("Machine learning: -3 kWh total forecast difference")
    comparison.get_by_text("Why days were excluded", exact=True).click()
    expect(
        comparison.get_by_text("10/25/2026: Production measurements are incomplete.")
    ).to_be_visible()
    capture = Path("results/ci/solar-forecast-diagnostics")
    capture.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(capture / f"forecast-quality-{page.viewport_size['width']}.png"), full_page=True
    )
    page.get_by_text("Current model and learning", exact=True).click()
    evidence = show_hour(page, 12)
    expect(evidence).to_contain_text("Measured: Unavailable kWh")
    expect(evidence).to_contain_text("ML forecast: Original choice: 0.900 kWh")
    expect(evidence).to_contain_text("PVWatts: 1.062 kWh")
    expect(evidence).to_contain_text("Forecast irradiance: 405.000 W/m²")
    expect(evidence).to_contain_text("Actual irradiance (satellite): Unavailable W/m²")
    expect(evidence).to_contain_text("Evaluation: Incomplete measurements")
    expect(evidence).to_contain_text("Forecast version: " + "a" * 64)
    expect(evidence).to_contain_text("Measurement boundary: AC")
    assert evidence.bounding_box()["width"] <= page.viewport_size["width"]
    page.locator("#quality-chart-0").press("Escape")
    expect(evidence).to_be_hidden()
    canvas = page.locator("#quality-chart-0")
    point = canvas.evaluate(
        "canvas => Chart.getChart(canvas).getDatasetMeta(1).data[12].getCenterPoint()"
    )
    if page.viewport_size["width"] < 500:
        canvas.tap(position={"x": point["x"], "y": point["y"]})
        box = evidence.bounding_box()
        assert box["y"] >= 84
        assert box["y"] + box["height"] <= page.viewport_size["height"]
    else:
        canvas.hover(position={"x": point["x"], "y": point["y"]})
        canvas.click(position={"x": point["x"], "y": point["y"]})
    expect(evidence).to_be_visible()
    expect(evidence).to_contain_text("ML forecast: Original choice: 0.900 kWh")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/quality-hour-tooltip-{page.viewport_size['width']}-{'ingress' in url}.png",
        full_page=True,
    )
    evidence.get_by_role("button", name="Close hourly evidence", exact=True).click()
    expect(evidence).to_be_hidden()
    marker = page.evaluate(
        "value => new Date(value).toLocaleString(undefined, {year:'numeric', month:'numeric', day:'numeric', hour:'numeric', minute:'2-digit', timeZone:'Europe/Amsterdam'})",
        original["radiation"]["complete_until"],
    )
    expect(page.locator(".quality-radiation-status")).to_contain_text(
        "Satellite data complete through: " + marker
    )
    expect(page.locator(".quality-radiation-status")).to_contain_text("Coverage: 23 of 24 hours")
    expect(page.locator(".quality-radiation-source")).to_contain_text(
        "Actual irradiance is estimated from"
    )
    comparison = page.get_by_role("region", name="Model comparison for this day", exact=True)
    before = comparison.inner_text()
    state = page.locator("#quality-chart-0").evaluate(
        """canvas => {const chart=Chart.getChart(canvas);return {types:chart.data.datasets.map(row=>row.type), active:chart.data.datasets[1].borderDash, alternative:chart.data.datasets[2].borderDash, outline:chart.data.datasets[0].borderWidth, right:chart.options.scales.radiation.display};}"""
    )
    assert state == {
        "types": ["bar", "line", "line", "line", "line"],
        "active": [],
        "alternative": [8, 4],
        "outline": 0,
        "right": True,
    }
    forecast_line = page.get_by_role("checkbox", name="Forecast irradiance", exact=True)
    satellite_line = page.get_by_role("checkbox", name="Actual irradiance (satellite)", exact=True)
    expect(forecast_line).to_be_checked()
    expect(satellite_line).to_be_checked()
    assert page.locator("#quality-chart-0").evaluate(
        """canvas => {const chart=Chart.getChart(canvas);return chart.options.scales.radiation.display && chart.options.scales.radiation.title.text.includes('W/m²') && chart.data.datasets[3].borderColor !== chart.data.datasets[4].borderColor && chart.data.datasets[4].data[0].y === 0 && chart.data.datasets[4].data[12].y === null && chart.data.datasets[4].spanGaps === false;}"""
    )
    assert comparison.inner_text() == before
    width = page.viewport_size["width"]
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=f"results/ci/satellite-reference-{width}-{'ingress' in url}.png", full_page=True
    )
    forecast_line.uncheck()
    satellite_line.uncheck()
    assert not canvas.evaluate("canvas => Chart.getChart(canvas).options.scales.radiation.display")
    satellite_line.check()
    assert canvas.evaluate("canvas => Chart.getChart(canvas).options.scales.radiation.display")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert (
        solar.evaluate("button => getComputedStyle(button).backgroundColor") == "rgba(0, 0, 0, 0)"
    )
    assert solar.evaluate("button => getComputedStyle(button).borderBottomWidth") == "3px"
    page.get_by_role("button", name="Devices", exact=True).click()
    expect(page.locator("#quality-charts canvas")).to_have_count(0)
    expect_day(page, "2026-10-26")
    page.get_by_role("button", name="Solar", exact=True).click()
    expect(page.get_by_label("Solar source", exact=True)).to_have_value("roof")
    original["radiation"]["issue"] = (
        "Satellite refresh failed. Retained observations are shown. Retry after ten minutes."
    )
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(page.locator(".quality-radiation-source")).to_contain_text("Satellite refresh failed")
    expect(page.locator(".quality-daily")).to_contain_text("Not scored yet")
    choice = body["current_models"]["solar"]["source_selection"]["roof"]
    choice.update(
        requested="automatic",
        active="pvwatts",
        issue="Selected forecast unavailable. PVWatts is active",
    )
    body["current_models"]["solar"]["source_model_labels"]["roof"] = "PVWatts fallback"
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(current).to_contain_text("Active forecast model now: PVWatts fallback")
    expect(current).to_contain_text("Selected forecast unavailable. PVWatts is active")
    choice["issue"] = None
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(current).to_contain_text("Collecting comparable forecast evidence")
    choice.update(active="ml", admission_basis="historical_forecast_backtest", required_days=0)
    body["current_models"]["solar"]["source_model_labels"]["roof"] = "Current learned forecast"
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(current).to_contain_text("Machine learning passed the historical forecast assessment.")
    expect(page.locator(".quality-current-model")).not_to_have_attribute("open", "")
    assert (
        page.locator("#quality-chart-0").evaluate(
            "canvas => Chart.getChart(canvas).data.datasets[1].data[12].y"
        )
        == 0.90
    )
    page.get_by_role("button", name="Household", exact=True).click()
    expect(page.get_by_role("link", name="Appliances settings", exact=True)).to_be_visible()
    assert not errors
