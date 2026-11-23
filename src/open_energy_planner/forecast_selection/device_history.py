"""Replay selected historical device days with original weather and earlier training outcomes."""

import logging
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from functools import partial
from hashlib import sha256
from math import fsum
from statistics import median
from weakref import WeakKeyDictionary
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
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog
from open_energy_planner.weather.model import TemperatureHour

REPLAY_HISTORY_DAYS = 364
REPLAY_WEEKDAYS_PER_SEASON = 4
REPLAY_WEEKEND_DAYS_PER_SEASON = 2
REPLAY_SEASONS = ("winter", "spring", "summer", "autumn")
REPLAY_CALENDAR_LEAD_HOURS = 12
REPLAY_WEATHER_HISTORY_DAYS = 366
REPLAY_POLICY = "device-chronological-weather-replay-v1"
_LOGGER = logging.getLogger(__name__)
_REPLAY_LOGS = WeakKeyDictionary()


def replay_weather_history_floor(now):
    """Keep the original forecast archive request stable throughout each UTC day."""
    day = now.astimezone(UTC).date() - timedelta(days=REPLAY_WEATHER_HISTORY_DAYS)
    return datetime.combine(day, time.min, tzinfo=UTC)


def replay_days(now, timezone, runs=()):
    """Keep the freshest complete original weather run for each eligible local day."""
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    days = {}
    for run in sorted(runs, key=lambda item: item.initialized_at, reverse=True):
        origin = max(run.forecast_origin, run.available_at or run.forecast_origin)
        day = origin.astimezone(zone).date() + timedelta(days=1)
        age = (today - day).days - 1
        if not 0 <= age < REPLAY_HISTORY_DAYS or day in days:
            continue
        hours, _ = comparison_hours(day, timezone)
        if hours and {hour for hour in hours} <= {row.start for row in run.hours}:
            days[day] = (day, origin, run)
    return tuple(sorted(days.values()))


def calendar_replay_days(now, timezone):
    """Provide equivalent training/test ordering for devices without weather features."""
    today = now.astimezone(ZoneInfo(timezone)).date()
    days = (today - timedelta(days=offset) for offset in range(1, REPLAY_HISTORY_DAYS + 1))
    return tuple(
        (
            day,
            day_window(day.isoformat(), timezone)[0] - timedelta(hours=REPLAY_CALENDAR_LEAD_HOURS),
            None,
        )
        for day in days
    )


