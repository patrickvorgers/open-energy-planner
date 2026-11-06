"""Show truthful weather-model readiness beside a full synthetic day on desktop and mobile."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


@pytest.mark.parametrize("app_site", [None, "ingress"], indirect=True)
@pytest.mark.parametrize(
    "status",
    ["collecting_forward_accuracy", "no_consistent_accuracy_benefit", "accuracy_validated"],
)
def test_weather_candidate_waiting_rejected_and_validated_are_separate_from_activation(
    app_page, status
):
    """The real responsive view explains forward evidence without changing its frozen daily graph."""
    page, url = app_page
    start = datetime(2026, 10, 25, 23, tzinfo=UTC)
    validated = status == "accuracy_validated"
    windows = [
        {
            "start": (start - timedelta(days=index + 1)).isoformat(),
            "end": (start - timedelta(days=index)).isoformat(),
            "mae_kwh": {"candidate": 0.04 if validated else 0.2, "simpler": 0.1, "profile": 0.12},
        }
        for index in range(3)
    ]
    learning = {
        "required_relative_benefit_percent": 5,
        "required_absolute_benefit_kwh": 0.001,
        "status": status,
        "candidate": "xgboost-weather-v1",
        "completed_windows": 2 if status == "collecting_forward_accuracy" else 3,
        "required_windows": 3,
        "windows": windows[:2] if status == "collecting_forward_accuracy" else windows,
        "pending_until": (start + timedelta(days=2)).isoformat(),
        "excluded_window": {
            "start": (start - timedelta(days=4)).isoformat(),
            "reason": "incomplete_measurements",
        },
    }
    source = {
        "id": "sensor.heat",
        "title": "Heat pump",
        "model_label": "Original weekday profile",
        "forecast_version": "synthetic-original",
        "issued_at": start.isoformat(),
        "last_received_at": (start + timedelta(days=1)).isoformat(),
        "weather_sensitive": True,
        "weather_features_used": True,
        "composition_selected": False,
        "rows": [
            {
                "start": (start + timedelta(hours=index)).isoformat(),
                "end": (start + timedelta(hours=index + 1)).isoformat(),
                "predicted_kwh": 0.3 if 6 <= index < 10 else 0.5,
                "measured_kwh": 0.4 if 6 <= index < 10 else 0.5,
                "error_kwh": -0.1 if 6 <= index < 10 else 0,
                "evaluation_issue": None,
            }
            for index in range(24)
        ],
        "daily": {
            "predicted_kwh": 11.2,
            "measured_kwh": 11.6,
            "error_kwh": -0.4,
            "relative_error_percent": -100 * 0.4 / 11.6,
            "evaluation_issue": None,
        },
        "metrics": {
            "mae_kwh": 0.4 / 24,
            "bias_kwh": -0.4 / 24,
            "rmse_kwh": (0.04 / 24) ** 0.5,
            "wape_percent": 100 * 0.4 / 11.6,
        },
        "coverage": {"hours": 24, "measured_hours": 24, "paired_hours": 24},
    }
    body = {
        "day": "2026-10-26",
        "start": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
        "captured_at": start.isoformat(),
        "as_of": (start + timedelta(days=2)).isoformat(),
        "timezone": "Europe/Amsterdam",
        "issues": [],
        "series": [source],
        "current_models": {
            "consumers": {
                "options": [{"entity_id": "sensor.heat", "name": "Heat pump"}],
                "result": {
                    "status": "shadow_waiting_for_decision_evidence",
                    "devices": [
                        {
                            "entity_id": "sensor.heat",
                            "forecast": {
                                "model": "xgboost-weather-v1" if validated else "ridge-v2"
                            },
                            "weather_features_used": True,
                            "weather_learning": learning,
                        }
                    ],
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
            json={
                "models": [],
                "issue": None,
                "explanation": "Simulated decision evidence remains separate from forecast accuracy.",
            }
        ),
    )
    page.goto(url + "/quality")
    if not urlsplit(url).path:
        page.get_by_label("Installation API token", exact=True).fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_role("button", name="Devices", exact=True).click()
    page.get_by_text("Current model and learning", exact=True).click()
    details = page.locator(".quality-current-model")
    expect(details).to_contain_text("Component forecast model")
    expect(details).to_contain_text("Shadow waiting for decision evidence")
    expect(details).to_contain_text(
        "Home Assistant retains control of operating mode, comfort and safety."
    )
    expect(details).to_contain_text("Excluded comparison")
    expect(details).to_contain_text("5.0%")
    expect(details).to_contain_text("0.001 kWh per hour")
    if status == "collecting_forward_accuracy":
        expect(details).to_contain_text("2 of 3 complete forward days")
        expect(details).to_contain_text("Collecting later measurements")
    elif validated:
        expect(details).to_contain_text(
            "Household activation still requires the existing decision comparison."
        )
    else:
        expect(details).to_contain_text("The simpler model is retained")
    expect(page.locator("#quality-charts h2")).to_have_text("Heat pump")
    page.get_by_text("Hourly evidence and provenance", exact=True).click()
    expect(page.locator("#quality-charts")).to_contain_text("Original weekday profile")
    page.get_by_text("Hourly evidence and provenance", exact=True).click()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    capture = Path("results/ci")
    capture.mkdir(parents=True, exist_ok=True)
    suffix = f"{page.viewport_size['width']}-{'ingress' if urlsplit(url).path else 'standalone'}"
    page.screenshot(path=str(capture / f"weather-learning-{status}-{suffix}.png"), full_page=True)
