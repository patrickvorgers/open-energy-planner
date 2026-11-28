"""Replay original forecast runs without pretending historical API availability was recorded.

Origins are deterministic simulation times. Fits use earlier measured outcomes
only. Native model creation and provider receipt clocks remain actual clocks.
Raw model error and the proposed ML service with physical fallback are separate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from itertools import pairwise
from math import fsum, isfinite
from typing import TYPE_CHECKING

from open_energy_planner.history.model import EnergyHistory, EnergyHour
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.planning_policy import PLANNING_HORIZON_HOURS
from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import (
    SolarConfiguration,
    SolarForecast,
    SolarInverter,
    SolarSource,
    version,
)
from open_energy_planner.solar.curtailment import CurtailmentHistory, archive_feedback
from open_energy_planner.solar.learning import (
    ac_comparison_issue,
    learning_interval,
    production_hours,
    unrestricted_interval,
)
from open_energy_planner.solar.model_policy import MIN_ABSOLUTE_GAIN_KWH, MIN_RELATIVE_GAIN
from open_energy_planner.weather.run import (
    FORECAST_WEATHER_MODEL,
    FORECAST_WEATHER_POLICY,
    ForecastWeatherRun,
)

if TYPE_CHECKING:
    from open_energy_planner.solar.boosting import TrainingResult
    from open_energy_planner.solar.storage import SolarStore

BACKTEST_POLICY = "four-weekly-frozen-ifs-dual-origin-48h-ac-clipping-v2"
BACKTEST_RUN_COUNT = 4
BACKTEST_SPACING_DAYS = 7
BACKTEST_RUN_HOUR = 12
BACKTEST_SETTLEMENT_LAG_HOURS = 24
SIMULATION_DELAYS_HOURS = (0, 24)
HOUR = timedelta(hours=1)
MAX_CACHED_PHYSICAL_RUNS = 64


class BacktestStatus(StrEnum):
    """Distinguish missing evidence from a completed unsuccessful forecast comparison."""

    ELIGIBLE = "eligible"
    INSUFFICIENT_HISTORY = "insufficient_history"
    NOT_IMPROVED = "not_improved"


@lru_cache(maxsize=MAX_CACHED_PHYSICAL_RUNS)
def physical_run(configuration, location, run):
    """Reuse physical hourly comparison values for one immutable forecast run and installation."""
    from open_energy_planner.solar.calculation.predictor import PVWattsPredictor

    return PVWattsPredictor().hourly(configuration, location, run.hours)


def validation_baselines(configuration, location, runs, before):
    """Compute physics only for runs touching the fitter's held-out week, never for every training year."""
    from open_energy_planner.solar.boosting import MAX_TRAINING_FORECAST_LEAD_HOURS, VALIDATION_DAYS

    end = before.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=VALIDATION_DAYS)
    return {
        run.version: physical_run(configuration, location, run)
        for run in runs
        if run.initialized_at < end
        and run.initialized_at + timedelta(hours=MAX_TRAINING_FORECAST_LEAD_HOURS) > start
    }


def scheduled_runs(now):
    """Choose four weekly run initializations before inspecting availability or measured outcomes."""
    latest = aware(now).astimezone(UTC) - timedelta(
        hours=max(SIMULATION_DELAYS_HOURS) + PLANNING_HORIZON_HOURS + BACKTEST_SETTLEMENT_LAG_HOURS
    )
    anchor = latest.replace(hour=BACKTEST_RUN_HOUR, minute=0, second=0, microsecond=0)
    if anchor > latest:
        anchor -= timedelta(days=1)
    return tuple(
        anchor - timedelta(days=index * BACKTEST_SPACING_DAYS)
        for index in reversed(range(BACKTEST_RUN_COUNT))
    )


