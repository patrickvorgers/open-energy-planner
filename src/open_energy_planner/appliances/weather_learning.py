"""Select appliance weather models only from preserved forward forecasts and later measurements."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from statistics import mean

from open_energy_planner.appliances.weather_model import WeatherDemandModel
from open_energy_planner.core.units import HOURS_PER_DAY
from open_energy_planner.demand.calculation.forecasting import WeekdayProfile, predict
from open_energy_planner.demand.calculation.model import digest, quantity

WEATHER_SELECTION_POLICY = "appliance-forward-accuracy-v1"
REQUIRED_ACCURACY_WINDOWS = 3
ACCURACY_WINDOW_HOURS = HOURS_PER_DAY
MIN_RELATIVE_MAE_BENEFIT = 0.05
MIN_ABSOLUTE_MAE_BENEFIT_KWH = 0.001
MAX_EVIDENCE_AGE_DAYS = 90


class WeatherLearningStatus(StrEnum):
    """Closed readiness states exposed by the appliance forecast API and current-model view."""

    TRAINING_REQUIRED = "training_history_required"
    RUNTIME_UNAVAILABLE = "model_runtime_unavailable"
    FORECAST_UNAVAILABLE = "weather_forecast_unavailable"
    ARCHIVE_REQUIRED = "forward_archive_required"
    EVIDENCE_UNREADABLE = "comparison_evidence_unreadable"
    COLLECTING = "collecting_forward_accuracy"
    NO_BENEFIT = "no_consistent_accuracy_benefit"
    VALIDATED = "accuracy_validated"


def _hourly(forecast, start):
    """Sum complete forecast hours without making an hourly mean from partial coverage."""
    result = []
    for offset in range(ACCURACY_WINDOW_HOURS):
        hour = start + timedelta(hours=offset)
        steps = [
            step
            for step in forecast.steps
            if hour <= step.start
            and step.start + timedelta(hours=step.duration_h) <= hour + timedelta(hours=1)
        ]
        if sum(step.duration_h for step in steps) != 1:
            raise ValueError("Incomplete forward prediction hour")
        result.append(sum(step.kwh for step in steps))
    return result


def _immutable(documents, prefix, document):
    """Archive content under its digest before a mutable selection pointer can reference it."""
    version = digest(document)
    path = prefix + version + ".json"
    if documents.exists(path):
        if documents.read(path) != document:
            raise ValueError("Weather learning evidence archive differs")
    else:
        documents.write(path, document)
    return version


def _read_pending(documents, version, family):
    """Check stored bounds, quantities and ownership before using archived forward predictions."""
    if (
        not isinstance(version, str)
        or len(version) != 64
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise ValueError("Invalid weather learning reference")
    document = documents.read("consumer-weather-window-" + version + ".json")
    if digest(document) != version or document["family"] != family:
        raise ValueError("Weather learning evidence identity differs")
    model_document = documents.read("consumer-weather-model-" + document["model_version"] + ".json")
    if digest(model_document) != document["model_version"]:
        raise ValueError("Weather learning model identity differs")
    model = WeatherDemandModel.restore(model_document, document["configuration_version"])
    if (
        model.model_digest != document["model_digest"]
        or model.training_version != document["training_version"]
    ):
        raise ValueError("Weather learning model provenance differs")
    start, end, issued = (
        datetime.fromisoformat(document[field]) for field in ("start", "end", "issued_at")
    )
    if (
        not all(instant.tzinfo is not None for instant in (start, end, issued))
        or issued > start
        or model.trained_until > issued
        or end - start != timedelta(hours=ACCURACY_WINDOW_HOURS)
    ):
        raise ValueError("Invalid weather learning chronology")
    if set(document["predictions"]) != {"candidate", "simpler", "profile"}:
        raise ValueError("Invalid weather learning comparisons")
    for values in document["predictions"].values():
        if not isinstance(values, list) or len(values) != ACCURACY_WINDOW_HOURS:
            raise ValueError("Incomplete weather learning predictions")
        for value in values:
            quantity(value)
    return document


def _assess(documents, pending, samples, now):
    """Score original predictions against a complete later day, retaining actuals and exclusions."""
    start, end = (datetime.fromisoformat(pending[field]) for field in ("start", "end"))
    if now < end or not samples or samples[-1].start + timedelta(hours=1) < end:
        return None
    by_time = {sample.start: sample.residual_kwh for sample in samples}
    actual = [
        by_time.get(start + timedelta(hours=offset)) for offset in range(ACCURACY_WINDOW_HOURS)
    ]
    if any(value is None for value in actual):
        return {
            "start": pending["start"],
            "end": pending["end"],
            "reason": "incomplete_measurements",
        }
    scores = {
        name: mean(
            abs(prediction - measured) for prediction, measured in zip(values, actual, strict=True)
        )
        for name, values in pending["predictions"].items()
    }
    improved = all(
        scores[name] - scores["candidate"]
        > max(MIN_ABSOLUTE_MAE_BENEFIT_KWH, MIN_RELATIVE_MAE_BENEFIT * scores[name])
        for name in ("simpler", "profile")
    )
    result = {
        "start": pending["start"],
        "end": pending["end"],
        "issued_at": pending["issued_at"],
        "mae_kwh": scores,
        "improved": improved,
        "forecast_version": digest(pending),
        "actual_version": digest(
            list(
                zip(
                    [sample.start.isoformat() for sample in samples if start <= sample.start < end],
                    actual,
                    strict=True,
                )
            )
        ),
    }
    _immutable(documents, "consumer-weather-assessment-", {**result, "actual_kwh": actual})
    return result


def select_weather_model(configuration, samples, temperatures, now, simpler, fallback, documents):
    """Keep the simpler forecast until three disjoint forward days beat both simpler alternatives.

    This accuracy gate does not grant household activation or device control.
    Existing financial composition selection remains authoritative. Each family
    is bound to the meter, configuration, feature schema and simpler algorithm.
    Archive failures propagate to the worker rather than claiming saved evidence.
    """
    evidence = {
        "policy": WEATHER_SELECTION_POLICY,
        "candidate": WeatherDemandModel.model_id,
        "selected_model": simpler.model_id,
        "status": WeatherLearningStatus.TRAINING_REQUIRED,
        "completed_windows": 0,
        "required_windows": REQUIRED_ACCURACY_WINDOWS,
        "required_relative_benefit_percent": MIN_RELATIVE_MAE_BENEFIT * 100,
        "required_absolute_benefit_kwh": MIN_ABSOLUTE_MAE_BENEFIT_KWH,
        "windows": [],
    }
    try:
        candidate = WeatherDemandModel.fit(
            configuration, samples, temperatures=temperatures, known_at=now
        )
    except (ImportError, OSError):
        return simpler, fallback, {**evidence, "status": WeatherLearningStatus.RUNTIME_UNAVAILABLE}
    except ValueError:
        return simpler, fallback, evidence
    try:
        forecast = predict(configuration, samples, now, candidate, temperatures=temperatures)
    except ValueError:
        return simpler, fallback, {**evidence, "status": WeatherLearningStatus.FORECAST_UNAVAILABLE}
    if documents is None:
        return simpler, fallback, {**evidence, "status": WeatherLearningStatus.ARCHIVE_REQUIRED}
    family = digest(
        [
            configuration.version,
            WEATHER_SELECTION_POLICY,
            candidate.schema,
            candidate.feature_names,
            simpler.model_id,
        ]
    )
    pointer = "consumer-weather-selection-" + family + ".json"
    state = {"family": family, "windows": [], "pending": None, "excluded_window": None}
    try:
        if documents.exists(pointer):
            stored = documents.read(pointer)
            if (
                stored["family"] != family
                or not isinstance(stored["windows"], list)
                or len(stored["windows"]) > REQUIRED_ACCURACY_WINDOWS
            ):
                raise ValueError("Invalid weather learning state")
            # Recompute every score from original forecasts and current measured
            # history. A stored promotion flag alone is never authority.
            for reference in stored["windows"]:
                original = _read_pending(documents, reference, family)
                assessed = _assess(documents, original, samples, now)
                if (
                    assessed is not None
                    and "reason" not in assessed
                    and datetime.fromisoformat(original["end"])
                    >= now - timedelta(days=MAX_EVIDENCE_AGE_DAYS)
                ):
                    state["windows"].append(reference)
            state["pending"] = stored["pending"]
            state["excluded_window"] = stored.get("excluded_window")
        if state["pending"]:
            pending = _read_pending(documents, state["pending"], family)
            assessed = _assess(documents, pending, samples, now)
            if assessed is not None:
                if "reason" in assessed:
                    state["excluded_window"] = assessed
                else:
                    state["windows"] = [*state["windows"], state["pending"]][
                        -REQUIRED_ACCURACY_WINDOWS:
                    ]
                state["pending"] = None
        windows = [
            _assess(documents, _read_pending(documents, reference, family), samples, now)
            for reference in state["windows"]
        ]
        starts = [datetime.fromisoformat(window["start"]) for window in windows]
        if any(
            second - first < timedelta(hours=ACCURACY_WINDOW_HOURS)
            for first, second in pairwise(starts)
        ):
            raise ValueError("Weather learning windows overlap")
    except (OSError, KeyError, TypeError, ValueError):
        # Corrupt evidence cannot activate learning or be overwritten as if valid.
        return simpler, fallback, {**evidence, "status": WeatherLearningStatus.EVIDENCE_UNREADABLE}
    if state["pending"] is None:
        start = now.replace(minute=0, second=0, microsecond=0)
        if start < now:
            start += timedelta(hours=1)
        profile = predict(configuration, samples, now, WeekdayProfile.fit(configuration, samples))
        model_document = json.loads(json.dumps(asdict(candidate), default=str))
        window = {
            "family": family,
            "start": start.isoformat(),
            "end": (start + timedelta(hours=ACCURACY_WINDOW_HOURS)).isoformat(),
            "issued_at": now.isoformat(),
            "training_version": candidate.training_version,
            "configuration_version": configuration.version,
            "model_digest": candidate.model_digest,
            "model_version": digest(model_document),
            "weather_version": forecast.weather_version,
            "predictions": {
                "candidate": _hourly(forecast, start),
                "simpler": _hourly(fallback, start),
                "profile": _hourly(profile, start),
            },
        }
        with documents.transaction():
            _immutable(
                documents,
                "consumer-weather-model-",
                model_document,
            )
            state["pending"] = _immutable(documents, "consumer-weather-window-", window)
            documents.write(pointer, state)
    else:
        documents.write(pointer, state)
    improved = len(windows) == REQUIRED_ACCURACY_WINDOWS and all(
        window["improved"] for window in windows
    )
    evidence.update(
        status=WeatherLearningStatus.VALIDATED
        if improved
        else WeatherLearningStatus.NO_BENEFIT
        if len(windows) == REQUIRED_ACCURACY_WINDOWS
        else WeatherLearningStatus.COLLECTING,
        completed_windows=len(windows),
        windows=windows,
        pending_until=_read_pending(documents, state["pending"], family)["end"],
        excluded_window=state["excluded_window"],
    )
    if improved:
        evidence["selected_model"] = candidate.model_id
        return candidate, forecast, evidence
    return simpler, fallback, evidence
