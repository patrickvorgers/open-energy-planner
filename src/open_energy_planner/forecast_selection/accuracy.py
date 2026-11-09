"""Retain pre-midnight forecasts and select demand models from matched measured local days."""

from datetime import datetime, timedelta
from math import fsum
from zoneinfo import ZoneInfo

from open_energy_planner.core.calendar import day_window
from open_energy_planner.demand.calculation.model import digest, quantity
from open_energy_planner.forecast_quality.calculation.metrics import integral
from open_energy_planner.forecast_selection.calculation.accuracy import (
    HOUR,
    MAX_ACCURACY_LOOKBACK_DAYS,
    MIN_ABSOLUTE_ACCURACY_GAIN_KWH,
    MIN_ACCURACY_DAYS,
    MIN_RELATIVE_ACCURACY_GAIN,
    comparison_hours,
    improves,
    mean_absolute_error,
)

DAILY_ACCURACY_POLICY = "matched-local-day-demand-v1"


def read_capture(documents, path, family, day, timezone):
    """Validate ownership, chronology and complete immutable forecast vectors before selection."""
    if not documents.exists(path):
        return None
    envelope = documents.read(path)
    record = envelope["capture"]
    hours, _end = comparison_hours(day, timezone)
    start, _end = day_window(day.isoformat(), timezone)
    issued = datetime.fromisoformat(record["issued_at"])
    if (
        envelope["version"] != digest(record)
        or record["family"] != family
        or record["day"] != day.isoformat()
        or record["timezone"] != timezone
        or issued.utcoffset() is None
        or issued > start
        or record["hours"] != [hour.isoformat() for hour in hours]
        or "baseline" not in record["predictions"]
    ):
        raise ValueError("Daily forecast evidence differs")
    for values in record["predictions"].values():
        if len(values) != len(hours):
            raise ValueError("Daily forecast coverage differs")
        for value in values:
            quantity(value)
    return record


def choose_daily(documents, identity, timezone, forecasts, actual, now, *, excluded_dates=()):
    """Choose a challenger only after three matching complete local-day improvements.

    Forecasts map model roles to original forecast documents. Baseline is always
    required. Refresh tomorrow's pending capture until midnight. Freeze it on
    the next observation, including after restart, without reconstructing a
    missed midnight. All models use the same days and measurements. Excluded
    Local dates represent controlled use and cannot prove spontaneous behavior.
    Archive corruption keeps the baseline and remains untouched for diagnosis.
    """
    result = {
        "policy": DAILY_ACCURACY_POLICY,
        "selected": "baseline",
        "status": "collecting_forward_accuracy"
        if len(forecasts) > 1
        else "training_history_required",
        "completed_windows": 0,
        "required_windows": MIN_ACCURACY_DAYS,
        "required_relative_benefit_percent": MIN_RELATIVE_ACCURACY_GAIN * 100,
        "required_absolute_benefit_kwh": MIN_ABSOLUTE_ACCURACY_GAIN_KWH,
        "windows": [],
        "timezone": timezone,
    }
    if documents is None:
        return {**result, "status": "forward_archive_required"}
    family = digest([DAILY_ACCURACY_POLICY, identity, timezone, sorted(forecasts)])
    prefix = "accuracy-day-" + family + "-"
    pointer = "accuracy-pending-" + family + ".json"
    today = now.astimezone(ZoneInfo(timezone)).date()
    tomorrow = today + timedelta(days=1)
    try:
        if documents.exists(pointer):
            pending = documents.read(pointer)
            pending_day = datetime.fromisoformat(pending["capture"]["day"]).date()
            # Apply the same evidence validation to mutable and frozen captures.
            read_capture(documents, pointer, family, pending_day, timezone)
            if pending_day <= today:
                target = prefix + pending_day.isoformat() + ".json"
                if not documents.exists(target):
                    documents.write(target, pending)
        hours, _end = comparison_hours(tomorrow, timezone)
        predictions = {}
        for name, forecast in forecasts.items():
            issued = datetime.fromisoformat(forecast["issued_at"])
            if issued.utcoffset() is None or issued > now:
                raise ValueError("Future forecast issue time")
            values = [integral(forecast["steps"], hour, hour + HOUR) for hour in hours]
            if any(value is None for value in values):
                continue
            for value in values:
                quantity(value)
            predictions[name] = values
        if set(predictions) == set(forecasts) and "baseline" in predictions:
            capture = {
                "family": family,
                "day": tomorrow.isoformat(),
                "timezone": timezone,
                "issued_at": now.isoformat(),
                "hours": [hour.isoformat() for hour in hours],
                "predictions": predictions,
                "forecasts": forecasts,
            }
            documents.write(pointer, {"capture": capture, "version": digest(capture)})
            result["pending_until"] = day_window(tomorrow.isoformat(), timezone)[1].isoformat()
        windows = []
        for offset in range(MAX_ACCURACY_LOOKBACK_DAYS):
            day = today - timedelta(days=offset)
            hours, end = comparison_hours(day, timezone)
            if end > now:
                continue
            record = read_capture(
                documents, prefix + day.isoformat() + ".json", family, day, timezone
            )
            if record is None or set(record["predictions"]) != set(forecasts):
                continue
            if "*" in excluded_dates or day.isoformat() in excluded_dates:
                result["excluded_reason"] = (
                    "control_history_unavailable" if "*" in excluded_dates else "controlled_use"
                )
                continue
            values = [actual.get(hour) for hour in hours]
            if any(value is None for value in values):
                continue
            for value in values:
                quantity(value)
            scores = {
                name: mean_absolute_error(predicted, values)
                for name, predicted in record["predictions"].items()
            }
            windows.append(
                {
                    "day": day.isoformat(),
                    "mae_kwh": scores,
                    "actual_version": digest(values),
                    "forecast_version": digest(record),
                    "daily_energy_error_kwh": {
                        name: abs(fsum(predicted) - fsum(values))
                        for name, predicted in record["predictions"].items()
                    },
                }
            )
            if len(windows) == MIN_ACCURACY_DAYS:
                break
        result.update(completed_windows=len(windows), windows=windows)
        if len(windows) == MIN_ACCURACY_DAYS and len(forecasts) > 1:
            eligible = [
                name
                for name in forecasts
                if name != "baseline"
                and all(
                    improves(window["mae_kwh"][name], window["mae_kwh"]["baseline"])
                    for window in windows
                )
            ]
            if eligible:
                result.update(
                    selected=min(
                        eligible,
                        key=lambda name: fsum(window["mae_kwh"][name] for window in windows),
                    ),
                    status="accuracy_validated",
                )
            else:
                result["status"] = "no_consistent_accuracy_benefit"
    except (OSError, ValueError, TypeError, KeyError):
        return {**result, "selected": "baseline", "status": "comparison_evidence_unreadable"}
    return result