def evidence_family(configuration, source, location, model_policy):
    """Bind historical admission to the physical regime, authorization, site and exact learning policy."""

    def physical_source(value):
        """Keep causal source settings while ignoring requested forecast preference and display names."""
        definition = asdict(value)
        definition.pop("forecast_source")
        for group in (definition, *definition["ha_imports"]):
            for panel in group["panels"]:
                panel.pop("name", None)
        return definition

    inverter = next(row for row in configuration.inverters if row.inverter_id == source.inverter_id)
    return version(
        {
            "source": physical_source(source),
            "shared_sources": [
                physical_source(other)
                for other in sorted(configuration.sources, key=lambda value: value.source_id)
                if other.inverter_id == source.inverter_id and other.source_id != source.source_id
            ],
            "inverter": asdict(inverter),
            "location": asdict(location),
            "model_policy": model_policy,
            "weather_policy": FORECAST_WEATHER_POLICY,
            "backtest_policy": BACKTEST_POLICY,
            "curtailment_policy": "observed-feedback-v1",
        }
    )


@dataclass(frozen=True)
class BacktestWindow:
    """One full simulated planning horizon, retaining actual model and training identities."""

    initialized_at: datetime
    origin: datetime
    end: datetime
    origin_delay_hours: int
    matched_hours: int
    baseline_mae_kwh: float
    model_mae_kwh: float
    service_mae_kwh: float
    training_end: datetime
    model_created_at: datetime
    training_reference: str
    run_reference: str


@dataclass(frozen=True)
class OriginScore:
    """Matched forecast errors for one simulation delay, without double-counting physical evidence."""

    origin_delay_hours: int
    matched_hours: int
    baseline_mae_kwh: float
    model_mae_kwh: float
    service_mae_kwh: float


