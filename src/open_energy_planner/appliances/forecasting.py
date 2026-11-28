"""Forecast Energy-owned consumers separately, retaining the total-household safety baseline."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from open_energy_planner.appliances.sources import SOURCE_OWNERSHIP_UNAVAILABLE, validate_membership
from open_energy_planner.core.units import HOURS_PER_DAY
from open_energy_planner.demand.calculation.forecasting import (
    MIN_PROFILE_TRAINING_DAYS,
    RECENT_HISTORY_HOURS,
    predict,
)
from open_energy_planner.demand.calculation.model import (
    DemandConfiguration,
    DemandForecast,
    EnergyTerm,
    ExcludedLoad,
    ForecastStep,
    digest,
)
from open_energy_planner.demand.reconstruction import DemandReconstructor
from open_energy_planner.demand.training import daily_models
from open_energy_planner.demand.wire import forecast_document, forecast_window
from open_energy_planner.history.importer import HistoryImport
from open_energy_planner.history.storage import HistoryArchive
from open_energy_planner.home_assistant.names import source_name
from open_energy_planner.installation.forecast_policy import FORECAST_MAX_AGE_MINUTES

CONSUMER_SETTINGS_POINTER = "consumer-settings.json"
COMPOSITE_MODEL_ID = "consumer-composite-v1"
CONSUMER_ID_HEX_LENGTH = 16
COMPONENT_FORECAST_UNAVAILABLE = (
    "Some appliance forecasts are unavailable. Check the energy sensors and usable history."
)


def parse_selection(value):
    """Accept unique Energy meter identities and an explicit weather flag."""
    if not isinstance(value, list):
        raise TypeError("Expected individual consumers")
    selected = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"entity_id", "weather_sensitive"}
            or type(item["weather_sensitive"]) is not bool
        ):
            raise ValueError("Select an Energy consumer and its weather sensitivity")
        EnergyTerm(item["entity_id"], 1)
        selected.append(dict(item))
    if len({item["entity_id"] for item in selected}) != len(selected):
        raise ValueError("Duplicate individual consumer")
    return tuple(sorted(selected, key=lambda item: item["entity_id"]))


class ConsumerForecasts:
    """Own durable choices and transient evidence. No device control or HA clients."""

    def __init__(self, documents):
        """Restore checked choices without restoring stale forecasts as live predictions."""
        self.documents = documents
        self.selected = ()
        self.options = ()
        self.catalogue_known = False
        self.histories, self.failures = {}, {}
        self.result = None
        self.checked_at = None
        self.weather_reference, self.weather_valid_until = None, None
        self.issue = None
        try:
            if documents.exists(CONSUMER_SETTINGS_POINTER):
                self.selected = parse_selection(documents.read(CONSUMER_SETTINGS_POINTER))
        except (OSError, ValueError, TypeError, KeyError):
            self.issue = "consumer_configuration_unreadable"
        self.history_import = HistoryImport(HistoryArchive(documents))
        self.history_import.restore(item["entity_id"] for item in self.selected)
        self.histories = self._restore_histories()
        self.failures.update(self.history_import.issues)
        self.retry_after = {}

    def _restore_histories(self):
        """Restore complete accepted meters independently of unfinished yearly source checkpoints."""
        histories = {}
        for item in self.selected:
            entity = item["entity_id"]
            pointer = "consumer-history-cohort-" + digest(entity) + ".json"
            try:
                if self.documents.exists(pointer):
                    references = self.documents.read(pointer)
                    if set(references) != {entity}:
                        raise ValueError("Invalid consumer history cohort")
                    histories.update(self.history_import.restore_checkpoint(references))
            except (OSError, ValueError, TypeError, KeyError):
                self.failures[entity] = "history_cache_unreadable"
        return histories

    @property
    def version(self):
        """Version choices independently of their display names."""
        return consumer_version(self.selected, self.options, self.catalogue_known, self.issue)

    def sync(self, catalogue):
        """Use authoritative consumer membership, including upstream links and physical names."""
        previous = self.version
        metadata = {item["entity_id"]: item for item in catalogue["entities"]}
        self.options = tuple(
            {
                **item,
                "name": source_name(metadata.get(item["entity_id"], {}), "Appliance"),
            }
            for item in catalogue["suggestions"].get("consumers", [])
        )
        self.catalogue_known = True
        if self.version != previous:
            self.result = None

    def save(self, selected, expected_version):
        """Save checked choices through the same atomic selection boundary as typed appliances."""
        with self.saving_selection(selected, expected_version):
            pass

    @contextmanager
    def saving_selection(self, selected, expected_version, *related_documents):
        """Persist related appliance settings atomically and install selection only after successful commit."""
        if self.issue == "consumer_configuration_unreadable":
            raise ValueError(SOURCE_OWNERSHIP_UNAVAILABLE)
        if expected_version != self.version:
            raise ValueError("Consumer settings changed. Refresh this page")
        if not self.catalogue_known:
            raise ValueError("Select consumers configured in Home Assistant Energy")
        selected = parse_selection(list(selected))
        validate_membership(selected, self.options)
        changed = selected != self.selected
        with self.documents.transaction(*related_documents) as owns_commit:
            if related_documents and not owns_commit:
                raise ValueError("Related appliance settings must own their commit boundary")
            if changed:
                self.documents.write(
                    "consumer-settings-" + digest(selected) + ".json", list(selected)
                )
                self.documents.write(CONSUMER_SETTINGS_POINTER, list(selected))
            yield
        if not changed:
            return
        self.selected, self.issue, self.result = selected, None, None
        self.history_import.restore(item["entity_id"] for item in selected)
        self.failures = {
            item["entity_id"]: self.history_import.issues[item["entity_id"]]
            for item in selected
            if item["entity_id"] in self.history_import.issues
        }
        self.retry_after = {}
        self.histories = self._restore_histories()

    def request(self, configuration, now):
        """Resume all selected meter years independently, backing off failed transfers."""
        if configuration is None or self.issue or not self.catalogue_known:
            return None
        available = {item["entity_id"] for item in self.options}
        for item in self.selected:
            entity = item["entity_id"]
            if entity not in available or now < self.retry_after.get(entity, now):
                continue
            pending = self.history_import.request(
                entity, digest([configuration.version, self.version]), now
            )
            if pending is not None:
                return pending
        return None

    def receive(self, pending, report=None, error=None, *, now):
        """Install complete meter cohorts and retain accepted history during temporary failures."""
        entity = pending["entity_id"]
        if error:
            from open_energy_planner.demand.runtime import HISTORY_RETRY_MINUTES

            self.failures[entity] = error
            self.retry_after[entity] = now + timedelta(minutes=HISTORY_RETRY_MINUTES)
        else:
            complete = False
            with self.history_import.transaction():
                history = self.history_import.receive(pending, report, now)
                complete = (
                    history is not None
                    and self.history_import.request(entity, pending["configuration_version"], now)
                    is None
                )
                if complete:
                    self.documents.write(
                        "consumer-history-cohort-" + digest(entity) + ".json",
                        self.history_import.checkpoint((entity,)),
                    )
            if complete:
                self.histories[entity] = history
            self.failures.pop(entity, None)
            self.retry_after.pop(entity, None)

    def inputs(self):
        """Capture immutable worker inputs without sharing mutable runtime dictionaries."""
        return (
            self.version,
            tuple(dict(item) for item in self.selected),
            tuple(self.histories.values()),
            self.options,
        )

    def key(self):
        """Compare borrowed immutable histories without serializing a year of readings on every poll."""
        return self.version, tuple(sorted(self.histories.items()))

    def mark_checked(self, now, weather_reference, weather_valid_until):
        """Attach current validity to the installed component issue without changing the archive."""
        self.checked_at = now
        self.weather_reference = weather_reference
        self.weather_valid_until = weather_valid_until

    def read_view(self, *, forecasts=True):
        """Capture accepted inputs for projection without borrowing the mutable service."""
        return ConsumerView(
            self.selected,
            self.options,
            self.catalogue_known,
            self.issue,
            dict(self.failures),
            self.result if forecasts else None,
            self.checked_at,
            self.weather_reference,
            self.weather_valid_until,
        )

    def document(self, *, forecasts=True, now=None):
        """Expose current choices and component issues through the shared read projection."""
        return self.read_view(forecasts=forecasts).document(forecasts=forecasts, now=now)


def consumer_version(selected, options, catalogue_known, issue):
    """Bind settings to choices and Energy source membership independently of display names."""
    return digest(
        [
            selected,
            [{key: value for key, value in row.items() if key != "name"} for row in options],
            catalogue_known,
            issue,
        ]
    )


@dataclass(frozen=True, eq=False)
class ConsumerView:
    """Copy-on-write source choices and forecasts, with a private history-diagnostic copy."""

    selected: tuple
    options: tuple
    catalogue_known: bool
    issue: str | None
    failures: dict
    result: dict | None
    checked_at: datetime | None
    weather_reference: str | None
    weather_valid_until: datetime | None

    def _live_document(self, forecast, now, *, for_ha=False):
        """Project a native issue onto the transport grid with the app-owned validity deadline."""
        issued_at = datetime.fromisoformat(forecast["issued_at"])
        valid_until = (self.checked_at or issued_at) + timedelta(minutes=FORECAST_MAX_AGE_MINUTES)
        if self.weather_valid_until and forecast.get("weather_version") == self.weather_reference:
            valid_until = min(valid_until, self.weather_valid_until)
        steps = tuple(
            ForecastStep(datetime.fromisoformat(step["start"]), step["duration_h"], step["kwh"])
            for step in forecast["steps"]
        )
        return {
            **(
                {key: forecast[key] for key in ("model", "unit", "issued_at")}
                if for_ha
                else forecast
            ),
            "valid_until": valid_until.isoformat(),
            "steps": [
                {"start": step.start.isoformat(), "duration_h": step.duration_h, "kwh": step.kwh}
                for step in forecast_window(steps, now)
            ],
        }

    def ha_projection(self, now):
        """Project only membership and active forecasts consumed by HA virtual devices."""
        result = self.result
        return {
            "options": [
                {key: option[key] for key in ("entity_id", "name", "power_entity") if key in option}
                for option in self.options
            ],
            "selected": [
                {"entity_id": item["entity_id"], "weather_sensitive": item["weather_sensitive"]}
                for item in self.selected
            ],
            "result": {
                "status": result["status"],
                "devices": [
                    {
                        "entity_id": device["entity_id"],
                        "status": device.get("status", result["status"]),
                        "forecast": self._live_document(device["forecast"], now, for_ha=True),
                    }
                    for device in result.get("devices", ())
                ],
            }
            if result is not None
            else None,
        }

    def document(self, *, forecasts=True, now=None):
        """Expose current choices and project live component issues when a request time is supplied."""
        document = {
            "version": consumer_version(
                self.selected, self.options, self.catalogue_known, self.issue
            ),
            "selected": list(self.selected),
            "options": list(self.options),
            "issue": self.issue,
            "history_issues": dict(self.failures),
        }
        if forecasts:
            document["result"] = dict(self.result) if self.result is not None else None
        if forecasts and now is not None and (result := document["result"]) is not None:
            if "devices" in result:
                result["devices"] = [
                    {
                        **device,
                        **{
                            key: self._live_document(device[key], now)
                            for key in ("forecast", "baseline_forecast")
                            if key in device
                        },
                    }
                    for device in result["devices"]
                ]
            for key in ("residual", "residual_baseline"):
                if result.get(key):
                    result[key] = self._live_document(result[key], now)
        return document


def build_components(
    configuration,
    household_histories,
    temperatures,
    now,
    inputs,
    *,
    documents=None,
    controlled_days=None,
    grid=None,
    weather_version=None,
    replay_windows=(),
    calendar_windows=(),
    replay_jobs=None,
    replay_evidence=None,
):
    """Build household demand from its remainder and ready disjoint consumers exactly once.

    Each component uses its baseline until original local-day accuracy admits a
    learned alternative. Missing components stay inside the measured remainder.
    Complete residual evidence remains mandatory before changing the boundary.

    Returns:
        A complete or explicitly partial composition and its status document,
        or two None values when no consumers are selected. Individual failures
        do not discard independent ready components. Residual failure retains
        ready records without authorizing household replacement. The preparation
        chain requires coverage for every controlled component.
    """
    settings_version, selected, histories, options = inputs[:4]
    if not selected:
        return None, None
    if issue := _component_admission_issue(configuration, selected, options):
        return None, {"status": issue, "devices": []}

    controlled_days = controlled_days or {}
    control_history_unavailable = any("*" in days for days in controlled_days.values())
    forecast_component = partial(
        component,
        temperatures=temperatures,
        now=now,
        documents=documents,
        grid=grid,
        weather_version=weather_version,
    )

    def forecast_selected(single, samples, item):
        """Apply the selected meter's replay and control-history policy."""
        weather_sensitive = item["weather_sensitive"]
        return forecast_component(
            single,
            samples,
            weather_sensitive=weather_sensitive,
            learner=item["entity_id"],
            replay_windows=replay_windows if weather_sensitive else calendar_windows,
            replay_jobs=replay_jobs,
            replay_evidence=replay_evidence,
            excluded_dates=("*",)
            if control_history_unavailable
            else controlled_days.get(item["entity_id"], ()),
        )

    records, forecasts = _separate_ready_consumers(
        configuration.timezone, selected, histories, now, forecast_selected
    )
    separated = [item["entity_id"] for item in records]
    configuration = replace(
        configuration,
        exclusions=tuple(
            ExcludedLoad(digest(entity)[:CONSUMER_ID_HEX_LENGTH], entity) for entity in separated
        ),
    )
    try:
        residual = DemandReconstructor().reconstruct(
            configuration, (*household_histories, *histories), before=now
        )
        residual_configuration = replace(configuration, exclusions=())
        _, remaining, residual_selection, baseline_forecast = forecast_component(
            residual_configuration,
            residual,
            weather_sensitive=True,
            boundary=configuration.version,
            learner="residual",
            excluded_dates=("*",)
            if control_history_unavailable
            else _residual_controlled_dates(controlled_days, separated),
        )
        baseline = forecast_document(baseline_forecast)
    except ValueError:
        return None, _unavailable_composition(records)
    return _combine_ready_consumers(
        residual_configuration,
        now,
        weather_version,
        forecasts,
        remaining,
        {
            "devices": records,
            "residual_baseline": baseline,
            "residual_selection": residual_selection,
            "configuration_version": settings_version,
        },
        has_unavailable_components=len(records) != len(selected),
    )


