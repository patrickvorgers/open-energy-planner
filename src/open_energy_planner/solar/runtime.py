"""Versioned solar orchestration. Background workers own model calculation and immutable writes."""

from dataclasses import asdict, replace
from datetime import timedelta
from importlib.metadata import version as distribution_version

from open_energy_planner.forecast_quality.policy import FORECAST_MODELS
from open_energy_planner.solar.calculation.model import ForecastSource, SolarForecast, version

from ..planning_policy import FINE_INTERVAL_MINUTES
from .ha_forecasts import HA_REFRESH_MAX_AGE
from .learning import calibration_blocker, fit
from .model_policy import DEFAULT_GROUND_ALBEDO, DEFAULT_TEMPERATURE_COEFFICIENT_MAGNITUDE
from .source_selection import MIN_ACCURACY_DAYS, SOURCE_POLICY, choose_sources, ha_energies
from .wire import configuration_document, document, forecast_document

HISTORY_LOOKBACK_DAYS = 365
HISTORY_RETRY_MINUTES = 15
EAST_WEST_ARRAY_SHARE = 0.5
MODEL_RETRY_MINUTES = 5


class SolarRuntime:
    """Cache independent per-source history and physical forecasts without provider access."""

    def __init__(self, store):
        """Restore settings while requiring new weather/location before publishing live forecasts."""
        self.store = store
        self.configuration, self.issue = store.current, store.issue
        self.location, self.weather = None, ()
        self.weather_revision, self.history_revision = 0, 0
        self.histories, self.ranges, self.history_issues = {}, {}, {}
        self.history_retry_after = {}
        self.forecast, self.candidate, self.calibrations = None, None, ()
        self.baseline = None
        self.last_key, self.failed_key, self.retry_after = None, None, None
        self.ha_predictions, self.source_selection, self.ha_issues = {}, {}, {}
        self.ha_revision = 0

    def configure(self, configuration):
        """Reset incompatible live state only after a changed definition has been persisted."""
        self.configuration, self.issue = configuration, None
        self.history_revision += 1
        self.histories, self.ranges, self.history_issues = {}, {}, {}
        self.history_retry_after = {}
        self.forecast, self.candidate, self.calibrations, self.last_key = None, None, (), None
        self.baseline = None
        self.failed_key, self.retry_after = None, None
        self.source_selection = {}

    def save_ha(self, predictions):
        """Retain original cache observations and each real refresh without overwriting earlier receipts."""
        with self.store.documents.transaction():
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

    def set_weather(self, location, weather):
        """Install typed source evidence. Changing the site revokes old-site weather and outputs."""
        if self.location != location:
            self.forecast, self.candidate, self.baseline, self.last_key = None, None, None, None
        self.location, self.weather = location, tuple(weather)
        self.weather_revision += 1

    def request(self, now):
        """Request one source's daily bounded history without requiring all inverters to succeed together."""
        if self.configuration is None or self.store.issue:
            return None
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(days=HISTORY_LOOKBACK_DAYS, hours=1)
        for source in self.configuration.sources:
            if self.ranges.get(source.entity_id) != (start, end) or (
                source.entity_id in self.history_issues
                and now >= self.history_retry_after[source.entity_id]
            ):
                body = {
                    "entity_id": source.entity_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "configuration_version": self.configuration.version,
                }
                return {**body, "version": version(body)}
        return None

    def receive(self, ref, history, now, *, error=None):
        """Reject delayed source replies and advance failed channels so other sources can still learn."""
        from datetime import datetime

        pending = self.request(now)
        if pending is None or pending["version"] != ref:
            raise ValueError("Solar history request changed")
        entity = pending["entity_id"]
        start, end = (datetime.fromisoformat(pending[key_item]) for key_item in ("start", "end"))
        if error:
            self.history_issues[entity] = error
            self.history_retry_after[entity] = now + timedelta(minutes=HISTORY_RETRY_MINUTES)
        else:
            if history.entity_id != entity or any(
                not start <= hour.start < end for hour in history.hours
            ):
                raise ValueError("Solar history source/range mismatch")
            self.histories[entity] = history
            self.history_revision += 1
            self.history_issues.pop(entity, None)
            self.history_retry_after.pop(entity, None)
        # A read failure does not loop on source one forever. Retry next daily
        # cohort or after fifteen minutes. A failed channel is never zero production.
        self.ranges[entity] = start, end

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
        expired = bool(self.weather and now - self.weather[-1].received_at > timedelta(hours=2))
        if expired and not ha_only:
            self.issue = "solar_weather_expired"
            return None
        key = self.key(now)
        if key == self.last_key or (
            key == self.failed_key and self.retry_after and now < self.retry_after
        ):
            return None
        return (
            key,
            self.configuration,
            self.location,
            () if expired else self.weather,
            tuple(self.histories.items()),
            now,
            self.ha_predictions,
        )

    def build(self, job):
        """Compute and archive exact physical output plus shadow corrections outside the event loop."""
        from open_energy_planner.solar.calculation.predictor import PVWattsPredictor

        key, configuration, location, weather, histories, now, ha_predictions = job
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
            return key, forecast, (), None, None, statuses, {}
        predictor = PVWattsPredictor()
        forecast = predictor.forecast(configuration, location, weather, now)
        hourly = predictor.hourly(
            configuration,
            location,
            tuple(weather_item for weather_item in weather if weather_item.start < now),
        )
        histories = dict(histories)
        inverters = {inverter.inverter_id: inverter for inverter in configuration.inverters}
        calibrations = tuple(
            replace(
                candidate,
                configuration_version=configuration.version,
                weather_version=forecast.weather_version,
                history_version=version(document(histories[source.entity_id])),
            )
            for source in configuration.sources
            if (
                candidate := fit(
                    source,
                    histories.get(source.entity_id),
                    hourly,
                    now,
                    inverters[source.inverter_id],
                )
            )
            is not None
        )
        self.store.archive_series(
            "weather",
            forecast.weather_version,
            [document(weather_item) for weather_item in weather],
        )
        for history in histories.values():
            body = document(history)
            self.store.archive_series(
                "history", version(body), body["hours"], entity=history.entity_id
            )
        for calibration in calibrations:
            body = document(calibration)
            self.store.archive("calibration", version(asdict(calibration)), body)
        candidate = (
            predictor.forecast(configuration, location, weather, now, calibrations=calibrations)
            if calibrations
            else None
        )
        if candidate:
            self.store.archive(
                "forecast", candidate.version, {"forecast": forecast_document(candidate)}
            )
        self.store.archive(
            "forecast",
            forecast.version,
            {
                "forecast": forecast_document(forecast),
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
        )
        selected = tuple(
            row for row in calibrations if choices[row.source_id][0] == ForecastSource.ML
        )
        names = {
            ForecastSource.PVWATTS: baseline.model,
            ForecastSource.ML: candidate.model if candidate else None,
            ForecastSource.HOME_ASSISTANT: "ha-production-v1",
        }
        models = {source_id: names[choice[0]] for source_id, choice in choices.items()}
        for source_id, status in selection_status.items():
            status["model"] = models[source_id]
        if any(choice[0] != ForecastSource.PVWATTS for choice in choices.values()):
            forecast = replace(
                baseline,
                energies=tuple(row for choice in choices.values() for row in choice[1]),
                model="solar-selected-v1",
                quality="selected",
                calibration_versions=tuple(version(asdict(row)) for row in selected),
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
                self.store.archive(
                    "quality-source-models",
                    archived.version,
                    {
                        source.source_id: (
                            models[source.source_id]
                            if archived is forecast
                            else archived.model
                            if source.source_id in calibrated
                            else baseline.model
                        )
                        for source in configuration.sources
                    },
                )
        return key, forecast, calibrations, candidate, baseline, selection_status, ha_issues

    def install(self, result, now):
        """Never let a delayed worker replace forecasts for newer source definitions or weather."""
        if result[0] != self.key(now):
            return False
        (
            self.last_key,
            self.forecast,
            self.calibrations,
            self.candidate,
            self.baseline,
            self.source_selection,
            self.ha_issues,
        ) = result
        self.failed_key, self.retry_after, self.issue = None, None, None
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
                status = calibration_blocker(source, inverters[source.inverter_id])
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
            "calibrations": [document(candidate_item) for candidate_item in self.calibrations],
            "calibration_status": statuses,
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
            "request": self.request(now),
        }
