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

from ..planning_policy import FINE_INTERVAL_MINUTES
from .evaluation import compare, evaluation_cutoffs
from .reconstruction import DemandReconstructor
from .wire import configuration_document, forecast_document

HISTORY_LOOKBACK_DAYS = 365
MODEL_RETRY_MINUTES = 5
_LOGGER = logging.getLogger(__name__)


class DemandRuntime:
    """Own configured demand inputs and cached predictions without fetching HA or weather itself."""

    def __init__(self, store, *, scorer=None):
        """Restore configuration. Never restore stale predictions as live evidence after restart."""
        self.store, self.scorer = store, scorer
        self.configuration = store.current
        self.histories, self.ranges, self.data_versions = {}, {}, {}
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
        from open_energy_planner.appliances.forecasting import ConsumerForecasts

        self.consumers = ConsumerForecasts(store.documents)

    def configure(self, configuration):
        """Invalidate incompatible observations/models after a persisted measurement-definition change."""
        self.configuration = configuration
        self.histories, self.ranges, self.data_versions = {}, {}, {}
        self.pending_histories = {}
        self.forecast = self.candidate = self.baseline = self.last_key = None
        self.learned_preferred = False
        self.candidate_features = "time"
        self.failed_key, self.retry_after = None, None
        self.history_issues = {}
        self.history_quality = None
        self.comparison = None
        self.issue = None
        self.model_status = "waiting_for_refresh"

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
        """Request one missing daily history channel with a bounded annual UTC range."""
        if self.configuration is None or self.store.issue:
            return None
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(days=HISTORY_LOOKBACK_DAYS, hours=1)
        for entity in self.configuration.entities:
            if self.ranges.get(entity) != (start, end):
                document = {
                    "entity_id": entity,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "configuration_version": self.configuration.version,
                }
                return {**document, "version": digest(document)}
        return None

    def receive(self, request, history, now):
        """Install a normalized response only for the current channel/range/configuration request."""
        pending = self.request(now)
        if (
            pending is None
            or request != pending["version"]
            or history.entity_id != pending["entity_id"]
        ):
            raise ValueError("Demand history request changed")
        from datetime import datetime

        start, end = (datetime.fromisoformat(pending[key]) for key in ("start", "end"))
        if any(not start <= hour.start < end for hour in history.hours):
            raise ValueError("Demand history outside requested range")
        self.pending_histories[history.entity_id] = history
        self.ranges[history.entity_id] = start, end
        if all(self.ranges.get(entity) == (start, end) for entity in self.configuration.entities):
            # Install one complete source cohort, not a mix of yesterday's and
            # today's channels while Recorder responses arrive sequentially.
            self.histories = {
                entity: self.pending_histories[entity] for entity in self.configuration.entities
            }
            self.data_versions = {
                entity: digest(asdict(item)) for entity, item in self.histories.items()
            }
            self.history_quality = None
        self.history_issues.pop(history.entity_id, None)

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
        )

    def failed(self, key, now):
        """Back off a current failed archive/training job. Never attach an old failure to new inputs."""
        if key != self.key(now):
            return False
        self.failed_key, self.retry_after = key, now + timedelta(minutes=MODEL_RETRY_MINUTES)
        self.issue = "demand_forecast_update_failed"
        return True

    def build(self, job):
        """Train and archive forecasts off-loop, retaining the incumbent until decision validation.

        With consumers selected, a complete composite is the challenger. Publish
        it only with learned_preferred, otherwise retain the household baseline.
        Always archive that baseline. Ordinary forecasts retain their model
        archive. Composites retain their exact component/parameter document.

        Returns:
            Ten values: key, forecast, candidate, status, issue, comparison,
            baseline_forecast, feature family, consumer_result and history quality. A validated
            composite reports active. Missing evidence retains a readiness status.
        """
        key, configuration, histories, temperatures, now, learned_preferred, consumers = job
        issue, comparison = None, None
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

        composite, consumer_result = build_components(
            configuration, histories, known_weather, now, consumers
        )
        if consumers[1]:
            candidate = composite
            # Selection validates the complete composition against the incumbent,
            # not each component's in-sample error in isolation.
            status = consumer_result["status"]
            forecast = composite if learned_preferred and composite else baseline_forecast
            if composite and learned_preferred:
                status = "active"
        else:
            forecast = predict(configuration, samples, now, selected, temperatures=known_weather)
        self.store.archive(baseline_forecast, baseline)
        if not consumers[1]:
            self.store.archive(forecast, selected)
        if candidate is not None and not consumers[1]:
            self.store.archive(candidate, ridge)
        if composite is not None:
            self.store.documents.write(
                "consumer-forecast-" + composite.version + ".json",
                {"forecast": forecast_document(composite), "components": consumer_result},
            )
        if composite is not None or consumer_result and consumer_result.get("devices"):
            self.store.archive_components(forecast, composite, consumer_result)
            self.store.archive_components(baseline_forecast, composite, consumer_result)
        if composite is not None and learned_preferred:
            consumer_result = {
                **consumer_result,
                "status": status,
                "devices": [{**record, "status": status} for record in consumer_result["devices"]],
            }
        return (
            key,
            forecast,
            candidate,
            status,
            issue,
            comparison,
            baseline_forecast,
            "consumers:" + consumers[0]
            if consumers[1]
            else "temperature"
            if ridge and ridge.temperature_enabled
            else "time",
            consumer_result,
            reconstruction.document(),
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
        ) = result
        if key != self.key(now):
            return False
        self.last_key, self.forecast, self.candidate = key, forecast, candidate
        self.failed_key, self.retry_after = None, None
        self.model_status, self.issue = status, issue
        self.comparison = comparison
        self.baseline = baseline_forecast
        self.candidate_features = features
        self.consumers.result = consumer_result
        quality_changed = self.history_quality != history_quality
        self.history_quality = history_quality
        if history_quality["rejected_percentage"] is not None:
            _LOGGER.log(
                logging.INFO if quality_changed else logging.DEBUG,
                "Household energy training accepted: %d usable hours, %d excluded hours (%.1f%%). "
                "Excluded hours were omitted from training. Recorder data is unchanged.",
                history_quality["usable_hours"],
                history_quality["rejected_hours"],
                history_quality["rejected_percentage"],
            )
        return True

    def select_learned(self, enabled):
        """Apply a validated family preference on the next archived forecast worker result."""
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
            "request": self.request(now),
            "decision_comparison": {
                **asdict(comparison),
                "windows": [stamp.isoformat() for stamp in comparison.windows],
                "improved": comparison.improved,
            }
            if comparison
            else None,
        }