@dataclass(frozen=True)
class HistoricalForecastAssessment:
    """Derive admission from complete matched windows rather than trusting a persisted eligibility flag."""

    source_family: str
    model_policy: str
    run_initializations: tuple[datetime, ...]
    windows: tuple[BacktestWindow, ...]
    created_at: datetime
    issue: str | None = None
    policy: str = BACKTEST_POLICY
    weather_model: str = FORECAST_WEATHER_MODEL
    weather_policy: str = FORECAST_WEATHER_POLICY
    status: BacktestStatus = field(init=False)
    per_origin: tuple[OriginScore, ...] = field(init=False)
    matched_pairs: int = field(init=False)
    unique_hours: int = field(init=False)

    def __post_init__(self):
        """Validate exact periods, clocks and finite errors before deriving the automatic decision."""
        created = aware(self.created_at)
        if (
            self.policy != BACKTEST_POLICY
            or self.weather_policy != FORECAST_WEATHER_POLICY
            or self.weather_model != FORECAST_WEATHER_MODEL
        ):
            raise ValueError("Unsupported historical solar forecast policy")
        for digest in (self.source_family, self.model_policy):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("Invalid historical solar forecast reference")
        if (
            len(self.run_initializations) != BACKTEST_RUN_COUNT
            or tuple(sorted(set(self.run_initializations))) != self.run_initializations
        ):
            raise ValueError("Invalid historical solar forecast origins")
        expected = {
            (instant, delay)
            for instant in self.run_initializations
            for delay in SIMULATION_DELAYS_HOURS
        }
        found, actual_hours = set(), set()
        for window in self.windows:
            key = window.initialized_at, window.origin_delay_hours
            if (
                key not in expected
                or key in found
                or window.origin
                != window.initialized_at + timedelta(hours=window.origin_delay_hours)
                or window.end != window.origin + timedelta(hours=PLANNING_HORIZON_HOURS)
                or window.matched_hours != PLANNING_HORIZON_HOURS
                or not aware(window.training_end) <= aware(window.origin)
                or not aware(window.training_end) <= aware(window.model_created_at) <= created
                or aware(window.end) > created
            ):
                raise ValueError("Invalid historical solar forecast window")
            for value in (window.baseline_mae_kwh, window.model_mae_kwh, window.service_mae_kwh):
                if type(value) not in (int, float) or not isfinite(value) or value < 0:
                    raise ValueError("Invalid historical solar forecast score")
            found.add(key)
            actual_hours.update(
                window.origin + index * HOUR for index in range(PLANNING_HORIZON_HOURS)
            )
        scores = []
        for delay in SIMULATION_DELAYS_HOURS:
            windows = [window for window in self.windows if window.origin_delay_hours == delay]
            if windows:
                scores.append(
                    OriginScore(
                        delay,
                        sum(window.matched_hours for window in windows),
                        *(
                            fsum(getattr(window, name) for window in windows) / len(windows)
                            for name in ("baseline_mae_kwh", "model_mae_kwh", "service_mae_kwh")
                        ),
                    )
                )
        eligible = (
            found == expected
            and self.issue is None
            and all(
                score.baseline_mae_kwh - error
                >= max(MIN_ABSOLUTE_GAIN_KWH, score.baseline_mae_kwh * MIN_RELATIVE_GAIN)
                for score in scores
                for error in (score.model_mae_kwh, score.service_mae_kwh)
            )
        )
        status = (
            BacktestStatus.INSUFFICIENT_HISTORY
            if found != expected or self.issue
            else BacktestStatus.ELIGIBLE
            if eligible
            else BacktestStatus.NOT_IMPROVED
        )
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "per_origin", tuple(scores))
        object.__setattr__(
            self, "matched_pairs", sum(window.matched_hours for window in self.windows)
        )
        object.__setattr__(self, "unique_hours", len(actual_hours))

    @classmethod
    def restore(cls, body):
        """Decode stored clocks and rederive scores/counts/status from the original window evidence."""
        from open_energy_planner.solar.wire import document

        try:
            arguments = {
                key: value
                for key, value in body.items()
                if key not in ("status", "per_origin", "matched_pairs", "unique_hours")
            }
            arguments["created_at"] = datetime.fromisoformat(arguments["created_at"])
            arguments["run_initializations"] = tuple(
                datetime.fromisoformat(value) for value in arguments["run_initializations"]
            )
            arguments["windows"] = tuple(
                BacktestWindow(
                    **{
                        **window,
                        **{
                            name: datetime.fromisoformat(window[name])
                            for name in (
                                "initialized_at",
                                "origin",
                                "end",
                                "training_end",
                                "model_created_at",
                            )
                        },
                    }
                )
                for window in arguments["windows"]
            )
            result = cls(**arguments)
            if document(result) != body:
                raise ValueError("Historical solar forecast summary changed")
            return result
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("Invalid historical solar forecast evidence") from None


@dataclass(frozen=True)
class _ReplaySite:
    """Keep a source and its shared-inverter evidence together for a historical replay."""

    configuration: SolarConfiguration
    source: SolarSource
    inverter: SolarInverter
    siblings: tuple[SolarSource, ...]
    location: SiteLocation
    histories: Mapping[str, EnergyHistory]
    feedback: Mapping[str, CurtailmentHistory | None]


@dataclass(frozen=True)
class _ReplayEvidence:
    """Retain original fit data and the bounded evidence used for cache identity."""

    history: EnergyHistory
    bounded_history: tuple[EnergyHour, ...]
    runs: tuple[ForecastWeatherRun, ...]
    initializations: tuple[datetime, ...]
    identity: str


def _bounded_feedback(site: _ReplaySite, last_end: datetime) -> dict:
    """Hash feedback only for the shared inverter and strictly before the final test boundary."""
    feedback_entities = {source.curtailment_entity_id for source in site.siblings}
    return {
        entity: None
        if value is None
        else {
            "checked_until": min(value.checked_until, last_end).isoformat(),
            "readings": [[at.isoformat(), state] for at, state in value.readings if at < last_end],
        }
        for entity, value in site.feedback.items()
        if entity in feedback_entities
    }


