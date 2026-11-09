"""Cached demand orchestration. Expensive training runs only in an app-owned background executor."""

import logging
from dataclasses import asdict
from datetime import timedelta

from open_energy_planner.demand.calculation.forecasting import (
    RidgeDemandModel,
    WeekdayProfile,
    predict,
)
from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.history.importer import HistoryImport
from open_energy_planner.history.storage import HistoryArchive

from ..planning_policy import FINE_INTERVAL_MINUTES
from .evaluation import compare, evaluation_cutoffs
from .reconstruction import DemandReconstructor
from .wire import configuration_document, forecast_document

HISTORY_RETRY_MINUTES = 15
MODEL_RETRY_MINUTES = 5
_LOGGER = logging.getLogger(__name__)


class DemandRuntime:
    """Own configured demand inputs and cached predictions without fetching HA or weather itself."""

    def __init__(self, store, *, scorer=None, plans=None):
        """Restore configuration. Never restore stale predictions as live evidence after restart."""
        self.store, self.scorer, self.plans = store, scorer, plans
        self.configuration = store.current
        self.histories, self.data_versions = {}, {}
        self.pending_histories = {}
        self.temperatures = ()
        self.weather_version = "none"
        self.forecast = self.candidate = self.baseline = None
        self.learned_preferred = False
        self.candidate_features = "time"
        self.last_key = None
        self.failed_key, self.retry_after = None, None
        self.issue = store.issue
        self.model_status = "waiting_for_configuration"
        self.history_issues = {}
        self.history_quality = None
        self.comparison = None
        self.daily_selection = None
        from open_energy_planner.appliances.forecasting import ConsumerForecasts

        self.history_import = HistoryImport(HistoryArchive(store.documents))
        self.history_retry_after = {}
        self.pending_histories = self.history_import.restore(
            self.configuration.entities if self.configuration else ()
        )
        self.histories = self._restore_cohort()
        self.history_issues.update(self.history_import.issues)
        self.data_versions = {
            entity: digest(asdict(history)) for entity, history in self.histories.items()
        }
        self.consumers = ConsumerForecasts(store.documents)

    def configure(self, configuration):
        """Invalidate incompatible observations/models after a persisted measurement-definition change."""
        self.configuration = configuration
        self.pending_histories = self.history_import.restore(configuration.entities)
        self.history_issues = dict(self.history_import.issues)
        self.histories = self._restore_cohort()
        self.data_versions = {
            entity: digest(asdict(history)) for entity, history in self.histories.items()
        }
        self.history_retry_after = {}
        self.forecast = self.candidate = self.baseline = self.last_key = None
        self.learned_preferred = False
        self.candidate_features = "time"
        self.failed_key, self.retry_after = None, None
        self.history_quality = None
        self.comparison = None
        self.daily_selection = None
        self.issue = None
        self.model_status = "waiting_for_refresh"

    def _restore_cohort(self):
        """Expose only the last completely accepted household boundary after restart."""
        if self.configuration is None:
            return {}
        pointer = "household-history-cohort-" + self.configuration.version + ".json"
        try:
            if not self.store.documents.exists(pointer):
                return {}
            references = self.store.documents.read(pointer)
            if set(references) != set(self.configuration.entities):
                raise ValueError("Invalid household history cohort")
            return self.history_import.restore_checkpoint(references)
        except (OSError, ValueError, TypeError, KeyError):
            self.history_issues["household"] = "history_cache_unreadable"
            return {}

    def key(self, now):
        """Coalesce quarter boundaries and input-version changes without training on each sensor poll."""
        if self.configuration is None:
            return None
        quarter = now.replace(
            minute=(now.minute // FINE_INTERVAL_MINUTES) * FINE_INTERVAL_MINUTES,
            second=0,
            microsecond=0,
        )
        return (
            self.configuration.version,
            quarter,
            tuple(sorted(self.data_versions.items())),
            self.weather_version,
            self.learned_preferred,
            self.consumers.key(),
        )

    def request(self, now):
        """Discover all available years and resume one bounded source-bound transfer at a time."""
        if self.configuration is None or self.store.issue:
            return None
        for entity in self.configuration.entities:
            if entity in self.history_retry_after and now < self.history_retry_after[entity]:
                continue
            pending = self.history_import.request(entity, self.configuration.version, now)
            if pending is not None:
                return pending
        return None

    def receive(self, request, report, now):
        """Persist an admitted chunk and install only a complete current household source cohort."""
        pending = self.request(now)
        if pending is None or request != pending["version"] or report is None:
            raise ValueError("Demand history request changed")
        pending_histories = self.pending_histories.copy()
        complete = False
        entities = self.configuration.entities
        with self.history_import.transaction():
            accepted = self.history_import.receive(pending, report, now)
            entity = pending["entity_id"]
            if accepted is not None:
                pending_histories[entity] = accepted
            complete = all(entity in pending_histories for entity in entities) and all(
                self.history_import.request(entity, self.configuration.version, now) is None
                and entity not in self.history_import.issues
                for entity in entities
            )
            if complete:
                self.store.documents.write(
                    "household-history-cohort-" + self.configuration.version + ".json",
                    self.history_import.checkpoint(entities),
                )
        self.pending_histories = pending_histories
        self.history_issues.pop(entity, None)
        self.history_retry_after.pop(entity, None)
        if complete:
            self.histories = {entity: pending_histories[entity] for entity in entities}
            self.data_versions = {
                entity: digest(asdict(item)) for entity, item in self.histories.items()
            }
            self.history_quality = None

    def history_failed(self, pending, error, now):
        """Retain accepted evidence while a failed meter backs off and independent meters proceed."""
        if self.request(now) != pending:
            raise ValueError("Demand history request changed")
        self.history_issues[pending["entity_id"]] = error
        self.history_retry_after[pending["entity_id"]] = now + timedelta(
            minutes=HISTORY_RETRY_MINUTES
        )

    def weather(self, temperatures):
        """Install typed weather. Source receipt/version interpretation stays in the weather adapter."""
        self.temperatures = tuple(temperatures)
        self.weather_version = digest([asdict(item) for item in self.temperatures])

    def job(self, now):
        """Capture immutable worker inputs under the app lock, or skip an unchanged cached quarter."""
        key = self.key(now)
        if key is None or key == self.last_key:
            return None
        if key == self.failed_key and self.retry_after and now < self.retry_after:
            return None
        return (
            key,
            self.configuration,
            tuple(self.histories.values()),
            self.temperatures,
            now,
            self.learned_preferred,
            self.consumers.inputs(),
            self.candidate_features,
        )

    def failed(self, key, now):
        """Back off a current failed archive/training job. Never attach an old failure to new inputs."""
        if key != self.key(now):
            return False
        self.failed_key, self.retry_after = key, now + timedelta(minutes=MODEL_RETRY_MINUTES)
        self.issue = "demand_forecast_update_failed"
        return True

    def control_days(self, now):
        """Reuse committed publication history instead of maintaining a second intervention archive.

        Reads the current configuration timezone and selected consumers. The
        publication-history owner receives one bounded local-day query per
        lookback day on each build. This method does not write history.

        Args:
            now: Aware time used to identify completed local days.

        Returns:
            A mapping from controlled consumer entity IDs to lists of local
            YYYY-MM-DD dates. If no plan repository is configured, returns an
            empty mapping.

        Failure Behavior:
            OSError, ValueError, KeyError or TypeError during history lookup
            returns ['*'] for each currently selected consumer, or 'household'
            when none is selected. Daily selection then excludes all days and
            cannot promote a learned model from unavailable control evidence.
        """
        from zoneinfo import ZoneInfo

        from open_energy_planner.core.calendar import day_window
        from open_energy_planner.forecast_quality.daily import controlled_consumers
        from open_energy_planner.forecast_selection.calculation.accuracy import (
            MAX_ACCURACY_LOOKBACK_DAYS,
        )

        if self.plans is None:
            return {}
        result = {}
        today = now.astimezone(ZoneInfo(self.configuration.timezone)).date()
        try:
            for offset in range(1, MAX_ACCURACY_LOOKBACK_DAYS + 1):
                day = today - timedelta(days=offset)
                start, end = day_window(day.isoformat(), self.configuration.timezone)
                for entity in controlled_consumers(self.plans, start, end):
                    result.setdefault(entity, []).append(day.isoformat())
        except (OSError, ValueError, KeyError, TypeError):
            return {row["entity_id"]: ["*"] for row in self.consumers.selected} or {
                "household": ["*"]
            }
        return result

    def build(self, job):
        """Train and archive forecasts off-loop using independently retained daily accuracy.

        Measurement separation is active independently of learned-model admission.
        Each component retains its baseline until daily accuracy admits another
        model. Unavailable components remain inside the household remainder.
        Preserve original whole-house and component forecasts and parameters.

        Returns:
            Eleven values: key, forecast, candidate, status, issue, comparison,
            baseline forecast, feature family, consumer result, history quality
            and daily selection. The injected scorer remains a research seam.
        """
        (
            key,
            configuration,
            histories,
            temperatures,
            now,
            learned_preferred,
            consumers,
            previous_features,
        ) = job
        issue, comparison, daily_selection = None, None, None
        reconstruction = DemandReconstructor().analyze(configuration, histories, before=now)
        samples = reconstruction.samples
        baseline = WeekdayProfile.fit(configuration, samples)
        if reconstruction.notification_required:
            issue = "demand_boundary_inconsistent"
        selected, ridge, candidate = baseline, None, None
        status = "insufficient_training_history"
        known_weather = tuple(
            temperature for temperature in temperatures if temperature.known_at <= now
        )
        try:
            ridge = RidgeDemandModel.fit(configuration, samples, temperatures=known_weather)
            candidate = predict(configuration, samples, now, ridge, temperatures=known_weather)
            status = "shadow_waiting_for_decision_evidence"
            if learned_preferred:
                selected, status = ridge, "active"
            if self.scorer is not None:
                last = samples[-1].start + timedelta(hours=1)
                cutoffs = evaluation_cutoffs(last)
                comparison = compare(
                    configuration, samples, cutoffs, self.scorer, temperatures=temperatures
                )
                if (
                    comparison.improved
                    and comparison.temperature_enabled == ridge.temperature_enabled
                ):
                    selected, status = ridge, "active"
                else:
                    selected, status = baseline, "shadow_no_consistent_decision_benefit"
        except ValueError:
            if ridge is not None:
                status = "shadow_feature_or_evaluation_coverage_missing"
        baseline_forecast = predict(
            configuration, samples, now, baseline, temperatures=known_weather
        )
        from open_energy_planner.appliances.forecasting import build_components

        controlled_days = self.control_days(now)
        composite, consumer_result = build_components(
            configuration,
            histories,
            known_weather,
            now,
            consumers,
            documents=self.store.documents,
            controlled_days=controlled_days,
        )
        consumer_features = "consumers:" + consumers[0]
        weather_models = tuple(
            (record["entity_id"], record["forecast"]["model"])
            for record in (consumer_result or {}).get("devices", ())
            if record.get("weather_sensitive")
        )
        if any(model_id == "xgboost-weather-v1" for _, model_id in weather_models):
            consumer_features += ":weather:" + digest(weather_models)
        if consumer_features != previous_features and (
            ":weather:" in consumer_features or ":weather:" in previous_features
        ):
            # Another algorithm family needs its own financial shadow evidence.
            learned_preferred = False
        if consumers[1]:
            candidate = composite
            # Selection validates the complete composition against the incumbent,
            # not each component's in-sample error in isolation.
            status = consumer_result["status"]
            # Separation is an accounting boundary, not a reward for ML. Each
            # component independently retains its baseline until daily proof.
            forecast = composite or baseline_forecast
        else:
            if self.scorer is None and baseline.residual_profile:
                from open_energy_planner.appliances.forecasting import component

                excluded_dates = sorted({day for days in controlled_days.values() for day in days})
                selected, forecast, daily_selection = component(
                    configuration,
                    samples,
                    known_weather,
                    now,
                    True,
                    documents=self.store.documents,
                    excluded_dates=excluded_dates,
                )
                status = daily_selection["status"]
            forecast = predict(configuration, samples, now, selected, temperatures=known_weather)
        self.store.archive(baseline_forecast, baseline)
        if not consumers[1]:
            self.store.archive(forecast, selected)
        if candidate is not None and not consumers[1]:
            self.store.archive(candidate, ridge)
        candidates_path = "quality-candidates-" + forecast.version + ".json"
        if not self.store.documents.exists(candidates_path):
            self.store.documents.write(
                candidates_path,
                {
                    "forecasts": [
                        {"version": alternative.version, "model": alternative.model}
                        for alternative in (baseline_forecast, candidate)
                        if alternative is not None
                        and not (alternative is candidate and consumers[1])
                    ]
                },
            )
        if composite is not None:
            self.store.documents.write(
                "consumer-forecast-" + composite.version + ".json",
                {"forecast": forecast_document(composite), "components": consumer_result},
            )
        if composite is not None or consumer_result and consumer_result.get("devices"):
            self.store.archive_components(forecast, composite, consumer_result)
            self.store.archive_components(baseline_forecast, composite, consumer_result)
        return (
            key,
            forecast,
            candidate,
            status,
            issue,
            comparison,
            baseline_forecast,
            consumer_features
            if consumers[1]
            else "temperature"
            if ridge and ridge.temperature_enabled
            else "time",
            consumer_result,
            reconstruction.document(),
            daily_selection,
        )

    def install(self, result, now):
        """Reject a worker result superseded by configuration, history, weather or a quarter boundary."""
        (
            key,
            forecast,
            candidate,
            status,
            issue,
            comparison,
            baseline_forecast,
            features,
            consumer_result,
            history_quality,
            daily_selection,
        ) = result
        if key != self.key(now):
            return False
        self.last_key, self.forecast, self.candidate = key, forecast, candidate
        self.failed_key, self.retry_after = None, None
        self.model_status, self.issue = status, issue
        self.comparison = comparison
        self.daily_selection = daily_selection
        self.baseline = baseline_forecast
        if features != self.candidate_features and (
            ":weather:" in features or ":weather:" in self.candidate_features
        ):
            self.learned_preferred = False
        self.candidate_features = features
        self.consumers.result = consumer_result
        quality_changed = self.history_quality != history_quality
        self.history_quality = history_quality
        if history_quality["usable_hours"] > 0:
            _LOGGER.log(
                logging.INFO if quality_changed else logging.DEBUG,
                "Household energy training history assessed: %d usable hours, %d excluded hours (%.1f%%). "
                "Excluded hours were omitted from training. Recorder data is unchanged.",
                history_quality["usable_hours"],
                history_quality["rejected_hours"],
                history_quality["rejected_percentage"],
            )
        return True

    def select_learned(self, enabled):
        """Apply a validated family preference on the next archived forecast worker result."""
        if self.daily_selection is not None or self.consumers.selected:
            self.learned_preferred = False
            return
        enabled = bool(enabled)
        if self.learned_preferred != enabled and self.baseline is not None:
            self.forecast = self.baseline
        self.learned_preferred = enabled

    def document(self, now):
        """Expose current model, quality and requested history without inventing missing measurements."""
        comparison = getattr(self, "comparison", None)
        return {
            "configuration": configuration_document(self.configuration),
            "forecast": forecast_document(self.forecast),
            "candidate": forecast_document(self.candidate),
            "ridge_status": self.model_status,
            "issue": self.issue or self.store.issue,
            "history_issues": self.history_issues.copy(),
            "history_quality": self.history_quality,
            "model_selection": self.daily_selection,
            "request": self.request(now),
            "decision_comparison": {
                **asdict(comparison),
                "windows": [stamp.isoformat() for stamp in comparison.windows],
                "improved": comparison.improved,
            }
            if comparison
            else None,
        }
