"""Versioned solar orchestration. Background workers own model calculation and immutable writes."""

import logging
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as distribution_version
from threading import Lock
from zoneinfo import ZoneInfo

from open_energy_planner.forecast_quality.policy import FORECAST_MODELS
from open_energy_planner.runtime.diagnostics.logging import BackgroundLog, failure_trace
from open_energy_planner.solar.calculation.model import (
    ForecastSource,
    SolarForecast,
    horizon,
    version,
)
from open_energy_planner.weather.run import (
    FORECAST_RUN_RESPONSE_HOURS,
    MAX_LIVE_RUN_AGE_HOURS,
    ForecastWeatherRun,
)

from ..planning_policy import FINE_INTERVAL_MINUTES, PLANNING_HORIZON_HOURS
from .boosting import TrainingResult, TrainingStatus, TrainingSummary, fit_boosted_model
from .calculation.projection import (
    forecast_window,
    learned_forecast,
    limit_ac_production,
    live_forecast_grid,
)
from .curtailment import (
    FEEDBACK_EPOCH,
    FEEDBACK_REFRESH_MINUTES,
    MAX_FEEDBACK_RANGE_DAYS,
    FeedbackRequestError,
    archive_feedback,
    load_feedback,
    parse_feedback,
    save_feedback,
)
from .ha_forecasts import HA_REFRESH_MAX_AGE
from .model_policy import DEFAULT_GROUND_ALBEDO, DEFAULT_TEMPERATURE_COEFFICIENT_MAGNITUDE
from .source_selection import MIN_ACCURACY_DAYS, SOURCE_POLICY, choose_sources, ha_energies
from .storage.history import HistoryContinuityError, SolarHistoryImport
from .training import RETRYABLE_TRAINING, DailySolarTraining
from .wire import configuration_document, document, forecast_document

HISTORY_RETRY_MINUTES = 15
HARD_HISTORY_ISSUES = frozenset({"history_continuity_unavailable", "history_cache_unreadable"})
EAST_WEST_ARRAY_SHARE = 0.5
MODEL_RETRY_MINUTES = 5
MIN_HISTORY_MODEL_UPDATE_SECONDS = 60
FEEDBACK_RESTORE_RETRY_SECONDS = 300
_LOGGER = logging.getLogger(__name__)