def _component_admission_issue(configuration, selected, options):
    """Check availability and disjoint ownership in the original admission order."""
    available = {item["entity_id"] for item in options}
    selected_entities = {item["entity_id"] for item in selected}
    if not selected_entities <= available:
        return "energy_consumer_removed"
    if configuration.exclusions:
        return "existing_exclusions_unsupported"
    if not configuration.terms:
        return "household_boundary_missing"
    if selected_entities & set(configuration.entities):
        return "consumer_overlaps_household_boundary"
    try:
        validate_membership(selected, options)
    except ValueError:
        return "energy_consumer_hierarchy_inconsistent"
    return None


def _separate_ready_consumers(timezone, selected, histories, now, forecast_selected):
    """Forecast each ready meter and produce matching records in selection order."""
    records, forecasts = [], []
    for item in selected:
        try:
            single = DemandConfiguration(timezone, 0, (EnergyTerm(item["entity_id"], 1),))
            samples = DemandReconstructor().reconstruct(single, histories, before=now)
            model, forecast, selection, baseline_forecast = forecast_selected(single, samples, item)
        except ValueError:
            continue
        forecasts.append(forecast)
        records.append(
            {
                **item,
                "status": selection["status"],
                "model_selection": selection,
                "weather_features_used": bool(getattr(model, "temperature_enabled", False)),
                "forecast": forecast_document(forecast),
                "baseline_forecast": forecast_document(baseline_forecast),
            }
        )
    return records, forecasts


