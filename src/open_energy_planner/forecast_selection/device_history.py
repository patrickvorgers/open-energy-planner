"""Replay selected historical device days with original weather and earlier training outcomes."""

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
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
from open_energy_planner.demand.calculation.model import DemandHour, digest, quantity
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


@dataclass(frozen=True)
class _ReplayTraining:
    """Measurement history and the observed temperatures available for fitting it."""

    samples: Sequence[DemandHour]
    measured: dict[datetime, float]
    observed: dict[datetime, float] | None


@dataclass(frozen=True)
class _ReplayEvidence:
    """Complete held-out measurements and the eligible day's earlier training boundary."""

    hours: tuple[datetime, ...]
    training_end: int
    measured: list[float]
    key: str


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
        results.extend(_score_group(remaining, season, weekend, score, stopping))
    return tuple(sorted(results, key=lambda row: row["day"]))


def _score_group(remaining, season, weekend, score, stopping):
    """Alternate cold and mild days within one season and day-type quota."""
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
        scored = _next_group_score(remaining, priority, score, stopping)
        if scored is not None:
            weekday, result = scored
            used.add(weekday)
            yield result


def _next_group_score(remaining, priority, score, stopping):
    """Take the next valid scored candidate, removing unavailable dates from the group."""
    while remaining and not stopping():
        choice = min(remaining, key=priority)
        remaining.remove(choice)
        result = score(choice[0])
        if result is not None:
            return choice[0][0].weekday(), result
    return None


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
    """Score original weather windows against complete measured days.

    The archive, configuration, measured energy, historical temperatures and
    current model roles are independent inputs. Exclusions, live control and
    stopping are caller-owned admission policies.
    """
    if not documents or not windows:
        return ()
    if len(models) < 2 or "*" in excluded_dates:
        return ()
    measured = {sample.start: sample.residual_kwh for sample in samples}
    latest_origin = max(window[1] for window in windows)
    observed = {row.start: row.celsius for row in temperatures if row.start + HOUR <= latest_origin}
    needs_temperature = any(
        getattr(model, "temperature_enabled", False) for model in models.values()
    )
    training = _ReplayTraining(samples, measured, observed if needs_temperature else None)
    family = replay_family(configuration, models)
    path = f"device-replay-{family}.json"
    previous = _read_replays(documents, path)
    checked = {
        day.isoformat(): previous[day.isoformat()]
        for day, _, _ in windows
        if day.isoformat() in previous
    }
    if checked != previous:
        _write_replays(documents, path, checked)
    logs = _REPLAY_LOGS.setdefault(documents, {})
    log = logs.setdefault(family, BackgroundLog(_LOGGER, "Device forecast history replay"))
    attempted_fit = failed = False
    eligible = _eligible_replays(training, windows, configuration.timezone, family, excluded_dates)

    def retain(day, entry):
        """Commit each finished test before any later fit can fail or be interrupted."""
        checked[day.isoformat()] = entry
        _write_replays(documents, path, checked)

    def score(candidate):
        """Recover compatible predictions or fit just this selected replacement day."""
        nonlocal attempted_fit, failed
        day, origin, run = candidate
        if controlled(day):
            return None
        evidence = eligible[day]
        saved = previous.get(day.isoformat())
        if saved is None or saved["key"] != evidence.key:
            attempted_fit = True
            try:
                predictions = _predictions(configuration, training, candidate, evidence, models)
            except (ImportError, OSError, RuntimeError):
                if not failed:
                    log.exception(
                        "Skipping affected comparison days. Retrying on the next forecast update.",
                        expected=True,
                    )
                failed = True
                return None
            saved = {
                "key": evidence.key,
                "predictions": predictions,
                "origin": origin.isoformat(),
                "weather_run": run.version if run else None,
            }
            if predictions is None:
                retain(day, saved)
                return None
        result = _score_predictions(day, evidence, saved, models)
        if result is not None and result is not saved.get("score"):
            retain(day, {**saved, "score": result})
        return result

    results = spread_replays(
        (window for window in windows if window[0] in eligible),
        configuration.timezone,
        score,
        stopping=stopping,
    )
    if attempted_fit and not failed and log.failure_key is not None:
        log.success("Historical device forecasts can be evaluated again.")
    return results


