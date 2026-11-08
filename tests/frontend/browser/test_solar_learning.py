"""Explain real solar learning status without confusing historical training with active forecasts."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import fsum

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


def solar_day(solar):
    """Retain one complete synthetic day with morning cloud, midday production and independent alternatives."""
    start = datetime(2026, 6, 15, tzinfo=UTC)
    measured = (
        (0,) * 6 + (0.03, 0.1, 0.24, 0.4, 0.51, 0.6, 0.55, 0.46, 0.31, 0.18, 0.07, 0.01) + (0,) * 6
    )
    baseline = (
        (0,) * 6 + (0.05, 0.18, 0.4, 0.55, 0.6, 0.62, 0.58, 0.5, 0.36, 0.22, 0.09, 0.02) + (0,) * 6
    )
    learned = (
        (0,) * 6
        + (0.03, 0.11, 0.25, 0.39, 0.52, 0.59, 0.54, 0.45, 0.32, 0.17, 0.07, 0.01)
        + (0,) * 6
    )
    provider = (
        (0,) * 6 + (0.04, 0.15, 0.31, 0.45, 0.56, 0.6, 0.56, 0.47, 0.34, 0.2, 0.08, 0.02) + (0,) * 6
    )
    assert all(len(values) == 24 for values in (measured, baseline, learned, provider))

    def forecast(label, values):
        """Compute displayed totals and hourly errors from this candidate's retained values."""
        differences = [
            predicted - actual for predicted, actual in zip(values, measured, strict=True)
        ]
        return {
            "model_label": label,
            "issued_at": (start - timedelta(minutes=5)).isoformat(),
            "rows": [
                {
                    "start": (start + timedelta(hours=index)).isoformat(),
                    "end": (start + timedelta(hours=index + 1)).isoformat(),
                    "predicted_kwh": predicted,
                    "measured_kwh": actual,
                    "error_kwh": predicted - actual,
                    "evaluation_issue": None,
                }
                for index, (predicted, actual) in enumerate(zip(values, measured, strict=True))
            ],
            "daily": {
                "predicted_kwh": fsum(values),
                "measured_kwh": fsum(measured),
                "error_kwh": fsum(differences),
                "relative_error_percent": fsum(differences) / fsum(measured) * 100,
                "evaluation_issue": None,
            },
            "metrics": {
                "mae_kwh": fsum(abs(error) for error in differences) / 24,
                "bias_kwh": fsum(differences) / 24,
                "rmse_kwh": (fsum(error * error for error in differences) / 24) ** 0.5,
                "wape_percent": fsum(abs(error) for error in differences) / fsum(measured) * 100,
            },
        }

    return {
        "day": "2026-06-15",
        "start": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
        "captured_at": start.isoformat(),
        "as_of": (start + timedelta(days=2)).isoformat(),
        "timezone": "UTC",
        "issues": [],
        "series": [
            {
                **forecast("PVWatts", baseline),
                "id": "roof",
                "scope": "solar",
                "title": "Garden roof",
                "measurement_boundary": "ac",
                "forecast_version": "original-midnight",
                "coverage": {"hours": 24, "measured_hours": 24, "paired_hours": 24},
                "alternatives": [
                    forecast("XGBoost", learned),
                    forecast("Home Assistant", provider),
                ],
            }
        ],
        "current_models": {"solar": solar},
    }