def _bounded_hours(history: EnergyHistory, last_end: datetime) -> tuple[EnergyHour, ...]:
    """Retain the final meter boundary needed to calculate the last hourly delta."""
    return tuple(hour for hour in history.hours if hour.start <= last_end)


def _assessment_inputs(
    site: _ReplaySite,
    family: str,
    initializations: tuple[datetime, ...],
    history: EnergyHistory,
    runs: tuple[ForecastWeatherRun, ...],
) -> _ReplayEvidence:
    """Bound original field evidence to the last replay window and derive its archive identity."""
    # Outcomes after the final test horizon and newer runs cannot change these fits.
    last_end = initializations[-1] + timedelta(
        hours=max(SIMULATION_DELAYS_HOURS) + PLANNING_HORIZON_HOURS
    )
    bounded_history = _bounded_hours(history, last_end)
    relevant_runs = tuple(run for run in runs if run.initialized_at <= initializations[-1])
    shared_histories = {}
    for other in site.siblings:
        if other.source_id == site.source.source_id or other.entity_id not in site.histories:
            continue
        shared_histories[other.entity_id] = _bounded_hours(
            site.histories[other.entity_id], last_end
        )
    identity = version(
        {
            "family": family,
            "origins": initializations,
            "history": bounded_history,
            "runs": [run.version for run in relevant_runs],
            "shared_histories": shared_histories,
            "curtailment_policy": "observed-feedback-v1",
            "feedback": _bounded_feedback(site, last_end),
        }
    )
    return _ReplayEvidence(history, bounded_history, relevant_runs, initializations, identity)


def _measured_production(source, inverter, bounded_history, now):
    """Keep complete, physically valid meter deltas from before the assessment clock."""
    from open_energy_planner.solar.boosting import AC_METER_HEADROOM, MAX_DC_CAPACITY_MULTIPLE

    max_power_kw = (
        inverter.ac_limit_w / 1000
        if source.measurement_boundary == "ac"
        else source.capacity_w / 1000 * MAX_DC_CAPACITY_MULTIPLE
    )
    actual = {}
    for previous, following in pairwise(bounded_history):
        value = following.total_kwh - previous.total_kwh
        if (
            following.start - previous.start == HOUR
            and 0 <= value <= max_power_kw * AC_METER_HEADROOM
            and following.start + HOUR <= now
        ):
            actual[following.start] = value
    return actual


def _archive_historical_fit(store, result, origin, runs, site):
    """Archive a fit and the original inputs needed to explain its evaluation."""
    from open_energy_planner.solar.wire import document

    metadata = store.archive_model(result.challenger)
    training_record = {
        "model": metadata,
        "summary": document(result.summary),
        "cutoff": origin.isoformat(),
        "run_references": [row.version for row in runs],
        "curtailment_feedback_references": {
            entity: archive_feedback(store, value)
            for entity, value in site.feedback.items()
            if value is not None
            and any(other.curtailment_entity_id == entity for other in site.siblings)
        },
    }
    reference = version(training_record)
    store.archive("historical-fit", reference, training_record)
    return reference


def _restore_assessment(store, pointer, family, model_policy, initializations):
    """Admit only an immutable report with the exact replay context requested."""
    if not store.documents.exists(pointer):
        return None
    reference = store.documents.read(pointer)
    record = store.documents.read("historical-assessment-" + reference + ".json")
    if version(record) != reference:
        raise ValueError("Historical solar forecast checksum changed")
    report = HistoricalForecastAssessment.restore(record)
    if (report.source_family, report.model_policy, report.run_initializations) != (
        family,
        model_policy,
        initializations,
    ):
        raise ValueError("Historical solar forecast context changed")
    return report


