"""Explain real solar learning status without confusing historical training with active forecasts."""

from datetime import UTC, datetime, timedelta
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
    """Desktop/mobile distinguish the PVWatts baseline, clean shadow training and actual later tree activation."""
    page, url = app_page
    solar = {
        "configuration": {"sources": [{"source_id": "roof", "entity_id": "sensor.synthetic_roof"}]},
        "source_names": {"roof": "Garden roof"},
        "source_model_labels": {"roof": "Physical solar model"},
        "calibration_status": {"roof": "waiting_for_accuracy_evidence"},
        "learning": {
            "roof": {
                "status": "nonlinear_shadow",
                "candidate_model": "solar-xgboost-production-v2",
                "training_hours": 390,
                "training_days": 49,
                "validation_hours": 56,
                "validation_days": 7,
                "baseline_mae_kwh": 0.21,
                "tree_mae_kwh": 0.018,
                "excluded_counts": {"meter_correction": 1},
                "diagnostic_counts": {"irradiance_yield_anomaly": 2},
            }
        },
        "source_selection": {
            "roof": {
                "requested": "ml",
                "active": "pvwatts",
                "complete_days": 1,
                "required_days": 3,
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
        "Training: 390 daylight hours across 49 days. Historical validation: 56 daylight hours across 7 days."
    )
    controlled = source.get_by_role("region", name="Model comparison", exact=True)
    complete = source.get_by_role("region", name="Complete forecast comparison", exact=True)
    expect(controlled).to_contain_text("does not multiply or depend on a PVWatts prediction")
    expect(controlled).to_contain_text("same weather and measured daylight hours")
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
    solar["source_selection"]["roof"].update(
        active="ml",
        complete_days=3,
        mae_kwh={"pvwatts": 0.2, "ml": 0.02},
        issue=None,
    )
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(source.locator(".quality-current-model")).to_have_attribute("open", "")
    expect(source).to_contain_text("Independent solar production (XGBoost CPU)")
    expect(source).to_contain_text("Forecast source: ML model: Active: ML model")
    expect(source).to_contain_text("3 of 3 complete days")
    expect(source).to_contain_text("XGBoost is active")
    expect(source).not_to_contain_text("learning in shadow mode")
    page.evaluate("window.scrollTo(0, 0)")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    page.screenshot(
        full_page=True, path=f"results/ci/solar-learning-active-{page.viewport_size['width']}.png"
    )


def test_solar_learning_distinguishes_validation_coverage_from_training_history(app_page):
    """Enough training can coexist with incomplete validation without inventing candidate scores."""
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
