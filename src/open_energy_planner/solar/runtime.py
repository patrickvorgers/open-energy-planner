"""Versioned solar orchestration. Background workers own model calculation and immutable writes."""

import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from importlib.metadata import version as distribution_version

from open_energy_planner.forecast_quality.policy import FORECAST_MODELS
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog, failure_trace
from open_energy_planner.solar.calculation.model import ForecastSource, SolarForecast, version
from open_energy_planner.solar.forecast_weather import (
    FORECAST_RUN_RESPONSE_HOURS,
    MAX_LIVE_RUN_AGE_HOURS,
    ForecastWeatherRun,
)

from ..planning_policy import FINE_INTERVAL_MINUTES, PLANNING_HORIZON_HOURS
from .boosting import TrainingResult, TrainingStatus, TrainingSummary, fit_boosted_model
from .calculation.projection import learned_forecast
from .ha_forecasts import HA_REFRESH_MAX_AGE
from .learning import learning_blocker
from .model_policy import DEFAULT_GROUND_ALBEDO, DEFAULT_TEMPERATURE_COEFFICIENT_MAGNITUDE
from .source_selection import MIN_ACCURACY_DAYS, SOURCE_POLICY, choose_sources, ha_energies
from .storage.history import HistoryContinuityError, SolarHistoryImport
from .wire import configuration_document, document, forecast_document

HISTORY_RETRY_MINUTES = 15
HARD_HISTORY_ISSUES = frozenset({"history_continuity_unavailable", "history_cache_unreadable"})
EAST_WEST_ARRAY_SHARE = 0.5
MODEL_RETRY_MINUTES = 5
MIN_HISTORY_MODEL_UPDATE_SECONDS = 60
_LOGGER = logging.getLogger(__name__)


