"""Evaluate original forecast coverage and accuracy without storage or platform access."""

from datetime import datetime, timedelta
from math import fsum, sqrt

from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.forecast_quality.policy import (
    FORECAST_MODELS,
    HOUR,
    RECORDER_DELAY_MINUTES,
)


def integral(steps, start, end):
    """Integrate original forecast energy in kWh over the aware interval [start, end).

    Steps must be supplied chronologically, with aware ISO start timestamps,
    duration_h in hours and kwh in kWh. Missing or overlapping original coverage returns None rather than
    filling or double-counting energy.
    """
    cursor, total = start, 0.0
    for row in steps:
        left = datetime.fromisoformat(row["start"])
        right = left + timedelta(hours=row["duration_h"])
        left, right = max(start, left), min(end, right)
        if left >= right:
            continue
        if left != cursor:
            return None
        total += (
            row["kwh"] * (right - left).total_seconds() / (SECONDS_PER_HOUR * row["duration_h"])
        )
        cursor = right
    return total if cursor == end else None


def series(
    identity,
    title,
    forecast,
    steps,
    actual,
    start,
    end,
    now,
    *,
    issue=None,
    receipt=None,
    maximum_comparable_kwh=None,
):
    """Score complete hours forecast before their start using comparable measurements.

    Args:
        identity: Stable series identifier.
        title: Display title.
        forecast: Original model/version document with timezone-aware ISO issued_at.
        steps: Original steps with aware ISO start, duration_h in hours and kwh in kWh.
        actual: Mapping from aware UTC hour starts to measured energy in kWh.
        start: Aware inclusive start aligned to whole UTC hours.
        end: Aware evaluation boundary. Complete hours whose start precedes end
            are included, even if their end extends beyond it. Align to a whole
            UTC hour when an exclusive comparison boundary is required.
        now: Aware evaluation time. Recently completed hours retain the Recorder delay.
        issue: Optional reason that excludes every hour from scoring.
        receipt: Optional measurement receipt time copied to last_received_at.
        maximum_comparable_kwh: Optional hourly clipping threshold in kWh.
            Observations at or above it are ineligible.

    Returns:
        A series document with original forecast identity, hourly rows, coverage
        counts and metrics. Rows expose predicted/measured energy and their
        difference in kWh. Positive bias means overprediction. MAE, bias and RMSE
        use kWh. WAPE is a percentage. All metrics are None without eligible hours.
        WAPE is also None when total eligible measured energy is non-positive.
    """
    issued = datetime.fromisoformat(forecast["issued_at"])
    completed = now - timedelta(minutes=RECORDER_DELAY_MINUTES)
    rows, errors = [], []
    cursor = start
    while cursor < end:
        prediction = integral(steps, cursor, cursor + HOUR)
        observation = actual.get(cursor) if cursor + HOUR <= completed else None
        reason = issue
        if prediction is None:
            reason = "Original forecast does not cover this complete hour"
        elif issued > cursor:
            reason = "Forecast was issued after this hour started"
        elif observation is None:
            reason = "Completed-hour measurement unavailable"
        elif maximum_comparable_kwh is not None and observation >= maximum_comparable_kwh:
            reason = "Clipped AC production cannot establish available PV"
        error = prediction - observation if reason is None else None
        if error is not None:
            errors.append((error, observation))
        rows.append(
            {
                "start": cursor.isoformat(),
                "end": (cursor + HOUR).isoformat(),
                "predicted_kwh": prediction,
                "measured_kwh": observation,
                "error_kwh": error,
                "evaluation_issue": reason,
            }
        )
        cursor += HOUR
    absolute = fsum(abs(error) for error, _value in errors)
    measured_total = fsum(value for _error, value in errors)
    return {
        "id": identity,
        "title": title,
        "model": forecast["model"],
        "model_label": FORECAST_MODELS.get(forecast["model"], forecast["model"]),
        "forecast_version": forecast["version"],
        "issued_at": forecast["issued_at"],
        "last_received_at": receipt,
        "comparison_issue": issue,
        "rows": rows,
        "coverage": {
            "hours": len(rows),
            "measured_hours": sum(row["measured_kwh"] is not None for row in rows),
            "paired_hours": len(errors),
        },
        "metrics": {
            "mae_kwh": absolute / len(errors) if errors else None,
            "bias_kwh": fsum(error for error, _value in errors) / len(errors) if errors else None,
            "rmse_kwh": sqrt(fsum(error * error for error, _value in errors) / len(errors))
            if errors
            else None,
            "wape_percent": absolute / measured_total * 100 if measured_total > 0 else None,
        },
    }