@pytest.mark.parametrize(
    "app_page",
    [{"width": 1280, "height": 900}, {"width": 768, "height": 1024}, {"width": 390, "height": 844}],
    indirect=True,
)
def test_solar_learning_explains_shadow_training_and_retains_status_on_refresh(app_page):
    """Historical forecast accuracy admits ML on desktop/mobile without inventing new captured days."""
    page, url = app_page
    private_issue = "https://private.invalid/?token=IMPORT_SENTINEL_74189"
    rejected = page.request.post(
        url + "/v1/solar/history",
        headers={"Authorization": f"Bearer {TOKEN}"},
        data={"request_version": "unchanged", "error": private_issue},
    )
    assert rejected.status == 400
    assert rejected.text() == "Invalid solar history"
    initialized = [datetime(2026, 5, 12, tzinfo=UTC) + timedelta(weeks=index) for index in range(4)]
    assessment = {
        "status": "insufficient_history",
        "weather_model": "ecmwf_ifs",
        "run_initializations": [value.isoformat() for value in initialized],
        "windows": [],
        "per_origin": [],
        "matched_pairs": 0,
        "unique_hours": 0,
        "created_at": "2026-06-17T00:00:00+00:00",
        "issue": "Complete archived forecast and measurement hours are still needed.",
    }
    solar = {
        "configuration": {"sources": [{"source_id": "roof", "entity_id": "sensor.synthetic_roof"}]},
        "source_names": {"roof": "Garden roof"},
        "source_model_labels": {"roof": "Physical solar model"},
        "calibration_status": {"roof": "waiting_for_accuracy_evidence"},
        "weather_service": {
            "model": "ecmwf_ifs",
            "selection": "latest_available",
            "initialized_at": None,
            "received_at": "2026-06-17T00:00:00+00:00",
            "available_at": None,
        },
        "learning": {
            "roof": {
                "status": "nonlinear_shadow",
                "candidate_model": "solar-xgboost-production-v2",
                "training_hours": 6000,
                "training_samples": 18000,
                "training_days": 730,
                "training_start": "2024-03-04T08:00:00+00:00",
                "training_end": "2026-06-08T18:00:00+00:00",
                "validation_hours": 56,
                "validation_days": 7,
                "validation_start": "2026-06-09T00:00:00+00:00",
                "validation_end": "2026-06-16T00:00:00+00:00",
                "time_trend": {
                    "status": "insufficient_history",
                    "annual_change": None,
                    "candidate_annual_change": None,
                    "reference": None,
                    "folds": [],
                    "reason": "A trend needs about four comparable years. Ordinary ML remains available.",
                },
                "historical_assessment": assessment,
                "baseline_mae_kwh": 0.21,
                "tree_mae_kwh": 0.018,
                "excluded_counts": {"meter_correction": 1},
                "diagnostic_counts": {"irradiance_yield_anomaly": 2},
            }
        },
        "history_import": {
            "sensor.synthetic_roof": {
                "available_from_month": "2021-03-01T00:00:00+00:00",
                "loaded_years": [2021, 2022, 2023, 2024, 2025, 2026],
                "loaded_through": "2026-06-16T00:00:00+00:00",
                "hours": 44800,
                "issue": private_issue,
            }
        },
        "source_selection": {
            "roof": {
                "requested": "ml",
                "active": "pvwatts",
                "complete_days": 0,
                "required_days": 3,
                "admission_basis": "captured_forecasts",
                "mae_kwh": {},
                "issue": "ML is collecting matched forecast accuracy. PVWatts remains active.",
            }
        },
    }
    page.route("**/v1/models", lambda route: route.fulfill(json={"models": [], "issue": None}))
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={
                "days": ["2026-06-15"],
                "next_before": None,
                "current_models": {"solar": solar},
            }
        ),
    )
    page.route(
        "**/v1/forecast-quality/days/2026-06-15", lambda route: route.fulfill(json=solar_day(solar))
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_role("button", name="Solar", exact=True).click()
    source = page.locator("#quality-charts article").filter(has_text="Garden roof")
    source.get_by_text("Current model and learning", exact=True).click()
    expect(source).to_contain_text("Physical solar model")
    expect(source).to_contain_text("Collecting accuracy evidence")
    expect(source).to_contain_text("XGBoost is learning in shadow mode")
    expect(source).to_contain_text(
        "Training: 6000 daylight hours across 730 days. Historical validation: 56 daylight hours across 7 days."
    )
    expect(source).to_contain_text("From month 2021-03")
    expect(source).to_contain_text("2021, 2022, 2023, 2024, 2025, 2026")
    expect(source).to_contain_text("44,800")
    expect(source).to_contain_text("month-level evidence, not the first complete measured hour")
    expect(source).to_contain_text(
        "History import is temporarily unavailable. Last accepted history is retained."
    )
    expect(page.locator("body")).not_to_contain_text("private.invalid")
    expect(page.locator("body")).not_to_contain_text("IMPORT_SENTINEL")
    controlled = source.get_by_role("region", name="Model comparison", exact=True)
    complete = source.get_by_role("region", name="Complete forecast comparison", exact=True)
    weather = source.get_by_role("region", name="Current forecast weather", exact=True)
    historical = source.get_by_role("region", name="Historical forecast backtest", exact=True)
    expect(weather).to_contain_text(
        "Weather model: ECMWF IFS. Selection: Newest available forecast run"
    )
    expect(weather).to_contain_text("Run initialized: Not supplied")
    expect(weather).to_contain_text("Provider publication: Not known")
    expect(historical).to_contain_text("More complete forecast and measurement history is needed")
    expect(controlled).to_contain_text("does not multiply or depend on a PVWatts prediction")
    expect(controlled).to_contain_text(
        "same archived weather forecasts and measured daylight hours"
    )
    expect(controlled).to_contain_text("Reanalysis is not used for training or forecast admission")
    expect(controlled).to_contain_text(
        "18000 forecast/measurement pairs across 6000 unique production hours"
    )
    expect(controlled).to_contain_text("Training and validation measurements all count equally")
    expect(controlled).to_contain_text("No fixed age discount is applied")
    expect(controlled).not_to_contain_text("half")
    expect(controlled).to_contain_text(
        "No trend selected: More comparable seasonal history is needed"
    )
    expect(controlled).to_contain_text("does not establish physical panel degradation")
    expect(controlled).to_contain_text("Training period:")
    expect(controlled).to_contain_text("end exclusive")
    expect(controlled).to_contain_text(
        "Marked for review, still included: Irradiance yield anomaly: 2 hours"
    )
    expect(controlled).to_contain_text("Excluded from learning: Meter correction: 1 hour")
    expect(controlled.get_by_role("row", name="XGBoost 0.018", exact=True)).to_be_visible()
    expect(complete).to_contain_text("Home Assistant provider uses its own weather")
    expect(complete).to_contain_text("determines Automatic selection")
    expect(complete).to_contain_text(
        "Scores appear after the required comparable days are complete"
    )
    expect(controlled).not_to_contain_text("Simple correction")
    expect(source).to_contain_text("Historical training alone does not activate it.")
    expect(source).not_to_contain_text("Comparison unavailable")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("window.scrollTo(0, 0)")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    page.screenshot(
        full_page=True, path=f"results/ci/solar-learning-shadow-{page.viewport_size['width']}.png"
    )
    solar["source_model_labels"]["roof"] = "Independent solar production (XGBoost CPU)"
    solar["calibration_status"]["roof"] = "active"
    assessment.update(
        status="eligible",
        matched_pairs=384,
        unique_hours=288,
        issue=None,
        per_origin=[
            {
                "origin_delay_hours": delay,
                "matched_hours": 192,
                "baseline_mae_kwh": 0.2,
                "model_mae_kwh": 0.02,
                "service_mae_kwh": 0.025,
            }
            for delay in (0, 24)
        ],
        windows=[
            {
                "initialized_at": start.isoformat(),
                "origin": (start + timedelta(hours=delay)).isoformat(),
                "end": (start + timedelta(hours=delay + 48)).isoformat(),
                "origin_delay_hours": delay,
                "matched_hours": 48,
                "baseline_mae_kwh": 0.2,
                "model_mae_kwh": 0.02,
                "service_mae_kwh": 0.025,
            }
            for start in initialized
            for delay in (0, 24)
        ],
    )
    solar["source_selection"]["roof"].update(
        active="ml",
        complete_days=0,
        required_days=0,
        admission_basis="historical_forecast_backtest",
        mae_kwh={"pvwatts": 0.2, "ml": 0.02},
        issue=None,
    )
    solar["history_import"]["sensor.synthetic_roof"]["issue"] = "history_continuity_unavailable"
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(source.locator(".quality-current-model")).to_have_attribute("open", "")
    expect(source).to_contain_text("Independent solar production (XGBoost CPU)")
    expect(source).to_contain_text("The production meter identity or accepted readings changed")
    expect(source).to_contain_text("choose Learning data from for a new production regime")
    expect(source).to_contain_text("Forecast source: ML model: Active: ML model")
    expect(complete).to_contain_text(
        "0 real captured days are available; no new three-day wait is required"
    )
    expect(complete).to_contain_text("Home Assistant still needs a common captured comparison")
    expect(complete).to_contain_text("They are not real captured forecast scores")
    expect(complete.get_by_role("table")).to_have_count(0)
    expect(source).not_to_contain_text("Collecting accuracy evidence")
    expect(historical).to_contain_text("Accuracy requirements passed")
    expect(historical).to_contain_text(
        "384 forecast/measurement pairs across 288 unique production hours"
    )
    expect(historical).to_contain_text(
        "Origins at initialization and 24 hours later are simulations"
    )
    expect(historical).to_contain_text("Planning origin: 0 hours after initialization. 192 matched")
    expect(historical).to_contain_text(
        "Planning origin: 24 hours after initialization. 192 matched"
    )
    expect(historical.get_by_role("row", name="Served forecast 0.025", exact=True)).to_have_count(2)
    windows = historical.get_by_text("Simulated planning windows: 8", exact=True)
    windows.click()
    expect(historical).to_contain_text("provider lead starts at initialization")
    windows.click()
    expect(source).to_contain_text("XGBoost is active")
    expect(source).to_contain_text("No trend selected: More comparable seasonal history is needed")
    expect(source).not_to_contain_text("learning in shadow mode")
    page.evaluate("window.scrollTo(0, 0)")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    page.screenshot(
        full_page=True, path=f"results/ci/solar-learning-active-{page.viewport_size['width']}.png"
    )


def test_solar_learning_distinguishes_validation_coverage_from_training_history(app_page):
    """Coverage and candidate trend status cannot invent scores or activate the current forecast."""
    page, url = app_page
    training = {
        "status": "insufficient_validation_history",
        "training_hours": 3428,
        "training_days": 280,
        "validation_hours": 24,
        "validation_days": 4,
        "baseline_mae_kwh": None,
        "tree_mae_kwh": None,
        "excluded_counts": {"history_gap": 2},
        "diagnostic_counts": {},
    }
    solar = {
        "configuration": {"sources": [{"source_id": "roof", "entity_id": "sensor.synthetic_roof"}]},
        "source_names": {"roof": "Garden roof"},
        "source_model_labels": {"roof": "Physical solar model"},
        "learning": {"roof": training},
    }
    page.route("**/v1/models", lambda route: route.fulfill(json={"models": [], "issue": None}))
    page.route(
        "**/v1/forecast-quality/days",
        lambda route: route.fulfill(
            json={"days": [], "next_before": None, "current_models": {"solar": solar}}
        ),
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_role("button", name="Solar", exact=True).click()
    source = page.locator("#quality-models article").filter(has_text="Garden roof")
    source.get_by_text("Current model and learning", exact=True).click()
    controlled = source.get_by_role("region", name="Model comparison", exact=True)
    expect(controlled).to_contain_text("Collecting usable historical validation")
    expect(controlled).to_contain_text("Training: 3428 daylight hours across 280 days")
    expect(controlled).to_contain_text("Historical validation: 24 daylight hours across 4 days")
    expect(controlled.get_by_role("cell", name="Not yet available", exact=True)).to_have_count(2)
    expect(controlled).not_to_contain_text("Collecting usable training history")
    training.update(status="insufficient_clean_history", training_hours=30, training_days=3)
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(controlled).to_contain_text("Collecting usable training history")
    expect(controlled).not_to_contain_text("Collecting usable historical validation")
    # This independent future-cohort contract exercises an optional trend once
    # enough forecast seasons exist. It is not evidence of today's archive.
    quarters = [datetime(2028 + index // 4, index % 4 * 3 + 1, 1, tzinfo=UTC) for index in range(9)]
    training.update(
        status="nonlinear_shadow",
        time_trend={
            "status": "active",
            "annual_change": -0.02,
            "candidate_annual_change": -0.02,
            "reference": "2030-01-01T00:00:00+00:00",
            "folds": [
                {
                    "training_end": start.isoformat(),
                    "validation_start": start.isoformat(),
                    "validation_end": end.isoformat(),
                    "baseline_mae_kwh": 0.08,
                    "trend_mae_kwh": 0.05,
                    "annual_change": -0.02,
                    "hours": 720,
                    "days": 90,
                }
                for start, end in pairwise(quarters)
            ],
        },
    )
    solar["source_selection"] = {
        "roof": {"requested": "ml", "active": "pvwatts", "complete_days": 0, "required_days": 3}
    }
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(source).to_contain_text("Learned trend selected automatically for the candidate")
    expect(source).to_contain_text("Selected change in yield: -2.0% per year")
    expect(source).to_contain_text("Forecast source: ML model: Active: PVWatts")
    expect(source).to_contain_text("XGBoost is learning in shadow mode")
    source.get_by_text("Seasonal time-trend comparisons: 8", exact=True).click()
    expect(controlled).to_contain_text("Without trend 0.08 kWh/hour. With trend 0.05 kWh/hour")
    training["time_trend"].update(status="not_improved", annual_change=None)
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(controlled).to_contain_text(
        "Tested change in yield: -2.0% per year. This trend is not applied"
    )
