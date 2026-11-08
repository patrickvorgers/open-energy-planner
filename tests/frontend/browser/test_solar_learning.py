"""Explain real solar learning status without confusing historical training with active forecasts."""

from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


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
                "candidate_model": "solar-xgboost-v1",
                "training_hours": 390,
                "validation_hours": 56,
                "baseline_mae_kwh": 0.21,
                "linear_mae_kwh": 0.15,
                "tree_mae_kwh": 0.018,
                "excluded_counts": {"statistical_outlier": 2, "meter_correction": 1},
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
                "days": [],
                "next_before": None,
                "current_models": {"solar": solar},
            }
        ),
    )
    page.goto(url + "/quality")
    page.get_by_label("Installation API token", exact=True).fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_role("button", name="Solar", exact=True).click()
    source = page.locator("#quality-models article").filter(has_text="Garden roof")
    source.get_by_text("Current model and learning", exact=True).click()
    expect(source).to_contain_text("Physical solar model")
    expect(source).to_contain_text("Collecting accuracy evidence")
    expect(source).to_contain_text("Nonlinear candidate is learning in shadow mode")
    expect(source).to_contain_text("Training: 390 daylight hours. Historical validation: 56 hours")
    expect(source).to_contain_text(
        "Statistical outlier: 2 hours. Original measurements are retained."
    )
    expect(source).to_contain_text("Historical training alone does not activate it.")
    expect(source).not_to_contain_text("Comparison unavailable")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        full_page=True, path=f"results/ci/solar-learning-shadow-{page.viewport_size['width']}.png"
    )
    solar["source_model_labels"]["roof"] = "Solar ML (XGBoost CPU)"
    solar["calibration_status"]["roof"] = "active"
    solar["source_selection"]["roof"].update(
        active="ml",
        complete_days=3,
        mae_kwh={"pvwatts": 0.2, "ml": 0.02},
        issue=None,
    )
    page.get_by_role("button", name="Refresh evidence", exact=True).click()
    expect(source.locator("details")).to_have_attribute("open", "")
    expect(source).to_contain_text("Solar ML (XGBoost CPU)")
    expect(source).to_contain_text("Forecast source: ML model: Active: ML model")
    expect(source).to_contain_text("3 of 3 complete days")
    expect(source).to_contain_text("Nonlinear model is active")
    expect(source).not_to_contain_text("learning in shadow mode")
    page.screenshot(
        full_page=True, path=f"results/ci/solar-learning-active-{page.viewport_size['width']}.png"
    )
