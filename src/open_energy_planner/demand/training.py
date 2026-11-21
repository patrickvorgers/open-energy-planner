"""Persist one checked daily demand fit independently of live forecast refreshes."""

import json
import logging
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from open_energy_planner.core.units import DAYS_PER_WEEK, HOURS_PER_DAY
from open_energy_planner.demand.calculation.forecasting import (
    RidgeDemandModel,
    WeekdayProfile,
    features,
)
from open_energy_planner.demand.calculation.model import digest, quantity
from open_energy_planner.prices.model import aware
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog

DAILY_TRAINING_POLICY = "completed-hours-daily-demand-v2"
MODEL_DOCUMENT_SCHEMA = 1
MAX_PROFILE_CELLS = DAYS_PER_WEEK * HOURS_PER_DAY
_LOGGER = logging.getLogger(__name__)
_TRAINING_LOGS = WeakKeyDictionary()


def _training_log(documents, binding, kind):
    """Keep independent learners' retry diagnostics for only their archive owner's lifetime."""
    logs = _TRAINING_LOGS.setdefault(documents, {}) if documents is not None else {}
    return logs.setdefault((binding, kind), BackgroundLog(_LOGGER, f"Daily demand {kind} training"))


def _parameters(model):
    """Detach scalar metadata and optional native JSON using the archive's ISO clock representation."""
    return json.loads(json.dumps(asdict(model), default=lambda value: value.isoformat()))


def _restore(kind, body, configuration, now):
    """Validate persisted scalar models before allowing them to produce a new prediction."""
    if not isinstance(body, dict) or body.get("timezone") != configuration.timezone:
        raise ValueError("Demand training configuration changed")
    if kind == "baseline":
        model = WeekdayProfile(**{**body, "cells": tuple(tuple(cell) for cell in body["cells"])})
        quantity(model.annual_kwh)
        if model.annual_kwh != configuration.annual_kwh or len(model.cells) > MAX_PROFILE_CELLS:
            raise ValueError("Invalid demand profile archive")
        identities = []
        for weekday, hour, energy in model.cells:
            if type(weekday) is not int or not 0 <= weekday < DAYS_PER_WEEK:
                raise ValueError("Invalid demand profile weekday")
            if type(hour) is not int or not 0 <= hour < HOURS_PER_DAY:
                raise ValueError("Invalid demand profile hour")
            quantity(energy)
            identities.append((weekday, hour))
        if (
            identities != sorted(set(identities))
            or model.cells
            and {hour for _, hour in identities} != set(range(HOURS_PER_DAY))
        ):
            raise ValueError("Invalid demand profile cells")
        if model.fallback_kwh is not None:
            quantity(model.fallback_kwh)
        if bool(model.cells) != (model.fallback_kwh is not None):
            raise ValueError("Incomplete demand profile archive")
        return model
    if kind == "ridge":
        if type(body.get("temperature_enabled")) is not bool:
            raise ValueError("Invalid demand regression feature policy")
        model = RidgeDemandModel(
            **{
                **body,
                "coefficients": tuple(body["coefficients"]),
                "means": tuple(body["means"]),
                "scales": tuple(body["scales"]),
                "trained_until": aware(datetime.fromisoformat(body["trained_until"])),
            }
        )
        expected = len(
            features(
                now, configuration.timezone, temperature=0 if model.temperature_enabled else None
            )
        )
        for values in (model.coefficients, model.means, model.scales):
            if len(values) != expected or any(
                type(value) not in (int, float) or not isfinite(value) for value in values
            ):
                raise ValueError("Invalid demand regression coefficients")
        if (
            any(value <= 0 for value in model.scales)
            or model.trained_until > now
            or model.trained_until.minute
            or model.trained_until.second
            or model.trained_until.microsecond
        ):
            raise ValueError("Invalid demand regression training boundary")
        quantity(model.recent_kwh)
        return model
    if kind == "weather":
        from open_energy_planner.appliances.weather_model import WeatherDemandModel

        model = WeatherDemandModel.restore(body, configuration.version)
        if model.trained_until > now:
            raise ValueError("Invalid weather model training boundary")
        return model
    raise ValueError("Unknown daily demand model")