def _residual_controlled_dates(controlled_days, separated):
    """Exclude controlled dates for meters still represented in the residual."""
    separated_entities = set(separated)
    return sorted(
        {
            day
            for entity, days in controlled_days.items()
            if entity not in separated_entities
            for day in days
        }
    )


def _unavailable_composition(records):
    """Retain ready devices without emitting a replacement household forecast."""
    return {
        "status": "history_or_weather_not_ready",
        "detail": COMPONENT_FORECAST_UNAVAILABLE,
        "devices": records,
    }


def _combine_ready_consumers(
    configuration,
    now,
    weather_version,
    forecasts,
    remaining,
    evidence,
    *,
    has_unavailable_components,
):
    """Restore the household total and attach evidence for the same component boundary."""
    records = evidence["devices"]

    def residual_document():
        """Serialize the residual alongside the evidence shared by both outcomes."""
        return {"devices": records, "residual": forecast_document(remaining), **evidence}

    if has_unavailable_components and not records:
        return None, {**_unavailable_composition(records), **residual_document()}
    try:
        total = tuple(
            ForecastStep(
                step.start,
                step.duration_h,
                step.kwh + sum(part.steps[index].kwh for part in forecasts),
            )
            for index, step in enumerate(remaining.steps)
        )
        forecast = DemandForecast(
            # Keep the public household boundary unchanged. Selection family and
            # training identity additionally retain the component configuration.
            configuration.version,
            digest(
                [
                    evidence["configuration_version"],
                    remaining.training_version,
                    [part.version for part in forecasts],
                ]
            ),
            now,
            COMPOSITE_MODEL_ID,
            total,
            (),
            next(
                (
                    part.weather_version
                    for part in (remaining, *forecasts)
                    if part.weather_version == weather_version
                ),
                remaining.weather_version,
            ),
            quality="estimated",
        )
        return forecast, {
            "status": "partial_history" if has_unavailable_components else "active",
            "detail": COMPONENT_FORECAST_UNAVAILABLE if has_unavailable_components else None,
            "separated_entities": [record["entity_id"] for record in records],
            **residual_document(),
        }
    except ValueError:
        return None, _unavailable_composition(records)


