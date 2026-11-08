"""Replay original forecast runs without pretending historical API availability was recorded.

Origins are deterministic simulation times. Fits use earlier measured outcomes
only. Native model creation and provider receipt clocks remain actual clocks.
Raw model error and the proposed ML service with physical fallback are separate.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from itertools import pairwise
from math import fsum, isfinite

from open_energy_planner.planning_policy import PLANNING_HORIZON_HOURS
from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.forecast_weather import (
    FORECAST_WEATHER_MODEL,
    FORECAST_WEATHER_POLICY,
)
from open_energy_planner.solar.learning import learning_interval, unrestricted_interval
from open_energy_planner.solar.model_policy import MIN_ABSOLUTE_GAIN_KWH, MIN_RELATIVE_GAIN

BACKTEST_POLICY = "four-weekly-frozen-ifs-dual-origin-48h-v1"
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


def assess_forecasts(
    configuration, source, location, history, runs, now, model_policy, store, *, histories=None
):
    """Compare four predetermined runs at both origins, retaining original fits and actual clocks."""
    from open_energy_planner.solar.boosting import (
        AC_METER_HEADROOM,
        MAX_DC_CAPACITY_MULTIPLE,
        fit_boosted_model,
    )
    from open_energy_planner.solar.calculation.projection import learned_forecast
    from open_energy_planner.solar.wire import document

    initializations = scheduled_runs(now)
    family = evidence_family(configuration, source, location, model_policy)
    # Outcomes after the final test horizon and newer runs cannot change these fits.
    last_end = initializations[-1] + timedelta(
        hours=max(SIMULATION_DELAYS_HOURS) + PLANNING_HORIZON_HOURS
    )
    bounded_history = tuple(hour for hour in history.hours if hour.start <= last_end)
    relevant_runs = tuple(run for run in runs if run.initialized_at <= initializations[-1])
    shared_histories = {
        other.entity_id: tuple(
            hour for hour in histories[other.entity_id].hours if hour.start <= last_end
        )
        for other in configuration.sources
        if other.source_id != source.source_id
        and other.inverter_id == source.inverter_id
        and other.entity_id in (histories or {})
    }
    identity = version(
        {
            "family": family,
            "origins": initializations,
            "history": bounded_history,
            "runs": [run.version for run in relevant_runs],
            "shared_histories": shared_histories,
        }
    )
    pointer = "historical-assessment-current-" + identity + ".json"
    if store.documents.exists(pointer):
        reference = store.documents.read(pointer)
        record = store.documents.read("historical-assessment-" + reference + ".json")
        if version(record) != reference:
            raise ValueError("Historical solar forecast checksum changed")
        report = HistoricalForecastAssessment.restore(record)
        if (
            report.source_family != family
            or report.model_policy != model_policy
            or report.run_initializations != initializations
        ):
            raise ValueError("Historical solar forecast context changed")
        return report
    inverter = next(row for row in configuration.inverters if row.inverter_id == source.inverter_id)
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
    run_by_init = {run.initialized_at: run for run in relevant_runs}
    windows, issue = [], None
    for initialized in initializations:
        run = run_by_init.get(initialized)
        if run is None:
            issue = "Original forecast runs are still being imported. No new three-day wait is needed once the historical comparison is complete."
            break
        for delay in SIMULATION_DELAYS_HOURS:
            origin = initialized + timedelta(hours=delay)
            instants = tuple(origin + index * HOUR for index in range(PLANNING_HORIZON_HOURS))
            if any(
                instant not in actual
                or not learning_interval(source, instant, instant + HOUR)
                or not unrestricted_interval(source, instant, instant + HOUR)
                for instant in instants
            ):
                issue = "The predetermined historical forecast windows need complete usable production measurements within the selected learning period."
                break
            physical = validation_baselines(configuration, location, relevant_runs, origin)
            result = fit_boosted_model(
                source,
                inverter,
                location,
                history,
                (),
                (),
                origin,
                forecast_runs=relevant_runs,
                forecast_physical=physical,
                known_at=now,
            )
            if result.challenger is None:
                issue = "Earlier original forecasts and measurements are insufficient to train the historical models."
                break
            if result.challenger.policy_version != model_policy:
                raise ValueError("Historical solar learning policy changed")
            raw_models = {source.source_id: result.challenger}
            served_models = {source.source_id: result.model} if result.model else {}
            for other in configuration.sources:
                if (
                    other.source_id == source.source_id
                    or other.inverter_id != source.inverter_id
                    or other.entity_id not in (histories or {})
                ):
                    continue
                other_inverter = next(
                    row for row in configuration.inverters if row.inverter_id == other.inverter_id
                )
                other_result = fit_boosted_model(
                    other,
                    other_inverter,
                    location,
                    histories[other.entity_id],
                    (),
                    (),
                    origin,
                    forecast_runs=relevant_runs,
                    forecast_physical=physical,
                    known_at=now,
                )
                if other_result.model:
                    raw_models[other.source_id] = other_result.model
                    served_models[other.source_id] = other_result.model
            raw = learned_forecast(
                configuration, location, run.hours, now, raw_models, planning_origin=origin
            )
            served = (
                learned_forecast(
                    configuration, location, run.hours, now, served_models, planning_origin=origin
                )
                if source.source_id in served_models
                else None
            )
            field_name = "ac_kwh" if source.measurement_boundary == "ac" else "dc_kwh"
            baseline = {
                row.start: getattr(row, field_name)
                for row in physical_run(configuration, location, run)
                if row.source_id == source.source_id
            }

            def hourly_values(forecast, instants=instants, field_name=field_name):
                """Integrate the actual mixed-resolution service projection into matched measured hours."""
                values = {instant: 0.0 for instant in instants}
                for energy in forecast.energies:
                    instant = energy.start.replace(minute=0, second=0, microsecond=0)
                    if instant in values and energy.source_id == source.source_id:
                        values[instant] += getattr(energy, field_name)
                return values

            raw_values = hourly_values(raw)
            service_values = hourly_values(served) if served else baseline
            errors = [
                fsum(abs(values[instant] - actual[instant]) for instant in instants) / len(instants)
                for values in (baseline, raw_values, service_values)
            ]
            metadata = store.archive_model(result.challenger)
            training_record = {
                "model": metadata,
                "summary": document(result.summary),
                "cutoff": origin.isoformat(),
                "run_references": [row.version for row in relevant_runs],
            }
            reference = version(training_record)
            store.archive("historical-fit", reference, training_record)
            windows.append(
                BacktestWindow(
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
                )
            )
        if issue:
            break
    report = HistoricalForecastAssessment(
        family, model_policy, initializations, tuple(windows), now, issue
    )
    body = document(report)
    reference = version(body)
    with store.transaction():
        store.archive("historical-assessment", reference, body)
        store.documents.write(pointer, reference)
    return report