def _read(documents, target, binding, configuration, now):
    """Restore the latest checked fits for the learner's current configuration."""
    try:
        state = documents.read(target)
    except FileNotFoundError:
        return None, {}
    if (
        not isinstance(state, dict)
        or set(state)
        != {"schema", "binding", "day", "input_version", "models", "checksum", "complete"}
        or type(state["schema"]) is not int
        or state["schema"] != MODEL_DOCUMENT_SCHEMA
        or type(state["complete"]) is not bool
        or digest({key: value for key, value in state.items() if key != "checksum"})
        != state["checksum"]
        or not isinstance(state["models"], dict)
        or "baseline" not in state["models"]
    ):
        raise ValueError("Invalid active demand training checksum or schema")
    if state["binding"] != binding:
        return None, {}
    if date.fromisoformat(state["day"]) > now.astimezone(ZoneInfo(configuration.timezone)).date():
        raise ValueError("Daily demand training is from the future")
    models = {}
    for kind, parameters in state["models"].items():
        try:
            models[kind] = _restore(kind, parameters, configuration, now)
        except (ImportError, OSError):
            if kind != "weather":
                raise
            _training_log(documents, binding, "weather restoration").exception(
                "Stored weather model loading is unavailable. Baseline and regression models remain usable. Retrying on the next forecast update.",
                expected=True,
            )
        else:
            if kind == "weather":
                restore_log = _training_log(documents, binding, "weather restoration")
                if restore_log.failure_key is not None:
                    restore_log.success("The stored weather model is available for forecasts.")
    return state, models


def daily_models(
    documents,
    configuration,
    samples,
    now,
    *,
    temperatures=(),
    weather_sensitive=False,
    include_weather=True,
    boundary=None,
    learner="household",
):
    """Reuse daily baseline and learned models while new weather still refreshes predictions.

    Persisted fits use only outcomes completed before the actual training time. Historical
    weather received before the actual fit remains admissible. A successful fit
    freezes its input cohort for the day. Missing-history outcomes can improve
    when imports arrive. A new day with unchanged eligible data reuses the exact
    existing coefficients. Successful fits replace the learner's latest row.
    Transient fitting failures leave that row and its usable models intact.
    """
    now = aware(now)
    local = now.astimezone(ZoneInfo(configuration.timezone))
    day = local.date().isoformat()
    cutoff = now.astimezone(UTC)
    training = (
        tuple(sample for sample in samples if sample.start + timedelta(hours=1) <= cutoff)
        if documents is not None
        else tuple(samples)
    )
    instants = {sample.start for sample in training}
    weather = (
        tuple(
            temperature
            for temperature in temperatures
            if temperature.start in instants and temperature.known_at <= now
        )
        if weather_sensitive
        else ()
    )
    use_weather_model = weather_sensitive and include_weather
    binding = digest(
        [
            DAILY_TRAINING_POLICY,
            configuration.version,
            boundary,
            weather_sensitive,
            use_weather_model,
        ]
    )
    target = "active-training-" + digest(learner) + ".json"
    state, previous = (
        _read(documents, target, binding, configuration, now)
        if documents is not None
        else (None, {})
    )
    input_version = digest(
        [
            [(sample.start, sample.residual_kwh) for sample in training],
            [(temperature.start, temperature.celsius) for temperature in weather],
        ]
    )
    if state is not None and (
        state["input_version"] == input_version or state["day"] == day and state["complete"]
    ):
        return previous
    models = {"baseline": WeekdayProfile.fit(configuration, training)}
    if not models["baseline"].residual_profile and previous.get("baseline") is not None:
        models["baseline"] = previous["baseline"]
    failed, complete = False, True
    ridge_log = _training_log(documents, binding, "ridge")
    try:
        models["ridge"] = RidgeDemandModel.fit(configuration, training, temperatures=weather)
        ridge_log.success("Updated coefficients are available for forecasts.")
    except ValueError:
        complete = False
        if "ridge" in previous:
            models["ridge"] = previous["ridge"]
    except (ImportError, OSError, RuntimeError):
        ridge_log.exception(
            "Retaining usable models. Retrying on the next forecast update.", expected=True
        )
        failed = True
    if use_weather_model:
        from open_energy_planner.appliances.weather_model import WeatherDemandModel

        weather_log = _training_log(documents, binding, "weather")
        try:
            models["weather"] = WeatherDemandModel.fit(
                configuration, training, temperatures=weather, known_at=now
            )
            weather_log.success("Updated coefficients are available for forecasts.")
        except ValueError:
            complete = False
            if "weather" in previous:
                models["weather"] = previous["weather"]
        except (ImportError, OSError, RuntimeError):
            weather_log.exception(
                "Retaining usable models. Retrying on the next forecast update.", expected=True
            )
            failed = True
    if failed:
        return {**previous, **models}
    if documents is not None:
        body = {
            "schema": MODEL_DOCUMENT_SCHEMA,
            "binding": binding,
            "day": day,
            "input_version": input_version,
            "models": {kind: _parameters(model) for kind, model in models.items()},
            "complete": complete,
        }
        documents.write(target, {**body, "checksum": digest(body)})
    return models