def replay_season(day):
    """Name the meteorological season of a local test day."""
    return REPLAY_SEASONS[day.month % 12 // 3]


def _temperature(day, run, timezone):
    """Rank days by forecast temperature without consulting measured energy."""
    hours, _ = comparison_hours(day, timezone)
    expected = set(hours)
    return fsum(row.celsius for row in run.hours if row.start in expected) / len(hours)


def _priority(item, *, weekend, used, typical, warmer, first):
    """Prefer unused weekdays and alternating cold and mild forecast temperatures."""
    day, _, _ = item[0]
    offset = day.weekday() - (5 if weekend else 0)
    return (
        day.weekday() in used,
        (item[1] >= typical) != warmer,
        -item[1] if warmer else item[1],
        (offset - first) % (2 if weekend else 5),
        -day.toordinal(),
    )


def spread_replays(candidates, timezone, score, *, stopping=lambda: False):
    """Fill seasonal weekday/weekend quotas, replacing unavailable dates within their group."""
    groups = defaultdict(list)
    for candidate in candidates:
        day, _, run = candidate
        groups[(replay_season(day), day.weekday() >= 5)].append(
            (candidate, _temperature(day, run, timezone) if run else 0.0)
        )
    results = []
    for (season, weekend), remaining in sorted(groups.items()):
        limit = REPLAY_WEEKEND_DAYS_PER_SEASON if weekend else REPLAY_WEEKDAYS_PER_SEASON
        typical = median(temperature for _, temperature in remaining)
        # Rotate the starting weekday so small season groups do not favor one calendar day.
        first = REPLAY_SEASONS.index(season)
        used = set()
        for index in range(limit):
            priority = partial(
                _priority,
                weekend=weekend,
                used=used,
                typical=typical,
                warmer=bool(index % 2),
                first=first,
            )
            while remaining and not stopping():
                choice = min(remaining, key=priority)
                remaining.remove(choice)
                result = score(choice[0])
                if result is not None:
                    used.add(choice[0][0].weekday())
                    results.append(result)
                    break
    return tuple(sorted(results, key=lambda row: row["day"]))


def replay_family(configuration, models):
    """Bind durable predictions to their device configuration and model identities."""
    return digest(
        [
            REPLAY_POLICY,
            configuration.version,
            [
                (role, model.model_id, getattr(model, "temperature_enabled", False))
                for role, model in models.items()
            ],
        ]
    )


def replay_scores(
    documents,
    configuration,
    samples,
    temperatures,
    windows,
    models,
    excluded_dates,
    *,
    controlled=lambda day: False,
    stopping=lambda: False,
):
    """Score only complete common days, retaining simulated predictions apart from live captures."""
    if not documents or not windows or len(models) < 2 or "*" in excluded_dates:
        return ()
    by_time = {sample.start: sample.residual_kwh for sample in samples}
    latest_origin = max(window[1] for window in windows)
    observed = {row.start: row.celsius for row in temperatures if row.start + HOUR <= latest_origin}
    needs_temperature = any(
        getattr(model, "temperature_enabled", False) for model in models.values()
    )
    family = replay_family(configuration, models)
    path = f"device-replay-{family}.json"
    try:
        envelope = documents.read(path)
        previous = envelope["replays"]
        if (
            not isinstance(previous, dict)
            or len(previous) > REPLAY_HISTORY_DAYS
            or envelope["checksum"] != digest(previous)
        ):
            raise ValueError("Invalid archived device replay checksum or coverage")
    except FileNotFoundError:
        previous = {}
    checked = {
        day.isoformat(): previous[day.isoformat()]
        for day, _, _ in windows
        if day.isoformat() in previous
    }
    if checked != previous:
        documents.write(path, {"replays": checked, "checksum": digest(checked)})
    fingerprint = sha256()
    training_end = 0
    eligible = {}
    logs = _REPLAY_LOGS.setdefault(documents, {})
    log = logs.setdefault(family, BackgroundLog(_LOGGER, "Device forecast history replay"))
    attempted_fit = failed = False
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
        eligible[day] = training_end, measured, hours, key

    def score(candidate):
        """Recover compatible predictions or fit just this selected replacement day."""
        nonlocal attempted_fit, failed
        day, origin, run = candidate
        if controlled(day):
            return None
        training_end, measured, hours, key = eligible[day]
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
            attempted_fit = True
            try:
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
            except (ImportError, OSError, RuntimeError):
                if not failed:
                    log.exception(
                        "Skipping affected comparison days. Retrying on the next forecast update.",
                        expected=True,
                    )
                failed = True
                return None
            saved = {
                "key": key,
                "predictions": predictions,
                "origin": origin.isoformat(),
                "weather_run": run.version if run else None,
            }
            checked[day.isoformat()] = saved
            documents.write(path, {"replays": checked, "checksum": digest(checked)})
        predictions = saved["predictions"]
        if predictions is None:
            return None
        if set(predictions) != set(models) or any(
            len(values) != len(hours) for values in predictions.values()
        ):
            raise ValueError("Invalid archived device replay coverage")
        for values in predictions.values():
            for value in values:
                quantity(value)
        return {
            "day": day.isoformat(),
            "source": "historical_replay",
            "hours": len(hours),
            "actual_version": digest(measured),
            "forecast_version": digest(saved),
            "mae_kwh": {
                role: mean_absolute_error(values, measured) for role, values in predictions.items()
            },
            "daily_energy_error_kwh": {
                role: abs(fsum(values) - fsum(measured)) for role, values in predictions.items()
            },
        }

    results = spread_replays(
        (window for window in windows if window[0] in eligible),
        configuration.timezone,
        score,
        stopping=stopping,
    )
    if attempted_fit and not failed and log.failure_key is not None:
        log.success("Historical device forecasts can be evaluated again.")
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
        raise_on_transient_failure=True,
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