def _read_replays(documents, path):
    """Read compatible retained data or start empty when the archive is absent."""
    try:
        envelope = documents.read(path)
        previous = envelope["replays"]
        if (
            not isinstance(previous, dict)
            or len(previous) > REPLAY_HISTORY_DAYS
            or envelope["checksum"] != digest(previous)
        ):
            raise ValueError("Invalid archived device replay checksum or coverage")
        return previous
    except FileNotFoundError:
        return {}


def _write_replays(documents, path, entries):
    """Persist derived predictions and scores with their integrity checksum."""
    documents.write(path, {"replays": entries, "checksum": digest(entries)})


def _eligible_replays(training, windows, timezone, family, excluded_dates):
    """Fingerprint training strictly before each origin and require complete measured hours."""
    observed = training.observed
    fingerprint = sha256()
    training_end = 0
    eligible = {}
    for day, origin, run in sorted(windows):
        while (
            training_end < len(training.samples)
            and training.samples[training_end].start + HOUR <= origin
        ):
            sample = training.samples[training_end]
            fingerprint.update(
                repr(
                    (
                        sample.start,
                        sample.residual_kwh,
                        observed.get(sample.start) if observed is not None else None,
                    )
                ).encode()
            )
            training_end += 1
        if (
            training_end < MIN_RIDGE_TRAINING_DAYS * HOURS_PER_DAY
            or day.isoformat() in excluded_dates
        ):
            continue
        hours, _ = comparison_hours(day, timezone)
        measured = [training.measured.get(hour) for hour in hours]
        if not hours or any(value is None for value in measured):
            continue
        key = digest([family, origin, run.version if run else None, fingerprint.hexdigest()])
        eligible[day] = _ReplayEvidence(hours, training_end, measured, key)
    return eligible


def _score_predictions(day, evidence, saved, models):
    """Return the saved score itself when current actuals still match its version."""
    predictions = saved["predictions"]
    if predictions is None:
        return None
    measured = evidence.measured
    actual_version = digest(measured)
    cached = saved.get("score")
    if cached is not None and cached["actual_version"] == actual_version:
        return cached
    if set(predictions) != set(models) or any(
        len(values) != len(evidence.hours) for values in predictions.values()
    ):
        raise ValueError("Invalid archived device replay coverage")
    for values in predictions.values():
        for value in values:
            quantity(value)
    return {
        "day": day.isoformat(),
        "source": "historical_replay",
        "hours": len(evidence.hours),
        "actual_version": actual_version,
        "forecast_version": digest(
            {name: value for name, value in saved.items() if name != "score"}
        ),
        "mae_kwh": {
            role: mean_absolute_error(values, measured) for role, values in predictions.items()
        },
        "daily_energy_error_kwh": {
            role: abs(fsum(values) - fsum(measured)) for role, values in predictions.items()
        },
    }


def _predictions(configuration, training, candidate, evidence, models):
    """Fit on the chronological prefix and forecast using temperatures from the original run."""
    _, origin, run = candidate
    prefix = training.samples[: evidence.training_end]
    observed = training.observed
    weather = (
        tuple(
            TemperatureHour(sample.start, observed[sample.start], sample.start + HOUR)
            for sample in prefix
            if sample.start in observed
        )
        if observed is not None
        else ()
    )
    fitted = daily_models(
        None,
        configuration,
        prefix,
        origin,
        temperatures=weather,
        weather_sensitive=observed is not None,
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
                for hour in evidence.hours
            ]
            for role in models
        }
    except ValueError:
        return None