def component_history_ready(configuration, samples):
    """Require current measured coverage before authorizing component separation or daily selection."""
    return len(samples) >= MIN_PROFILE_TRAINING_DAYS * HOURS_PER_DAY and {
        sample.start.astimezone(ZoneInfo(configuration.timezone)).hour for sample in samples
    } == set(range(HOURS_PER_DAY))


def _recent_component_kwh(samples, now):
    """Read the contiguous completed feature window in mean kWh per hour."""
    # A new measured hour updates a feature without changing the saved fit.
    measured = samples[-RECENT_HISTORY_HOURS:]
    if (
        len(measured) == RECENT_HISTORY_HOURS
        and measured[-1].start - measured[0].start == timedelta(hours=RECENT_HISTORY_HOURS - 1)
        and measured[-1].start + timedelta(hours=1) <= now
    ):
        return sum(sample.residual_kwh for sample in measured) / RECENT_HISTORY_HOURS
    return None


def _predict_component_candidates(predict_model, fitted, weather, samples, now, *, refresh_recent):
    """Predict the required baseline and retain each independently usable alternative."""
    baseline = fitted["baseline"]
    if not baseline.residual_profile:
        raise ValueError("At least seven complete days covering every hour are required")
    models = {"baseline": baseline}
    forecasts = {"baseline": predict_model(baseline)}
    recent = _recent_component_kwh(samples, now) if refresh_recent else None
    for kind in ("ridge", "weather"):
        if kind not in fitted:
            continue
        candidate = fitted[kind]
        try:
            prediction = predict_model(candidate, temperatures=weather, recent=recent)
        except (ValueError, ImportError, OSError):
            continue
        models[kind], forecasts[kind] = candidate, prediction
    return models, forecasts


