"""Replay selected historical device days with original weather and earlier training outcomes."""

from datetime import timedelta
from hashlib import sha256
from math import fsum
from zoneinfo import ZoneInfo

from open_energy_planner.core.calendar import day_window
from open_energy_planner.core.units import HOURS_PER_DAY
from open_energy_planner.demand.calculation.forecasting import MIN_RIDGE_TRAINING_DAYS
from open_energy_planner.demand.calculation.model import digest, quantity
from open_energy_planner.demand.training import daily_models
from open_energy_planner.forecast_selection.calculation.accuracy import (
    HOUR,
    comparison_hours,
    mean_absolute_error,
)
from open_energy_planner.weather.model import TemperatureHour

REPLAY_BUCKET_DAYS = 28
REPLAY_BUCKETS = 13
REPLAY_POLICY = "device-chronological-weather-replay-v1"


def replay_days(now, timezone, runs=()):
    """Pick at most one full local day per fixed four-week block."""
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    buckets = {}
    for run in sorted(runs, key=lambda item: item.initialized_at, reverse=True):
        origin = max(run.forecast_origin, run.available_at or run.forecast_origin)
        day = origin.astimezone(zone).date() + timedelta(days=1)
        age = (today - day).days - 1
        if not 0 <= age < REPLAY_BUCKET_DAYS * REPLAY_BUCKETS:
            continue
        hours, _ = comparison_hours(day, timezone)
        if hours and {hour for hour in hours} <= {row.start for row in run.hours}:
            buckets.setdefault(day.toordinal() // REPLAY_BUCKET_DAYS, (day, origin, run))
    return tuple(buckets[bucket] for bucket in sorted(buckets, reverse=True)[:REPLAY_BUCKETS])


def calendar_replay_days(now, timezone):
    """Provide equivalent training/test ordering for devices without weather features."""
    today = now.astimezone(ZoneInfo(timezone)).date()
    latest = today - timedelta(days=1 + (today.toordinal() - 1) % REPLAY_BUCKET_DAYS)
    days = dict.fromkeys(
        (
            today - timedelta(days=1),
            *(
                latest - timedelta(days=offset * REPLAY_BUCKET_DAYS)
                for offset in range(REPLAY_BUCKETS)
            ),
        )
    )
    return tuple(
        (day, day_window(day.isoformat(), timezone)[0] - timedelta(hours=12), None)
        for day in tuple(days)[:REPLAY_BUCKETS]
    )


def replay_scores(documents, configuration, samples, temperatures, windows, models, excluded_dates):
    """Score only complete common days, retaining simulated predictions apart from live captures."""
    if not documents or not windows or len(models) < 2 or "*" in excluded_dates:
        return ()
    by_time = {sample.start: sample.residual_kwh for sample in samples}
    latest_origin = max(window[1] for window in windows)
    observed = {row.start: row.celsius for row in temperatures if row.start + HOUR <= latest_origin}
    needs_temperature = any(
        getattr(model, "temperature_enabled", False) for model in models.values()
    )
    family = digest(
        [
            REPLAY_POLICY,
            configuration.version,
            [
                (role, model.model_id, getattr(model, "temperature_enabled", False))
                for role, model in models.items()
            ],
        ]
    )
    path = f"device-replay-{family}.json"
    try:
        envelope = documents.read(path)
        previous = envelope["replays"]
        if (
            not isinstance(previous, dict)
            or len(previous) > REPLAY_BUCKETS
            or envelope["checksum"] != digest(previous)
        ):
            raise ValueError("Invalid archived device replay checksum or coverage")
    except FileNotFoundError:
        previous = {}
    checked = {}
    fingerprint = sha256()
    training_end = 0
    results = []
    for day, origin, run in sorted(windows):
        while training_end < len(samples) and samples[training_end].start + HOUR <= origin:
            sample = samples[training_end]
            fingerprint.update(
                repr(
                    (
                        sample.start,
                        sample.residual_kwh,
                        observed.get(sample.start) if needs_temperature else None,
                    )
                ).encode()
            )
            training_end += 1
        if (
            training_end < MIN_RIDGE_TRAINING_DAYS * HOURS_PER_DAY
            or day.isoformat() in excluded_dates
        ):
            continue
        hours, _ = comparison_hours(day, configuration.timezone)
        measured = [by_time.get(hour) for hour in hours]
        if not hours or any(value is None for value in measured):
            continue
        key = digest([family, origin, run.version if run else None, fingerprint.hexdigest()])
        saved = previous.get(day.isoformat())
        if saved is None or saved["key"] != key:
            prefix = samples[:training_end]
            training_weather = (
                tuple(
                    TemperatureHour(sample.start, observed[sample.start], sample.start + HOUR)
                    for sample in prefix
                    if sample.start in observed
                )
                if needs_temperature
                else ()
            )
            predictions = _predictions(
                configuration,
                prefix,
                origin,
                run,
                hours,
                models,
                training_weather,
                needs_temperature,
            )
            saved = {
                "key": key,
                "predictions": predictions,
                "origin": origin.isoformat(),
                "weather_run": run.version if run else None,
            }
        checked[day.isoformat()] = saved
        predictions = saved["predictions"]
        if predictions is None:
            continue
        if set(predictions) != set(models) or any(
            len(values) != len(hours) for values in predictions.values()
        ):
            raise ValueError("Invalid archived device replay coverage")
        for values in predictions.values():
            for value in values:
                quantity(value)
        results.append(
            {
                "day": day.isoformat(),
                "source": "historical_replay",
                "hours": len(hours),
                "actual_version": digest(measured),
                "forecast_version": digest(saved),
                "mae_kwh": {
                    role: mean_absolute_error(values, measured)
                    for role, values in predictions.items()
                },
                "daily_energy_error_kwh": {
                    role: abs(fsum(values) - fsum(measured)) for role, values in predictions.items()
                },
            }
        )
    if checked != previous:
        documents.write(path, {"replays": checked, "checksum": digest(checked)})
    return tuple(results)


def _predictions(configuration, training, origin, run, hours, models, weather, needs_temperature):
    """Fit on the chronological prefix and forecast using temperatures from the original run."""
    fitted = daily_models(
        None,
        configuration,
        training,
        origin,
        temperatures=weather,
        weather_sensitive=needs_temperature,
        include_weather="weather" in models,
    )
    if not fitted["baseline"].residual_profile or any(
        role not in fitted
        or getattr(fitted[role], "temperature_enabled", False)
        != getattr(model, "temperature_enabled", False)
        for role, model in models.items()
    ):
        return None
    predicted_weather = {row.start: row.celsius for row in run.hours} if run else {}
    try:
        return {
            role: [
                fitted[role].predict(hour, temperature=predicted_weather.get(hour))
                for hour in hours
            ]
            for role in models
        }
    except ValueError:
        return None
