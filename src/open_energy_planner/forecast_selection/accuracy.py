"""Retain pre-midnight forecasts and select demand models from matched measured local days."""

from datetime import date, datetime, timedelta
from math import fsum, isclose
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
DEVICE_ACCURACY_POLICY = "matched-local-day-device-replay-v1"
DEVICE_ERROR_TIE_TOLERANCE_KWH = 1e-9


def _best_device_model(forecasts, windows):
    """Rank paired hourly totals and daily errors without switching on floating-point noise."""
    hourly = {
        name: fsum(window["hours"] * window["mae_kwh"][name] for window in windows)
        for name in forecasts
    }
    best_hourly = min(hourly.values())
    daily = {
        name: fsum(window["daily_energy_error_kwh"][name] for window in windows)
        for name, error in hourly.items()
        if isclose(error, best_hourly, rel_tol=0, abs_tol=DEVICE_ERROR_TIE_TOLERANCE_KWH)
    }
    best_daily = min(daily.values())
    return min(
        (
            name
            for name, error in daily.items()
            if isclose(error, best_daily, rel_tol=0, abs_tol=DEVICE_ERROR_TIE_TOLERANCE_KWH)
        ),
        key=lambda name: (name != "baseline", name),
    )


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


def _update_pending_capture(documents, pointer, prefix, family, timezone, forecasts, now, today):
    tomorrow = today + timedelta(days=1)
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
        return day_window(tomorrow.isoformat(), timezone)[1].isoformat()
    return None


def _live_windows(
    documents,
    prefix,
    family,
    timezone,
    forecasts,
    actual,
    now,
    today,
    *,
    excluded_dates,
    include_replay_metadata,
    result,
):
    windows = []
    for offset in range(MAX_ACCURACY_LOOKBACK_DAYS):
        day = today - timedelta(days=offset)
        hours, end = comparison_hours(day, timezone)
        if end > now:
            continue
        record = read_capture(documents, prefix + day.isoformat() + ".json", family, day, timezone)
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
                **({"source": "live", "hours": len(hours)} if include_replay_metadata else {}),
                "mae_kwh": scores,
                "actual_version": digest(values),
                "forecast_version": digest(record),
                "daily_energy_error_kwh": {
                    name: abs(fsum(predicted) - fsum(values))
                    for name, predicted in record["predictions"].items()
                },
            }
        )
        if not include_replay_metadata and len(windows) == MIN_ACCURACY_DAYS:
            break
    return windows


def choose_daily(
    documents, identity, timezone, forecasts, actual, now, *, excluded_dates=(), replays=None
):
    """Rank device models from matched replay and live days, or apply the household gate.

    Forecasts map model roles to original forecast documents. Baseline is always
    required. Refresh tomorrow's pending capture until midnight. Freeze it on
    the next observation, including after restart, without reconstructing a
    missed midnight. All models use the same days and measurements. Excluded
    Local dates represent controlled use and cannot prove spontaneous behavior.
    Archive corruption keeps the baseline and remains untouched for diagnosis.
    """
    result = {
        "policy": DAILY_ACCURACY_POLICY if replays is None else DEVICE_ACCURACY_POLICY,
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
    if replays is not None:
        for field in (
            "required_windows",
            "required_relative_benefit_percent",
            "required_absolute_benefit_kwh",
        ):
            result.pop(field)
    family = digest([DAILY_ACCURACY_POLICY, identity, timezone, sorted(forecasts)])
    prefix = "accuracy-day-" + family + "-"
    pointer = "accuracy-pending-" + family + ".json"
    today = now.astimezone(ZoneInfo(timezone)).date()
    try:
        pending_until = _update_pending_capture(
            documents, pointer, prefix, family, timezone, forecasts, now, today
        )
        if pending_until is not None:
            result["pending_until"] = pending_until
        windows = _live_windows(
            documents,
            prefix,
            family,
            timezone,
            forecasts,
            actual,
            now,
            today,
            excluded_dates=excluded_dates,
            include_replay_metadata=replays is not None,
            result=result,
        )
        if replays is not None:
            from .device_history import REPLAY_HISTORY_DAYS, REPLAY_SEASONS, replay_season

            live_days = {window["day"] for window in windows}
            for window in replays:
                day_text = window["day"]
                day = date.fromisoformat(day_text)
                if (
                    not 0 < (today - day).days <= REPLAY_HISTORY_DAYS
                    or day_text in live_days
                    or day_text in excluded_dates
                    or "*" in excluded_dates
                    or set(window["mae_kwh"]) != set(forecasts)
                ):
                    continue
                hours, _ = comparison_hours(day, timezone)
                measured = [actual.get(hour) for hour in hours]
                # Drop a corrected day while its replacement score is computed off the live path.
                if (
                    window["hours"] == len(hours)
                    and all(value is not None for value in measured)
                    and digest(measured) == window["actual_version"]
                ):
                    windows.append(window)
            windows.sort(key=lambda window: window["day"], reverse=True)
            seasons = dict.fromkeys(REPLAY_SEASONS, 0)
            for window in windows:
                if window["source"] == "historical_replay":
                    seasons[replay_season(date.fromisoformat(window["day"]))] += 1
            result.update(
                completed_windows=len(windows), windows=windows, historical_seasons=seasons
            )
            if windows and len(forecasts) > 1:
                result.update(
                    selected=_best_device_model(forecasts, windows), status="accuracy_validated"
                )
            return result
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