class SolarRuntime:
    """Cache independent per-source history and physical forecasts without provider access."""

    def __init__(self, store):
        """Restore settings while requiring new weather/location before publishing live forecasts."""
        self.store = store
        self.weather_archive = store
        self.training = DailySolarTraining(store)
        self.configuration_lock = Lock()
        self.configuration, self.issue = store.current, store.issue
        self.restore_training_revisions()
        self.location, self.weather = None, ()
        self.weather_received_at = None
        self.run_evidence_revision = None
        self.forecast_runs, self.weather_service = (), {}
        self.forecast_runs_revision, self.last_fit_at = 0, None
        self.forecast_cohort_revision = 0
        self.weather_revision, self.history_revision = 0, 0
        self.histories, self.ranges, self.history_issues = {}, {}, {}
        self.history_retry_after = {}
        self.feedback = {}
        self.feedback_revision = 0
        self.feedback_logs = {}
        self.feedback_retry_after = {}
        self.restore_feedback()
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

    def save_configuration(self, configuration):
        """Persist and restore settings in the caller-owned worker while its application lock is held.

        The Solar configuration lock excludes notification reads of partial state
        without treating unrelated application work as a failed notification source.
        """
        with self.configuration_lock:
            self.store.save(configuration)
            self.store.issue = "solar_configuration_unreadable"
            self.configure(configuration)
            self.store.issue = None

    def restore_committed_configuration(self):
        """Retry active settings restoration while excluding partial notification reads."""
        with self.configuration_lock:
            self.configure(self.store.current)
            self.store.issue = None

    def configure(self, configuration):
        """Reset model state after persistence, retaining validated unchanged meter cohorts.

        Production writes run this method in the storage worker while their caller
        holds the app lock. Geometry,
        control declarations and forecast preferences do not change immutable
        meter history. Only unchanged source/entity/learning-date bindings with
        an accepted importer state may reuse their already validated aggregate.
        New bindings and selected learning cohorts use the full restore path.
        """
        previous = (
            {source.source_id: source for source in self.configuration.sources}
            if self.configuration
            else {}
        )
        unchanged = {
            source.entity_id: source.learning_start
            for source in configuration.sources
            if source.source_id in previous
            and (previous[source.source_id].entity_id, previous[source.source_id].learning_start)
            == (source.entity_id, source.learning_start)
        }
        retained = {
            entity: history
            for entity, history in self.histories.items()
            if entity in unchanged
            and (state := self.history_import.states.get(entity))
            and state["entity_id"] == entity
            and state.get("learning_start") == unchanged[entity]
            and self.history_import.learning_starts.get(entity) == unchanged[entity]
        }
        issues = {
            entity: issue for entity, issue in self.history_issues.items() if entity in unchanged
        }
        retries = {
            entity: retry
            for entity, retry in self.history_retry_after.items()
            if entity in unchanged
        }
        self.configuration, self.issue = configuration, None
        self.restore_training_revisions()
        self.restore_feedback()
        self.history_revision += 1
        self.ranges, self.history_issues = {}, issues
        self.history_import.configure(configuration.sources)
        self.histories = {
            **retained,
            **self.history_import.restore(
                source.entity_id
                for source in configuration.sources
                if source.entity_id not in retained
            ),
        }
        self.history_issues.update(self.history_import.issues)
        self.history_retry_after = retries
        self.forecast, self.candidate, self.calibrations, self.last_key = None, None, (), None
        self.baseline = None
        self.failed_key, self.retry_after = None, None
        self.source_selection = {}
        self.learning = {}

    def restore_training_revisions(self):
        """Restore compact generations during worker-owned state changes, keeping job snapshots memory-only."""
        self.evidence_revisions = (
            self.training.revisions(source.entity_id for source in self.configuration.sources)
            if self.configuration
            else {}
        )

    def restore_feedback(self):
        """Restore only configured sensor identities without treating unreadable evidence as absent feedback."""
        entities = (
            {
                source.curtailment_entity_id
                for source in self.configuration.sources
                if source.curtailment_entity_id
            }
            if self.configuration
            else set()
        )
        self.feedback_logs = {
            entity: log for entity, log in self.feedback_logs.items() if entity in entities
        }
        now = datetime.now(UTC)
        issues = getattr(self, "feedback_issues", {})
        retained = {
            entity: history
            for entity, history in self.feedback.items()
            if entity in entities
            and entity not in issues
            and (history is None or history.entity_id == entity and history.checked_until <= now)
        }
        restored, self.feedback_issues = self.read_feedback(entities - retained.keys(), now)
        self.feedback = {**retained, **restored}
        self.feedback_retry_after = {
            entity: now + timedelta(seconds=FEEDBACK_RESTORE_RETRY_SECONDS)
            for entity in self.feedback_issues
        }
        self.feedback_revision += 1

    def read_feedback(self, entities, now):
        """Read archives without installing feedback and return restored histories and issues.

        Startup calls this before publication. Configuration calls it in the storage
        worker with the caller's application lock held. Background retries use a
        worker without that lock. Only per-source feedback_logs are created or
        updated here. self.feedback is never changed. Expected read errors become
        sanitized issues and deduplicated logs instead of propagating.
        """
        restored, issues = {}, {}
        for entity in entities:
            log = self.feedback_logs.setdefault(
                entity, BackgroundLog(_LOGGER, "Solar curtailment feedback restore")
            )
            try:
                restored[entity] = load_feedback(self.store.documents, entity, now)
            except (OSError, ValueError, TypeError, KeyError) as error:
                restored[entity] = None
                issues[entity] = (
                    "Curtailment feedback history is unreadable. Check archive storage. Production history is retained."
                )
                log.exception(
                    "Linked production remains excluded until its retained feedback can be read.",
                    retry_seconds=FEEDBACK_RESTORE_RETRY_SECONDS,
                    expected=True,
                    reason=type(error).__name__,
                )
                _LOGGER.debug(
                    "Solar curtailment feedback restore traceback (%s).\n%s",
                    type(error).__name__,
                    failure_trace(error),
                )
            else:
                if restored[entity] is not None and restored[entity].count:
                    log.success("Retained feedback is readable. Background capture can continue.")
                else:
                    log.unavailable(
                        "Background capture can continue.",
                        reason="no retained observations",
                        waiting=True,
                    )
        return restored, issues

    def feedback_restore_job(self, now):
        """Capture a bounded retry cohort without doing archive I/O under the live state lock."""
        entities = tuple(
            entity for entity in self.feedback_issues if now >= self.feedback_retry_after[entity]
        )
        if not entities or self.configuration is None:
            return None
        for entity in entities:
            self.feedback_retry_after[entity] = now + timedelta(
                seconds=FEEDBACK_RESTORE_RETRY_SECONDS
            )
        return self.configuration.version, entities

    def install_feedback_restore(self, job, result):
        """Accept recovery only for the same configured sources, preserving failures and healthy peers."""
        if self.configuration is None or self.configuration.version != job[0]:
            return
        restored, issues = result
        for entity in job[1]:
            if entity not in issues and entity in self.feedback_issues:
                self.feedback[entity] = restored[entity]
                self.feedback_issues.pop(entity)
                self.feedback_retry_after.pop(entity, None)
                self.feedback_revision += 1
                self.last_key = None

    def feedback_requests(self, now):
        """Request independent ranges so one missing sensor cannot starve another source."""
        if self.store.issue:
            return []
        requests = []
        for entity, history in self.feedback.items():
            if entity in self.feedback_issues:
                continue
            if history is None or now - history.checked_until >= timedelta(
                minutes=FEEDBACK_REFRESH_MINUTES
            ):
                start = history.checked_until if history else FEEDBACK_EPOCH
                requests.append(
                    {
                        "entity_id": entity,
                        "configuration_version": self.configuration.version,
                        "start": start.isoformat(),
                        "end": min(
                            now, start + timedelta(days=MAX_FEEDBACK_RANGE_DAYS)
                        ).isoformat(),
                    }
                )
        return requests

    def receive_feedback(self, body, now):
        """Persist sensor-bound feedback with the caller's application lock held.

        The API delegates this call to its cancellation-safe storage worker.
        save_feedback commits before self.feedback changes. A revision change
        invalidates learning, source selection and the forecast. Storage failures
        propagate without installing the rejected response.

        Raises:
            FeedbackRequestError: The response is invalid or its request is stale.
            RuntimeError: The sensor's retained feedback archive is unreadable.
            ValueError: The persisted pointer no longer matches the request.
        """
        try:
            if not isinstance(body, dict) or set(body) != {
                "configuration_version",
                "start",
                "feedback",
            }:
                raise ValueError("Invalid solar feedback response")
            if (
                self.store.issue
                or self.configuration is None
                or body["configuration_version"] != self.configuration.version
            ):
                raise ValueError("Solar feedback configuration changed")
            incoming = parse_feedback(body["feedback"], now)
            if incoming.entity_id not in self.feedback:
                raise ValueError("Solar feedback sensor changed")
            previous = self.feedback[incoming.entity_id]
            start = datetime.fromisoformat(body["start"])
            if start != (previous.checked_until if previous else FEEDBACK_EPOCH):
                raise ValueError("Solar feedback request changed")
            if (
                not start
                < incoming.checked_until
                <= start + timedelta(days=MAX_FEEDBACK_RANGE_DAYS)
            ):
                raise ValueError("Solar feedback range changed")
            if any(at < start for at, _state in incoming.readings):
                raise ValueError("Solar feedback observations precede the request")
        except (ValueError, TypeError, KeyError, OverflowError):
            raise FeedbackRequestError("Invalid or outdated solar curtailment feedback") from None
        if incoming.entity_id in self.feedback_issues:
            raise RuntimeError("Solar feedback archive is unreadable")
        boundary = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.store.transaction():
            accepted = save_feedback(self.store, previous, incoming, start)
            if any(at < boundary for at, _state in incoming.readings):
                self.training.invalidate(
                    source.entity_id
                    for source in self.configuration.sources
                    if source.curtailment_entity_id == incoming.entity_id
                )
            revisions = self.training.revisions(
                source.entity_id for source in self.configuration.sources
            )
        self.evidence_revisions = revisions
        self.feedback[incoming.entity_id] = accepted
        if (
            previous is None
            or incoming.readings
            or (
                previous.checked_until.replace(minute=0, second=0, microsecond=0)
                != accepted.checked_until.replace(minute=0, second=0, microsecond=0)
            )
        ):
            self.feedback_revision += 1
            self.last_key = None
            self.learning, self.source_selection = {}, {}
            self.forecast, self.candidate, self.calibrations = self.baseline, None, ()

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

    def set_weather(self, location, weather, *, service=None, received_at=None):
        """Install typed source evidence. Changing the site revokes old-site weather and outputs."""
        if self.location != location:
            self.forecast, self.candidate, self.baseline, self.last_key = None, None, None, None
            self.learning = {}
            self.forecast_runs = ()
            self.run_evidence_revision = None
            self.forecast_runs_revision += 1
        incoming = tuple(weather)
        provenance = dict(service) if service is not None else {}
        received = received_at or (incoming[-1].received_at if incoming else None)
        if (
            location == self.location
            and len(incoming) == len(self.weather)
            and {key: value for key, value in provenance.items() if key != "received_at"}
            == {key: value for key, value in self.weather_service.items() if key != "received_at"}
            and all(
                original == replace(current, received_at=original.received_at)
                for original, current in zip(self.weather, incoming)
            )
        ):
            if received is not None:
                self.weather_received_at = max(received, self.weather_received_at or received)
            return
        self.weather_revision += 1
        self.location, self.weather, self.weather_service = location, incoming, provenance
        self.weather_received_at = received

    def set_forecast_runs(self, location, runs, *, authoritative=True):
        """Admit partial loaded runs, or reconcile an authoritative snapshot including explicit withdrawals."""
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
        selected = tuple(run.as_original() for run in selected)
        destructive, revisions, run_revision = self.training.accept_runs(
            site,
            selected,
            (source.entity_id for source in self.configuration.sources)
            if self.configuration
            else (),
            authoritative=authoritative,
        )
        if not authoritative:
            admitted = {run.initialized_at: run for run in self.forecast_runs}
            admitted.update((run.initialized_at, run) for run in selected)
            selected = tuple(admitted[initialized] for initialized in sorted(admitted))
        destructive = (
            destructive
            or revisions != self.evidence_revisions
            or (
                self.run_evidence_revision is not None
                and run_revision != self.run_evidence_revision
            )
        )
        self.evidence_revisions, self.run_evidence_revision = revisions, run_revision
        if selected == self.forecast_runs and not destructive:
            return False
        incoming = {run.initialized_at: run.version for run in selected}
        if destructive or any(
            incoming.get(run.initialized_at) != run.version for run in self.forecast_runs
        ):
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
        advance to other sources until their bounded retry. Identity changes
        retain the prior accepted cache and the original incoming evidence.
        Overlap conflicts and revised sums exclude affected endpoints from the
        replacement aggregate, removing it when no usable endpoints remain.
        Missing or changed accepted hours invalidate learned outputs.
        """
        pending = self.request(now)
        if pending is None or pending["version"] != ref:
            raise ValueError("Solar history request changed")
        entity = pending["entity_id"]
        if not error:
            try:
                with self.history_import.transaction():
                    accepted = self.history_import.receive(pending, report, now)
                    previous = self.histories.get(entity)
                    fresh = (
                        {hour.start: hour.total_kwh for hour in accepted.hours} if accepted else {}
                    )
                    destructive = previous and any(
                        fresh.get(hour.start) != hour.total_kwh for hour in previous.hours
                    )
                    if destructive:
                        self.training.invalidate((entity,))
                    revisions = self.training.revisions(
                        source.entity_id for source in self.configuration.sources
                    )
            except HistoryContinuityError:
                error = "history_continuity_unavailable"
            else:
                self.evidence_revisions = revisions
                if destructive:
                    self.last_key = None
                    self.forecast, self.candidate, self.calibrations = self.baseline, None, ()
                    self.source_selection, self.learning = {}, {}
                if accepted:
                    self.histories[entity] = accepted
                else:
                    self.histories.pop(entity, None)
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
                now.astimezone(ZoneInfo(self.location.timezone)).date() if self.location else None,
                self.weather_revision,
                self.history_revision,
                self.ha_revision,
                tuple(
                    (entity, tuple(row.fresh(now) for row in providers))
                    for entity, providers in sorted(self.ha_predictions.items())
                ),
                self.forecast_cohort_revision,
                self.feedback_revision,
                self.forecast_runs_revision,
            )
            if self.configuration and not self.store.issue
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
            expired = (
                self.weather_received_at is not None
                and now - self.weather_received_at > timedelta(hours=2)
            )
        if expired and not ha_only:
            self.issue = "solar_weather_expired"
            return None
        key = self.key(now)
        if key != self.last_key and self._reuse_forecast(key, now, expired):
            self.last_key = key
            self.forecast = replace(self.forecast, checked_at=now)
            self.issue = None
            return None
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
        self.issue = "solar_forecast_updating"
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
            dict(self.feedback),
            dict(self.evidence_revisions),
            self.run_evidence_revision,
        )

    def _reuse_forecast(self, key, now, weather_expired):
        """Reuse one immutable issue when only the planning quarter advanced."""
        forecast = self.forecast
        if (
            self.last_key is None
            or forecast is None
            or key[:1] != self.last_key[:1]
            or key[2:] != self.last_key[2:]
            or (weather_expired and forecast.weather_version != "none")
            or (forecast.valid_until is not None and now >= forecast.valid_until)
            or (
                now.astimezone(UTC).date() != forecast.freshness_at.astimezone(UTC).date()
                and any(
                    (result.attempt_status or result.summary.status) in RETRYABLE_TRAINING
                    for result in self.learning.values()
                )
            )
        ):
            return False
        grid = horizon(now)
        end = grid[-1][0] + timedelta(hours=grid[-1][1])
        # A short HA provider horizon cannot satisfy a full planning-window reuse.
        ends = {
            row.source_id: row.start + timedelta(hours=row.duration_h) for row in forecast.energies
        }
        return all(ends.get(source.source_id, now) >= end for source in self.configuration.sources)

    def _build_ha_only_forecast(self, configuration, location, now, ha_predictions):
        """Archive an explicit Home Assistant forecast when no weather fallback is available."""
        energies, issues = ha_energies(configuration, None, ha_predictions, now)
        if issues or len(energies) != len(configuration.sources):
            raise ValueError("HA solar forecast unavailable without weather fallback")
        forecast = SolarForecast(
            configuration.version,
            location,
            now,
            "none",
            limit_ac_production(
                configuration, tuple(row for rows in energies.values() for row in rows)
            ),
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
        ha_references = {
            entity: [row.version for row in providers]
            for entity, providers in ha_predictions.items()
        }
        self.store.archive(
            "forecast",
            forecast.version,
            {
                "forecast": forecast_document(forecast),
                "source_selection": statuses,
                "ha_references": ha_references,
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
                "ha_references": ha_references,
            },
        )
        return forecast, statuses

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
            feedback,
            evidence_revisions,
            run_evidence_revision,
        ) = job
        if not weather:
            forecast, statuses = self._build_ha_only_forecast(
                configuration, location, now, ha_predictions
            )
            return key, forecast, (), None, None, statuses, {}, {}
        predictor = PVWattsPredictor()
        grid = live_forecast_grid(now, weather)
        forecast = predictor.forecast(configuration, location, weather, now, grid=grid)
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
        self.weather_archive.archive_series(
            "weather", forecast.weather_version, [document(row) for row in weather]
        )
        for history in histories.values():
            body = document(history)
            self.store.archive_series(
                "history", version(body), body["hours"], entity=history.entity_id
            )
        learning = self._learned_candidates(
            configuration,
            location,
            histories,
            hourly,
            weather,
            now,
            forecast_runs,
            feedback=feedback,
            evidence_revisions=evidence_revisions,
            run_evidence_revision=run_evidence_revision,
        )
        models_by_source = {
            source_id: result.model
            for source_id, result in learning.items()
            if result.model is not None
        }
        try:
            candidate = learned_forecast(
                configuration, location, weather, now, models_by_source, grid=grid
            )
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
                    attempt_status=TrainingStatus.FAILED,
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
            feedback=feedback,
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
                energies=limit_ac_production(
                    configuration, tuple(row for choice in choices.values() for row in choice[1])
                ),
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
        self,
        configuration,
        location,
        histories,
        hourly,
        weather,
        now,
        forecast_runs=(),
        *,
        feedback=None,
        evidence_revisions=None,
        run_evidence_revision=None,
    ):
        """Persist bounded tree weights once and retain separate source/cleaning provenance per fit."""
        from .backtest import assess_forecasts, validation_baselines

        inverters = {row.inverter_id: row for row in configuration.inverters}
        forecast_physical = validation_baselines(configuration, location, forecast_runs, now)
        learning, failed = {}, False
        for source in configuration.sources:
            if source.entity_id not in histories:
                continue
            token = {}
            try:
                cached, token = self.training.prepare(
                    source,
                    inverters[source.inverter_id],
                    location,
                    histories[source.entity_id],
                    weather,
                    forecast_runs,
                    (feedback or {}).get(source.curtailment_entity_id),
                    now,
                    evidence_revision=(evidence_revisions or {}).get(source.entity_id),
                    run_evidence_revision=run_evidence_revision,
                )
                if cached is not None:
                    learning[source.source_id] = cached
                    continue
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
                    feedback=(feedback or {}).get(source.curtailment_entity_id),
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
                        feedback=feedback,
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
                    "curtailment_feedback_reference": archive_feedback(
                        self.store, (feedback or {})[source.curtailment_entity_id]
                    )
                    if (feedback or {}).get(source.curtailment_entity_id) is not None
                    else None,
                }
                learning[source.source_id] = self.training.save(token, result, record)
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
                learning[source.source_id] = self.training.fallback(token) or TrainingResult(
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

    def ha_history(self, now):
        """Expose requested history and curtailment feedback independently of forecast diagnostics."""
        return {
            "feedback_requests": self.feedback_requests(now),
            "issue": self.issue,
            "history_issues": dict(self.history_issues),
            "request": self.request(now),
        }

    def document(self, now, *, series=False):
        """Expose active prediction, per-source candidates and actionable status without recomputation."""
        forecast = forecast_document(self.forecast, series=False)
        candidate = forecast_document(self.candidate, series=False)
        if series:
            for issue, view in ((self.forecast, forecast), (self.candidate, candidate)):
                if issue is not None:
                    view["energies"] = [
                        document(row) for row in forecast_window(issue.energies, now)
                    ]
        statuses = {}
        if self.configuration:
            fitted = {candidate_item.source_id for candidate_item in self.calibrations}
            for source in self.configuration.sources:
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
            **self.ha_history(now),
            "feedback_issues": dict(self.feedback_issues),
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
            "weather_service": dict(self.weather_service),
            "history_import": self.history_import.status(
                (source.entity_id for source in self.configuration.sources)
                if self.configuration
                else (),
                self.histories,
            ),
        }