class SolarRuntime:
    """Cache independent per-source history and physical forecasts without provider access."""

    def __init__(self, store):
        """Restore settings while requiring new weather/location before publishing live forecasts."""
        self.store = store
        self.configuration, self.issue = store.current, store.issue
        self.location, self.weather = None, ()
        self.forecast_runs, self.weather_service = (), {}
        self.forecast_runs_revision, self.last_fit_at = 0, None
        self.forecast_cohort_revision = 0
        self.weather_revision, self.history_revision = 0, 0
        self.histories, self.ranges, self.history_issues = {}, {}, {}
        self.history_retry_after = {}
        self.history_import = SolarHistoryImport(store)
        self.history_import.configure(self.configuration.sources if self.configuration else ())
        self.histories = (
            self.history_import.restore(source.entity_id for source in self.configuration.sources)
            if self.configuration
            else {}
        )
        self.history_issues.update(self.history_import.issues)
        self.forecast, self.candidate, self.calibrations = None, None, ()
        self.baseline = None
        self.last_key, self.failed_key, self.retry_after = None, None, None
        self.ha_predictions, self.source_selection, self.ha_issues = {}, {}, {}
        self.ha_revision = 0
        self.learning = {}
        self.learning_log = BackgroundLog(_LOGGER, "Solar ML candidate update")

    def configure(self, configuration):
        """Reset incompatible live state only after a changed definition has been persisted."""
        self.configuration, self.issue = configuration, None
        self.history_revision += 1
        self.ranges, self.history_issues = {}, {}
        self.history_import.configure(configuration.sources)
        self.histories = self.history_import.restore(
            source.entity_id for source in configuration.sources
        )
        self.history_issues.update(self.history_import.issues)
        self.history_retry_after = {}
        self.forecast, self.candidate, self.calibrations, self.last_key = None, None, (), None
        self.baseline = None
        self.failed_key, self.retry_after = None, None
        self.source_selection = {}
        self.learning = {}

    def save_ha(self, predictions):
        """Retain original cache observations and each real refresh without overwriting earlier receipts."""
        with self.store.transaction():
            for entity, providers in predictions.items():
                for prediction in providers:
                    first = "ha-first-" + prediction.version + ".json"
                    if not self.store.documents.exists(first):
                        self.store.documents.write(first, document(prediction))
                    receipt = {"entity_id": entity, "prediction": document(prediction)}
                    self.store.archive("ha-receipt", version(receipt), receipt)

    def receive_ha(self, predictions):
        """Install only persisted normalized observations, keeping provider refresh distinct from issuance."""
        self.ha_predictions = predictions
        self.ha_revision += 1

    def set_weather(self, location, weather, *, service=None):
        """Install typed source evidence. Changing the site revokes old-site weather and outputs."""
        if self.location != location:
            self.forecast, self.candidate, self.baseline, self.last_key = None, None, None, None
            self.learning = {}
            self.forecast_runs = ()
            self.forecast_runs_revision += 1
        self.location, self.weather = location, tuple(weather)
        self.weather_service = dict(service) if service is not None else {}
        self.weather_revision += 1

    def set_forecast_runs(self, location, runs):
        """Coalesce added runs but revoke results when captured evidence is replaced or withdrawn."""
        if location != self.location:
            return False
        selected = tuple(runs)
        site = version(asdict(location))
        if any(
            not isinstance(run, ForecastWeatherRun)
            or run.location_version != site
            or len(run.hours) != FORECAST_RUN_RESPONSE_HOURS - 1
            for run in selected
        ) or len({run.initialized_at for run in selected}) != len(selected):
            raise ValueError("Invalid original solar forecast run cohort")
        selected = tuple(replace(run, forecast_origin=None) for run in selected)
        if selected == self.forecast_runs:
            return False
        incoming = {run.initialized_at: run.version for run in selected}
        if any(incoming.get(run.initialized_at) != run.version for run in self.forecast_runs):
            self.forecast_cohort_revision += 1
            self.last_key = None
            self.forecast, self.candidate, self.calibrations = self.baseline, None, ()
            self.source_selection, self.learning = {}, {}
        self.forecast_runs = selected
        self.forecast_runs_revision += 1
        return True

    def request(self, now):
        """Discover all available years and resume one bounded source transfer at a time."""
        if self.configuration is None or self.store.issue:
            return None
        for source in self.configuration.sources:
            if now < self.history_retry_after.get(source.entity_id, now):
                continue
            pending = self.history_import.request(source.entity_id, self.configuration.version, now)
            if pending is not None:
                return pending
        return None

    def receive(self, ref, history, now, *, error=None, report=None):
        """Persist a source-bound annual transfer before installing a multi-year aggregate.

        Delayed replies cannot cross configuration changes. Recorder failures
        advance to other sources until their bounded retry. Identity or overlap
        changes retain the prior accepted cache and the original incoming evidence.
        """
        pending = self.request(now)
        if pending is None or pending["version"] != ref:
            raise ValueError("Solar history request changed")
        entity = pending["entity_id"]
        if not error:
            try:
                accepted = self.history_import.receive(pending, report, now)
            except HistoryContinuityError:
                error = "history_continuity_unavailable"
            else:
                if accepted:
                    self.histories[entity] = accepted
                self.history_revision += 1
                self.history_issues.pop(entity, None)
                self.history_retry_after.pop(entity, None)
        if error:
            previous = self.history_issues.get(entity)
            if previous in HARD_HISTORY_ISSUES and error not in HARD_HISTORY_ISSUES:
                error = previous
            if error in HARD_HISTORY_ISSUES and previous != error:
                self.history_revision += 1
                self.last_key = None
                self.forecast, self.candidate, self.calibrations = self.baseline, None, ()
                self.source_selection, self.learning = {}, {}
            self.history_issues[entity] = error
            self.history_retry_after[entity] = now + timedelta(minutes=HISTORY_RETRY_MINUTES)
        self.ranges[entity] = pending["start"], pending["end"]

    def key(self, now):
        """Coalesce input/version changes and quarter boundaries without fitting on every HA poll."""
        return (
            (
                self.configuration.version,
                now.replace(
                    minute=now.minute // FINE_INTERVAL_MINUTES * FINE_INTERVAL_MINUTES,
                    second=0,
                    microsecond=0,
                ),
                self.location,
                self.weather_revision,
                self.history_revision,
                self.ha_revision,
                tuple(
                    (entity, tuple(row.fresh(now) for row in providers))
                    for entity, providers in sorted(self.ha_predictions.items())
                ),
                self.forecast_cohort_revision,
                self.forecast_runs_revision,
            )
            if self.configuration
            else None
        )

    def job(self, now):
        """Capture an immutable worker job. Unchanged failures back off for five minutes."""
        if self.configuration is None or self.store.issue or not self.configuration.sources:
            return None
        ha_only = all(
            source.forecast_source == ForecastSource.HOME_ASSISTANT
            for source in self.configuration.sources
        )
        if self.location is None or not self.weather and not ha_only:
            self.issue = "solar_weather_unavailable"
            return None
        if self.weather_service:
            try:
                initialized = datetime.fromisoformat(self.weather_service["initialized_at"])
                available = datetime.fromisoformat(self.weather_service["available_at"])
                expired = not initialized <= available <= now or now - initialized > timedelta(
                    hours=MAX_LIVE_RUN_AGE_HOURS
                )
                origin = now.replace(minute=0, second=0, microsecond=0)
                present = {row.start for row in self.weather}
                expired = expired or any(
                    origin + timedelta(hours=index) not in present
                    for index in range(PLANNING_HORIZON_HOURS + 1)
                )
            except (TypeError, ValueError, KeyError):
                expired = True
        else:
            expired = bool(self.weather and now - self.weather[-1].received_at > timedelta(hours=2))
        if expired and not ha_only:
            self.issue = "solar_weather_expired"
            return None
        key = self.key(now)
        if (
            self.last_key
            and key[:-1] == self.last_key[:-1]
            and self.last_fit_at
            and now - self.last_fit_at < timedelta(seconds=MIN_HISTORY_MODEL_UPDATE_SECONDS)
        ):
            return None
        if key == self.last_key or (
            key == self.failed_key and self.retry_after and now < self.retry_after
        ):
            return None
        return (
            key,
            self.configuration,
            self.location,
            () if expired else self.weather,
            tuple(
                (entity, history)
                for entity, history in self.histories.items()
                if self.history_issues.get(entity) not in HARD_HISTORY_ISSUES
            ),
            now,
            self.ha_predictions,
            self.forecast_runs,
            dict(self.weather_service),
        )

    def build(self, job):
        """Compute and archive exact physical output plus shadow corrections outside the event loop."""
        from open_energy_planner.solar.calculation.predictor import PVWattsPredictor

        (
            key,
            configuration,
            location,
            weather,
            histories,
            now,
            ha_predictions,
            forecast_runs,
            weather_service,
        ) = job
        if not weather:
            energies, issues = ha_energies(configuration, None, ha_predictions, now)
            if issues or len(energies) != len(configuration.sources):
                raise ValueError("HA solar forecast unavailable without weather fallback")
            forecast = SolarForecast(
                configuration.version,
                location,
                now,
                "none",
                tuple(row for rows in energies.values() for row in rows),
                model="ha-production-v1",
                quality="provider_estimate",
                valid_until=min(
                    prediction.provider_refreshed_at + HA_REFRESH_MAX_AGE
                    for source in configuration.sources
                    for prediction in ha_predictions[source.entity_id]
                ),
            )
            statuses = {
                source.source_id: {
                    "requested": source.forecast_source,
                    "active": ForecastSource.HOME_ASSISTANT,
                    "model": forecast.model,
                    "mae_kwh": {},
                    "complete_days": 0,
                    "required_days": MIN_ACCURACY_DAYS,
                    "policy": SOURCE_POLICY,
                    "issue": None,
                }
                for source in configuration.sources
            }
            self.store.archive(
                "forecast",
                forecast.version,
                {
                    "forecast": forecast_document(forecast),
                    "source_selection": statuses,
                    "ha_references": {
                        entity: [row.version for row in providers]
                        for entity, providers in ha_predictions.items()
                    },
                },
            )
            self.store.archive(
                "quality-source-models",
                forecast.version,
                {source.source_id: forecast.model for source in configuration.sources},
            )
            self.store.archive(
                "source-choice",
                forecast.version,
                {
                    "sources": statuses,
                    "ha_references": {
                        entity: [row.version for row in providers]
                        for entity, providers in ha_predictions.items()
                    },
                },
            )
            return key, forecast, (), None, None, statuses, {}, {}
        predictor = PVWattsPredictor()
        forecast = predictor.forecast(configuration, location, weather, now)
        weather_provenance = {
            "weather_service": weather_service,
            "forecast_weather_source": {
                "model": weather_service["model"],
                "initialized_at": weather_service["initialized_at"],
                "location_version": version(asdict(location)),
                "weather_version": forecast.weather_version,
                "policy": weather_service["policy"],
            }
            if weather_service
            else None,
        }
        hourly = predictor.hourly(
            configuration,
            location,
            tuple(weather_item for weather_item in weather if weather_item.received_at <= now),
        )
        histories = dict(histories)
        self.store.archive_series(
            "weather", forecast.weather_version, [document(row) for row in weather]
        )
        for history in histories.values():
            body = document(history)
            self.store.archive_series(
                "history", version(body), body["hours"], entity=history.entity_id
            )
        learning = self._learned_candidates(
            configuration, location, histories, hourly, weather, now, forecast_runs
        )
        models_by_source = {
            source_id: result.model
            for source_id, result in learning.items()
            if result.model is not None
        }
        try:
            candidate = learned_forecast(configuration, location, weather, now, models_by_source)
        except (ImportError, ValueError, RuntimeError, OSError) as error:
            self.learning_log.exception(
                "Independent solar prediction is unavailable. PVWatts remains available. Retrying with the next usable solar forecast update.",
                expected=True,
                reason=type(error).__name__,
            )
            _LOGGER.debug(
                "Independent solar prediction failure (%s).\n%s",
                type(error).__name__,
                failure_trace(error),
            )
            learning = {
                source_id: replace(
                    result,
                    model=None,
                    summary=replace(
                        result.summary,
                        status=TrainingStatus.FAILED,
                        issue="The independent prediction could not be updated. PVWatts remains usable.",
                    ),
                )
                if result.model is not None
                else result
                for source_id, result in learning.items()
            }
            models_by_source, candidate = {}, None
        calibrations = tuple(models_by_source.values())
        learned_names = {source_id: model.schema for source_id, model in models_by_source.items()}
        learned_references = {
            source_id: learning[source_id].archive_reference for source_id in models_by_source
        }
        if candidate:
            self.store.archive(
                "forecast",
                candidate.version,
                {"forecast": forecast_document(candidate), **weather_provenance},
            )
        self.store.archive(
            "forecast",
            forecast.version,
            {
                "forecast": forecast_document(forecast),
                **weather_provenance,
                "assumptions": {
                    "pvlib": distribution_version("pvlib"),
                    "gamma_pdc": -DEFAULT_TEMPERATURE_COEFFICIENT_MAGNITUDE,
                    "temperature": "faiman",
                    "sky": "isotropic",
                    "albedo": DEFAULT_GROUND_ALBEDO,
                    "east_west_split": [EAST_WEST_ARRAY_SHARE, EAST_WEST_ARRAY_SHARE],
                },
            },
        )
        baseline = forecast
        ha, ha_issues = ha_energies(configuration, baseline, ha_predictions, now)
        choices, selection_status = choose_sources(
            self.store,
            configuration,
            baseline,
            candidate,
            calibrations,
            ha,
            histories,
            now,
            ha_references={
                entity: [row.version for row in providers]
                for entity, providers in ha_predictions.items()
            },
            ha_configuration_versions={
                entity: [row.configuration_revision for row in providers]
                for entity, providers in ha_predictions.items()
            },
            ml_policies={
                source_id: row.model.policy_version
                for source_id, row in learning.items()
                if row.model is not None
            },
            historical_assessments={
                source_id: row.summary.historical_assessment for source_id, row in learning.items()
            },
        )
        selected = tuple(
            row for row in calibrations if choices[row.source_id][0] == ForecastSource.ML
        )
        names = {
            ForecastSource.PVWATTS: baseline.model,
            ForecastSource.HOME_ASSISTANT: "ha-production-v1",
        }
        models = {
            source_id: learned_names[source_id]
            if choice[0] == ForecastSource.ML
            else names[choice[0]]
            for source_id, choice in choices.items()
        }
        for source_id, status in selection_status.items():
            status["model"] = models[source_id]
        if any(choice[0] != ForecastSource.PVWATTS for choice in choices.values()):
            forecast = replace(
                baseline,
                energies=tuple(row for choice in choices.values() for row in choice[1]),
                model="solar-selected-v1",
                quality="selected",
                calibration_versions=tuple(learned_references[row.source_id] for row in selected),
                valid_until=min(
                    (
                        row.provider_refreshed_at + HA_REFRESH_MAX_AGE
                        for source in configuration.sources
                        if choices[source.source_id][0] == ForecastSource.HOME_ASSISTANT
                        for row in ha_predictions[source.entity_id]
                    ),
                    default=None,
                ),
            )
            self.store.archive(
                "forecast",
                forecast.version,
                {
                    "forecast": forecast_document(forecast),
                    **weather_provenance,
                    "source_selection": selection_status,
                    "ha_references": {
                        entity: [prediction.version for prediction in providers]
                        for entity, providers in ha_predictions.items()
                    },
                },
            )
        self.store.archive(
            "source-choice",
            forecast.version,
            {
                "sources": selection_status,
                "ha_references": {
                    entity: [row.version for row in providers]
                    for entity, providers in ha_predictions.items()
                },
            },
        )
        for archived, applied in (
            (baseline, ()),
            (candidate, calibrations),
            (forecast, selected),
        ):
            if archived is not None:
                calibrated = {row.source_id for row in applied}
                available_sources = {row.source_id for row in archived.energies}
                self.store.archive(
                    "quality-source-models",
                    archived.version,
                    {
                        source.source_id: (
                            models[source.source_id]
                            if archived is forecast
                            else learned_names[source.source_id]
                            if source.source_id in calibrated
                            else baseline.model
                        )
                        for source in configuration.sources
                        if source.source_id in available_sources
                    },
                )
        alternatives = [baseline]
        if candidate is not None:
            alternatives.append(candidate)
        if ha:
            ha_forecast = replace(
                baseline,
                energies=tuple(row for rows in ha.values() for row in rows),
                model="ha-production-v1",
                quality="provider_estimate",
            )
            self.store.archive(
                "forecast",
                ha_forecast.version,
                {
                    "forecast": forecast_document(ha_forecast),
                    "ha_references": {
                        entity: [row.version for row in providers]
                        for entity, providers in ha_predictions.items()
                    },
                },
            )
            alternatives.append(ha_forecast)
        self.store.archive(
            "quality-candidates",
            forecast.version,
            {"forecasts": [row.version for row in alternatives], **weather_provenance},
        )
        return (
            key,
            forecast,
            calibrations,
            candidate,
            baseline,
            selection_status,
            ha_issues,
            learning,
        )

    def _learned_candidates(
        self, configuration, location, histories, hourly, weather, now, forecast_runs=()
    ):
        """Persist bounded tree weights once and retain separate source/cleaning provenance per fit."""
        from .backtest import assess_forecasts, validation_baselines

        inverters = {row.inverter_id: row for row in configuration.inverters}
        forecast_physical = validation_baselines(configuration, location, forecast_runs, now)
        learning, failed = {}, False
        for source in configuration.sources:
            if source.entity_id not in histories:
                continue
            try:
                result = fit_boosted_model(
                    source,
                    inverters[source.inverter_id],
                    location,
                    histories[source.entity_id],
                    hourly,
                    weather,
                    now,
                    forecast_runs=forecast_runs,
                    forecast_physical=forecast_physical,
                )
                if result.model:
                    assessment = assess_forecasts(
                        configuration,
                        source,
                        location,
                        histories[source.entity_id],
                        forecast_runs,
                        now,
                        result.model.policy_version,
                        self.store,
                        histories=histories,
                    )
                    result = replace(
                        result, summary=replace(result.summary, historical_assessment=assessment)
                    )
                metadata = None
                if result.model:
                    metadata = self.store.archive_model(result.model)
                record = {
                    "source_id": source.source_id,
                    "configuration_version": configuration.version,
                    "location": document(location),
                    "history_version": version(document(histories[source.entity_id])),
                    "weather_version": version([document(row) for row in weather]),
                    "forecast_run_references": [run.version for run in forecast_runs],
                    "model": metadata,
                    "summary": document(result.summary),
                }
                reference = version(record)
                self.store.archive("ml-training", reference, record)
                learning[source.source_id] = replace(result, archive_reference=reference)
            except (ImportError, ValueError, RuntimeError, OSError) as error:
                failed = True
                self.learning_log.exception(
                    "PVWatts remains available. Retrying with the next usable solar forecast update.",
                    expected=True,
                    reason=type(error).__name__,
                )
                _LOGGER.debug(
                    "Solar ML candidate learning failure (%s).\n%s",
                    type(error).__name__,
                    failure_trace(error),
                )
                learning[source.source_id] = TrainingResult(
                    None,
                    TrainingSummary(
                        TrainingStatus.FAILED,
                        len(histories[source.entity_id].hours) - 1,
                        0,
                        0,
                        {},
                        (),
                        issue="The ML candidate could not be updated. Simpler forecasts remain usable.",
                    ),
                )
        if learning and not failed:
            self.learning_log.success(
                "Training checks completed. Candidate activation still requires matched forecast evidence."
            )
        return learning

    def install(self, result, now):
        """Preserve every captured input guard except additive immutable forecast-history imports."""
        current_key = self.key(now)
        if current_key is None or result[0][:-1] != current_key[:-1]:
            return False
        (
            self.last_key,
            self.forecast,
            self.calibrations,
            self.candidate,
            self.baseline,
            self.source_selection,
            self.ha_issues,
            self.learning,
        ) = result
        self.failed_key, self.retry_after, self.issue = None, None, None
        self.last_fit_at = now
        return True

    def failed(self, job, now):
        """Return whether this failure received backoff, without changing newer-input diagnostics."""
        if job[0] == self.key(now):
            self.failed_key, self.retry_after = job[0], now + timedelta(minutes=MODEL_RETRY_MINUTES)
            self.issue = "solar_forecast_unusable"
            return True
        return False

    def active_model(self, source_id):
        """Name the actual per-source predictor, not a combined forecast's mixed calibration label."""
        return self.source_selection.get(source_id, {}).get(
            "model", self.baseline.model if self.baseline else None
        )

    def document(self, now, *, series=False):
        """Expose active prediction, per-source candidates and actionable status without recomputation."""
        forecast = forecast_document(self.forecast, series=series)
        candidate = forecast_document(self.candidate, series=series)
        statuses = {}
        if self.configuration:
            inverters = {
                inverter.inverter_id: inverter for inverter in self.configuration.inverters
            }
            fitted = {candidate_item.source_id for candidate_item in self.calibrations}
            for source in self.configuration.sources:
                status = learning_blocker(source, inverters[source.inverter_id])
                if status is not None:
                    statuses[source.source_id] = status
                    continue
                if source.entity_id not in self.histories:
                    status = "history_required"
                else:
                    status = (
                        "active"
                        if source.source_id in fitted
                        and self.source_selection.get(source.source_id, {}).get("active")
                        == ForecastSource.ML
                        else "waiting_for_accuracy_evidence"
                        if source.source_id in fitted
                        else "insufficient_usable_history"
                    )
                statuses[source.source_id] = status
        return {
            "configuration": configuration_document(self.configuration),
            "forecast": forecast,
            "candidate": candidate,
            "calibrations": [
                {
                    "source_id": model.source_id,
                    "trained_until": model.trained_until.isoformat(),
                    "training_version": model.training_version,
                    "status": "nonlinear_shadow",
                }
                for model in self.calibrations
            ],
            "calibration_status": statuses,
            "learning": {
                source_id: {
                    **{
                        key: value
                        for key, value in document(row.summary).items()
                        if key not in ("excluded_periods", "diagnostic_periods")
                    },
                    "candidate_model": row.model.schema if row.model else None,
                    "audit_reference": row.archive_reference,
                }
                for source_id, row in self.learning.items()
            },
            "source_selection": self.source_selection,
            "ha_issues": self.ha_issues,
            "source_models": {
                source.source_id: self.active_model(source.source_id)
                for source in self.configuration.sources
            }
            if self.configuration
            else {},
            "source_model_labels": {
                source.source_id: FORECAST_MODELS.get(
                    self.active_model(source.source_id), self.active_model(source.source_id)
                )
                for source in self.configuration.sources
            }
            if self.configuration
            else {},
            "issue": self.issue,
            "history_issues": dict(self.history_issues),
            "weather_service": dict(self.weather_service),
            "history_import": self.history_import.status(
                (source.entity_id for source in self.configuration.sources)
                if self.configuration
                else (),
                self.histories,
            ),
            "request": self.request(now),
        }