def _queue_component_replay(replay_jobs, family, learner, replay, excluded_dates, selection):
    """Keep original live days out of the next background replay job."""
    original_days = {
        window["day"] for window in selection["windows"] if window.get("source") == "live"
    }
    replay_jobs.append((family, learner, partial(replay, (*excluded_dates, *original_days))))


def _component_selection_identity(configuration, boundary, models):
    """Keep comparison evidence tied to the measured boundary and fitted feature sets."""
    return [
        configuration.version,
        boundary,
        [
            (
                name,
                model.model_id,
                getattr(model, "temperature_enabled", False),
                getattr(model, "feature_names", ()),
            )
            for name, model in models.items()
        ],
    ]


def component(
    configuration,
    samples,
    temperatures,
    now,
    weather_sensitive,
    *,
    documents=None,
    include_weather=True,
    boundary=None,
    learner="component",
    excluded_dates=(),
    grid=None,
    weather_version=None,
    replay_windows=None,
    replay_jobs=None,
    replay_evidence=None,
):
    """Select suitable models by preserved daily accuracy, retaining a measured weekday baseline.

    Args:
        configuration: Demand configuration with the HA timezone and measurement boundary.
        samples: Completed hourly samples containing nonnegative residual AC kWh.
        temperatures: Available temperature evidence for optional weather features.
        now: Aware prediction time and knowledge cutoff.
        weather_sensitive: Whether temperature features and a weather candidate apply.
        documents: Optional demand archive used for original daily comparisons.
        include_weather: Whether weather sensitivity also fits a separate weather candidate.
        boundary: Version of the separated measurement boundary.
        learner: Stable household or consumer identity for the saved fit.
        excluded_dates: Local ISO dates of controlled use, or '*' to exclude all days.

    Returns:
        Four values: the selected fitted model, its forecast in AC kWh, daily
        selection evidence and the baseline forecast. Unusable candidate fits are omitted
        while the weekday baseline remains available.

    Raises:
        ValueError: The weekday baseline lacks seven complete days covering every hour,
            or its input/prediction contract is invalid.

    Side Effects:
        With an archive, choose_daily refreshes the pending next-day capture and
        freezes original predictions after midnight. Controlled local dates cannot
        provide promotion evidence. Unreadable comparison evidence keeps the baseline.
    """
    from open_energy_planner.forecast_selection.accuracy import choose_daily
    from open_energy_planner.forecast_selection.device_history import replay_family, replay_scores

    if not component_history_ready(configuration, samples):
        raise ValueError("At least seven complete days covering every hour are required")
    fitted = daily_models(
        documents,
        configuration,
        samples,
        now,
        temperatures=temperatures,
        weather_sensitive=weather_sensitive,
        include_weather=include_weather,
        boundary=boundary,
        learner=learner,
    )
    weather = temperatures if weather_sensitive else ()
    predict_model = partial(
        predict, configuration, samples, now, grid=grid, weather_version=weather_version
    )
    models, forecasts = _predict_component_candidates(
        predict_model, fitted, weather, samples, now, refresh_recent=replay_windows is not None
    )
    replays = None
    if replay_windows is not None:
        family = replay_family(configuration, models)
        replays = (replay_evidence or {}).get(family, ())
    selection = choose_daily(
        documents,
        _component_selection_identity(configuration, boundary, models),
        configuration.timezone,
        {name: forecast_document(forecast) for name, forecast in forecasts.items()},
        {sample.start: sample.residual_kwh for sample in samples},
        now,
        excluded_dates=excluded_dates,
        replays=replays,
    )
    if replay_jobs is not None and documents is not None and replay_windows and len(models) > 1:
        _queue_component_replay(
            replay_jobs,
            family,
            learner,
            partial(
                replay_scores, documents, configuration, samples, weather, replay_windows, models
            ),
            excluded_dates,
            selection,
        )
    chosen = selection["selected"]
    selection["selected_model"] = models[chosen].model_id
    if "weather" in models:
        weather_model = models["weather"]
        selection["training_quality"] = {
            "source_hours": weather_model.source_hours,
            "used_hours": weather_model.training_hours,
            "excluded_periods": [
                {"start": start, "end": end, "reason": reason}
                for start, end, reason in weather_model.excluded_periods
            ],
        }
    return models[chosen], forecasts[chosen], selection, forecasts["baseline"]