def _hourly_values(
    forecast: SolarForecast, instants: tuple[datetime, ...], source_id: str, field_name: str
) -> dict[datetime, float]:
    """Integrate forecasts into matched hours and keep explicit zero-production hours."""
    values = dict.fromkeys(instants, 0.0)
    for energy in forecast.energies:
        instant = energy.start.replace(minute=0, second=0, microsecond=0)
        if instant in values and energy.source_id == source_id:
            values[instant] += getattr(energy, field_name)
    return values


@dataclass(frozen=True)
class _WindowReplay:
    """Replay clock, metered evidence and original fit inputs shared by all planning origins."""

    site: _ReplaySite
    evidence: _ReplayEvidence
    now: datetime
    actual: Mapping[datetime, float]
    meter_hours: Mapping[str, Mapping[datetime, float]]

    def usable_hour(self, instant: datetime) -> bool:
        """Require a valid source measurement, shared AC evidence and unrestricted learning."""
        if instant not in self.actual:
            return False
        site = self.site
        ac_measurements = tuple(
            self.meter_hours.get(other.entity_id, {}).get(instant)
            if other.measurement_boundary == "ac"
            else None
            for other in site.siblings
        )
        if ac_comparison_issue(
            site.source.measurement_boundary, site.inverter.ac_limit_w, ac_measurements
        ):
            return False
        end = instant + HOUR
        if not learning_interval(site.source, instant, end):
            return False
        return unrestricted_interval(
            site.source, instant, end, site.feedback.get(site.source.curtailment_entity_id)
        )

    def fit(self, source, history, origin, physical) -> TrainingResult:
        """Use the simulated origin as the fit cutoff and the real replay time as known-at."""
        from open_energy_planner.solar.boosting import fit_boosted_model

        return fit_boosted_model(
            source,
            self.site.inverter,
            self.site.location,
            history,
            (),
            (),
            origin,
            forecast_runs=self.evidence.runs,
            forecast_physical=physical,
            known_at=self.now,
            feedback=self.site.feedback.get(source.curtailment_entity_id),
        )

    def fitted_siblings(self, origin, physical):
        """Use a sibling's accepted model in both comparisons only when its history is present."""
        models = {}
        for other in self.site.siblings:
            if (
                other.source_id == self.site.source.source_id
                or other.entity_id not in self.site.histories
            ):
                continue
            result = self.fit(other, self.site.histories[other.entity_id], origin, physical)
            if result.model:
                models[other.source_id] = result.model
        return models

    def errors(self, run, origin, instants, result, physical) -> tuple[float, ...]:
        """Score physics, the raw challenger and the served forecast on the same metered hours."""
        from open_energy_planner.solar.calculation.projection import learned_forecast

        site = self.site
        source_id = site.source.source_id
        sibling_models = self.fitted_siblings(origin, physical)
        raw_models = {source_id: result.challenger, **sibling_models}
        raw = learned_forecast(
            site.configuration,
            site.location,
            run.hours,
            self.now,
            raw_models,
            planning_origin=origin,
        )
        served = None
        if result.model:
            served_models = {source_id: result.model, **sibling_models}
            served = learned_forecast(
                site.configuration,
                site.location,
                run.hours,
                self.now,
                served_models,
                planning_origin=origin,
            )
        field_name = "ac_kwh" if site.source.measurement_boundary == "ac" else "dc_kwh"
        baseline = {
            row.start: getattr(row, field_name)
            for row in physical_run(site.configuration, site.location, run)
            if row.source_id == source_id
        }
        raw_values = _hourly_values(raw, instants, source_id, field_name)
        service_values = (
            _hourly_values(served, instants, source_id, field_name) if served else baseline
        )
        return tuple(
            fsum(abs(values[instant] - self.actual[instant]) for instant in instants)
            / len(instants)
            for values in (baseline, raw_values, service_values)
        )

    def assess_window(
        self, run, delay, model_policy, store
    ) -> tuple[BacktestWindow | None, str | None]:
        """Require a complete window before fitting, scoring, and archiving its causal inputs."""
        initialized = run.initialized_at
        origin = initialized + timedelta(hours=delay)
        instants = tuple(origin + index * HOUR for index in range(PLANNING_HORIZON_HOURS))
        if not all(self.usable_hour(instant) for instant in instants):
            return (
                None,
                "The predetermined historical forecast windows need complete usable production measurements within the selected learning period.",
            )
        site = self.site
        physical = validation_baselines(
            site.configuration, site.location, self.evidence.runs, origin
        )
        result = self.fit(site.source, self.evidence.history, origin, physical)
        if result.challenger is None:
            return (
                None,
                "Earlier original forecasts and measurements are insufficient to train the historical models.",
            )
        if result.challenger.policy_version != model_policy:
            raise ValueError("Historical solar learning policy changed")
        errors = self.errors(run, origin, instants, result, physical)
        reference = _archive_historical_fit(store, result, origin, self.evidence.runs, site)
        return BacktestWindow(
            initialized,
            origin,
            origin + PLANNING_HORIZON_HOURS * HOUR,
            delay,
            len(instants),
            *errors,
            result.challenger.trained_until,
            result.challenger.created_at,
            reference,
            run.version,
        ), None

    def assess_windows(self, model_policy, store) -> tuple[tuple[BacktestWindow, ...], str | None]:
        """Visit fixed runs and origins chronologically and retain successes before a gap."""
        run_by_init = {run.initialized_at: run for run in self.evidence.runs}
        windows = []
        for initialized in self.evidence.initializations:
            run = run_by_init.get(initialized)
            if run is None:
                return (
                    tuple(windows),
                    "Original forecast runs are still being imported. Historical assessment continues when the required forecasts are available.",
                )
            for delay in SIMULATION_DELAYS_HOURS:
                window, issue = self.assess_window(run, delay, model_policy, store)
                if window is None:
                    return tuple(windows), issue
                windows.append(window)
        return tuple(windows), None


def _window_replay(site, evidence, now) -> _WindowReplay:
    """Capture comparable meter hours once, before scoring each fixed replay window."""
    actual = _measured_production(site.source, site.inverter, evidence.bounded_history, now)
    meter_hours = {
        other.entity_id: production_hours(
            evidence.history
            if other.source_id == site.source.source_id
            else site.histories.get(other.entity_id),
            now,
        )
        for other in site.siblings
    }
    return _WindowReplay(site, evidence, now, actual, meter_hours)


def assess_forecasts(
    configuration: SolarConfiguration,
    source: SolarSource,
    location: SiteLocation,
    history: EnergyHistory,
    runs: tuple[ForecastWeatherRun, ...],
    now: datetime,
    model_policy: str,
    store: SolarStore,
    *,
    histories: Mapping[str, EnergyHistory] | None = None,
    feedback: Mapping[str, CurtailmentHistory | None] | None = None,
) -> HistoricalForecastAssessment:
    """Compare four predetermined runs at both origins, retaining original fits and actual clocks."""
    from open_energy_planner.solar.wire import document

    initializations = scheduled_runs(now)
    family = evidence_family(configuration, source, location, model_policy)
    site = _ReplaySite(
        configuration,
        source,
        next(row for row in configuration.inverters if row.inverter_id == source.inverter_id),
        tuple(other for other in configuration.sources if other.inverter_id == source.inverter_id),
        location,
        histories or {},
        feedback or {},
    )
    evidence = _assessment_inputs(site, family, initializations, history, runs)
    pointer = "historical-assessment-current-" + evidence.identity + ".json"
    report = _restore_assessment(store, pointer, family, model_policy, initializations)
    if report is not None:
        return report
    windows, issue = _window_replay(site, evidence, now).assess_windows(model_policy, store)
    report = HistoricalForecastAssessment(
        family, model_policy, initializations, windows, now, issue
    )
    body = document(report)
    reference = version(body)
    with store.transaction():
        store.archive("historical-assessment", reference, body)
        store.documents.write(pointer, reference)
    return report
