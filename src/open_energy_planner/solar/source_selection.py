"""Select solar forecasts using frozen matched daily accuracy, independently of prices."""

from dataclasses import asdict
from datetime import datetime, timedelta
from itertools import pairwise
from math import fsum
from zoneinfo import ZoneInfo

from open_energy_planner.core.calendar import day_window
from open_energy_planner.forecast_quality.calculation.metrics import integral
from open_energy_planner.forecast_selection.calculation.accuracy import (
    MAX_ACCURACY_LOOKBACK_DAYS,
    MIN_ACCURACY_DAYS,
    comparison_hours,
    mean_absolute_error,
)
from open_energy_planner.prices.model import aware
from open_energy_planner.solar.backtest import (
    BacktestStatus,
    HistoricalForecastAssessment,
    evidence_family,
    scheduled_runs,
)
from open_energy_planner.solar.calculation.model import (
    MAX_SOLAR_ENERGY_KWH,
    ForecastSource,
    SolarEnergy,
    horizon,
    identity,
    number,
    version,
)
from open_energy_planner.solar.forecast_weather import FORECAST_WEATHER_POLICY
from open_energy_planner.solar.learning import learning_interval, unrestricted_interval
from open_energy_planner.solar.model_policy import MIN_ABSOLUTE_GAIN_KWH, MIN_RELATIVE_GAIN
from open_energy_planner.solar.wire import document

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
SOURCE_POLICY = "matched-local-day-mae-v1"


def eligibility(configuration, source):
    """Exclude unknown available-production boundaries without inventing DC from hybrid AC metering."""
    inverter = next(row for row in configuration.inverters if row.inverter_id == source.inverter_id)
    if source.can_curtail and source.unrestricted_history_start is None:
        return "Production affected by curtailment cannot establish forecast accuracy"
    if inverter.hybrid:
        return (
            "Hybrid solar needs independent route evidence before using an HA production forecast"
        )
    if sum(row.inverter_id == source.inverter_id for row in configuration.sources) > 1:
        return "Shared-inverter sources need independent production evidence"
    return None


def ha_energies(configuration, baseline, predictions, now):
    """Project complete fresh AC groups using the declared standalone conversion exactly once.

    Dividing metered AC by declared efficiency is a standalone-equivalent DC
    input. It does not claim to recover clipped panel DC or hybrid battery routes.
    Missing provider coverage remains absent. No provider extends another's tail.
    """
    result, issues = {}, {}
    inverters = {row.inverter_id: row for row in configuration.inverters}
    for source in configuration.sources:
        providers = predictions.get(source.entity_id, ())
        issue = eligibility(configuration, source)
        if source.measurement_boundary != "ac":
            issue = "HA production forecasts need an AC production meter"
        if not providers:
            issue = "Waiting for a supported Home Assistant solar forecast"
        elif not all(provider.fresh(now) for provider in providers):
            issue = "Waiting for an observed successful Home Assistant solar forecast refresh"
        if issue:
            issues[source.source_id] = issue
            continue
        steps = [
            [{**asdict(step), "start": step.start.isoformat()} for step in provider.steps]
            for provider in providers
        ]
        common_end = min(
            provider.steps[-1].start + timedelta(hours=provider.steps[-1].duration_h)
            for provider in providers
        )
        energies = []
        targets = (
            (
                (row.start, row.duration_h)
                for row in baseline.energies
                if row.source_id == source.source_id
            )
            if baseline
            else horizon(now)
        )
        for start, duration_h in targets:
            if start + timedelta(hours=duration_h) > common_end:
                break
            amounts = [integral(rows, start, start + timedelta(hours=duration_h)) for rows in steps]
            if any(value is None for value in amounts):
                issues[source.source_id] = "Home Assistant forecast coverage is incomplete"
                energies = []
                break
            ac_kwh = fsum(amounts)
            limit = inverters[source.inverter_id].ac_limit_w / 1000 * duration_h
            if ac_kwh > limit:
                issues[source.source_id] = "HA forecast exceeds the configured inverter rating"
                energies = []
                break
            energies.append(
                SolarEnergy(
                    source.source_id,
                    start,
                    duration_h,
                    ac_kwh / inverters[source.inverter_id].efficiency,
                    ac_kwh,
                )
            )
        if energies:
            result[source.source_id] = tuple(energies)
        elif source.source_id not in issues:
            issues[source.source_id] = "Home Assistant forecast coverage is incomplete"
    return result, issues


def family(configuration, source, location, *, learned_policy=None):
    """Scope accuracy to metering, geometry and location, independently of prices and strategy."""
    inverter = next(row for row in configuration.inverters if row.inverter_id == source.inverter_id)
    source_definition = asdict(source)
    source_definition.pop("forecast_source")
    for field in ("unrestricted_history_start", "unrestricted_history_end"):
        source_definition.pop(field)
    if source_definition["learning_start"] is None:
        source_definition.pop("learning_start")

    for group in (source_definition, *source_definition["ha_imports"]):
        for panel in group["panels"]:
            panel.pop("name")
    return version(
        {
            "policy": SOURCE_POLICY,
            "source": source_definition,
            "inverter": asdict(inverter),
            "location": asdict(location),
            "weather_policy": FORECAST_WEATHER_POLICY,
            **({"learned_policy": learned_policy} if learned_policy else {}),
        }
    )


def _read_comparison(documents, path, source_id, day, timezone, hours):
    """Validate retained optional evidence before it can select a source or affect baseline availability."""
    if not documents.exists(path):
        return None
    record = documents.read(path)
    if (
        not isinstance(record, dict)
        or record["source_id"] != source_id
        or record["day"] != day.isoformat()
        or record["timezone"] != timezone
        or record["hours"] != [instant.isoformat() for instant in hours]
        or not isinstance(record["models"], dict)
        or ForecastSource.PVWATTS not in record["models"]
        or set(record["models"])
        - {ForecastSource.PVWATTS, ForecastSource.ML, ForecastSource.HOME_ASSISTANT}
    ):
        raise ValueError("Invalid retained solar comparison")
    start, _end = day_window(day.isoformat(), timezone)
    if aware(datetime.fromisoformat(record["captured_at"])) > start:
        raise ValueError("Solar comparison was captured after its day began")
    for values in record["models"].values():
        if not isinstance(values, list) or len(values) != len(hours):
            raise ValueError("Invalid retained solar comparison coverage")
        for value in values:
            number(value, 0, MAX_SOLAR_ENERGY_KWH)
    revisions = record.get("ha_configuration_versions", [])
    if not isinstance(revisions, list):
        raise TypeError("Invalid retained solar provider revisions")
    for revision in revisions:
        identity(revision)
    return record


def choose_sources(
    store,
    configuration,
    baseline,
    candidate,
    calibrations,
    ha,
    histories,
    now,
    *,
    ha_references=None,
    ha_configuration_versions=None,
    ml_policies=None,
    historical_assessments=None,
):
    """Freeze next-day forecasts before outcomes, then rank identical completed days by hourly MAE.

    At most one immutable capture per source/day is retained. Candidates cannot
    gain an advantage from a shorter horizon, missing hours or later revisions.
    All currently available candidates share the same three complete daily windows.
    Old raw Recorder data cannot reconstruct forecasts that were never captured.

    Args:
        store: Active solar archive. The single model worker owns these writes.
        configuration: Typed physical sources, geometry and converter limits.
        baseline: Original physical DC/AC forecasts in kWh with HA location.
        candidate: Independent direct-production forecast, or None before fitting.
        calibrations: Fitted direct-production model identities eligible for ML.
        ha: Source IDs mapped to normalized HA planning energies in kWh.
        histories: Meter entity IDs mapped to cumulative Recorder kWh sums.
        now: Aware capture/evaluation time. No completed outcome may enter a capture.
        ha_references: Meter entity IDs mapped to original HA observation versions.
        ha_configuration_versions: Meter entity IDs mapped to opaque HA entry
            revisions. When HA participates, only days with matching revisions
            enter the common cohort. Ordering does not change membership.

    Returns:
        Selected source IDs mapped to concrete model/energy pairs and per-source
        status with mean absolute hourly kWh errors. Historical comparison read or
        validation failures set a comparison issue and disable Automatic ranking,
        preserving the baseline. Optional accuracy failures do not block manual
        selection. Physical eligibility and current availability remain mandatory.
        Damaged evidence is retained unchanged. Other invariant failures propagate.
    """
    selected, statuses = {}, {}
    fitted = {row.source_id for row in calibrations}
    tomorrow = now.astimezone(ZoneInfo(baseline.location.timezone)).date() + DAY
    future_hours, _end = comparison_hours(tomorrow, baseline.location.timezone)
    for source in configuration.sources:
        source_id = source.source_id
        field = "ac_kwh" if source.measurement_boundary == "ac" else "dc_kwh"
        models = {
            ForecastSource.PVWATTS: tuple(
                row for row in baseline.energies if row.source_id == source_id
            )
        }
        if source_id in fitted and candidate:
            models[ForecastSource.ML] = tuple(
                row for row in candidate.energies if row.source_id == source_id
            )
        if source_id in ha:
            models[ForecastSource.HOME_ASSISTANT] = ha[source_id]
        identity = family(
            configuration,
            source,
            baseline.location,
            learned_policy=(ml_policies or {}).get(source_id),
        )
        prefix = "source-day-" + identity + "-"
        provider_revisions = sorted((ha_configuration_versions or {}).get(source.entity_id, []))
        capture = {
            "captured_at": now.isoformat(),
            "day": tomorrow.isoformat(),
            "timezone": baseline.location.timezone,
            "hours": [instant.isoformat() for instant in future_hours],
            "models": {},
            "configuration_version": configuration.version,
            "baseline_version": baseline.version,
            "ml_version": candidate.version if candidate else None,
            "source_id": source_id,
            "ha_versions": (ha_references or {}).get(source.entity_id, []),
            "ha_configuration_versions": provider_revisions,
        }
        for model, energies in models.items():
            steps = [
                {
                    "start": row.start.isoformat(),
                    "duration_h": row.duration_h,
                    "kwh": getattr(row, field),
                }
                for row in energies
            ]
            values = [integral(steps, instant, instant + HOUR) for instant in future_hours]
            if all(value is not None for value in values):
                capture["models"][model] = values
        target = prefix + capture["day"] + ".json"
        evidence_issue = None
        try:
            if (
                set(capture["models"]) == set(models)
                and not store.documents.exists(target)
                and ForecastSource.PVWATTS in capture["models"]
            ):
                store.documents.write(target, capture)
        except OSError:
            evidence_issue = "Solar accuracy evidence could not be saved"
        history = histories.get(source.entity_id)
        actual = {}
        if history:
            for before, after in pairwise(history.hours):
                if after.start - before.start == HOUR and after.start + HOUR <= now:
                    value = after.total_kwh - before.total_kwh
                    if value >= 0:
                        actual[after.start] = value
        cohorts = []
        for day_index in range(1, MAX_ACCURACY_LOOKBACK_DAYS + 1):
            day = tomorrow - day_index * DAY
            hours, end = comparison_hours(day, baseline.location.timezone)
            path = prefix + day.isoformat() + ".json"
            if end > now:
                continue
            if evidence_issue:
                break
            try:
                record = _read_comparison(
                    store.documents, path, source_id, day, baseline.location.timezone, hours
                )
            except (OSError, ValueError, TypeError, KeyError):
                evidence_issue = "Stored solar accuracy evidence is unreadable. Automatic comparison is unavailable"
                cohorts = []
                break
            if record is None:
                continue
            if not set(models) <= set(record["models"]):
                continue
            if (
                ForecastSource.HOME_ASSISTANT in models
                and record.get("ha_configuration_versions", []) != provider_revisions
            ):
                continue
            if not all(
                learning_interval(source, instant, instant + HOUR)
                and unrestricted_interval(source, instant, instant + HOUR)
                for instant in hours
            ):
                continue
            values = [actual.get(instant) for instant in hours]
            if any(value is None for value in values):
                continue
            cohorts.append((record, values))
            if len(cohorts) == MIN_ACCURACY_DAYS:
                break
        scores = {}
        admission_basis = "captured_forecasts"
        eligibility_issue = eligibility(configuration, source)
        issue = eligibility_issue or evidence_issue
        if len(cohorts) == MIN_ACCURACY_DAYS and issue is None:
            scores = {
                model: mean_absolute_error(
                    [
                        predicted
                        for record, _values in cohorts
                        for predicted in record["models"][model]
                    ],
                    [observed for _record, values in cohorts for observed in values],
                )
                for model in models
            }
        assessment = (historical_assessments or {}).get(source_id)
        if (
            not scores
            and issue is None
            and ForecastSource.ML in models
            and isinstance(assessment, HistoricalForecastAssessment)
            and assessment.status == BacktestStatus.ELIGIBLE
            and assessment.created_at <= now
            and assessment.run_initializations == scheduled_runs(now)
            and assessment.source_family
            == evidence_family(
                configuration, source, baseline.location, (ml_policies or {}).get(source_id)
            )
            and assessment.model_policy == (ml_policies or {}).get(source_id)
        ):
            scores = {
                ForecastSource.PVWATTS: fsum(
                    score.baseline_mae_kwh for score in assessment.per_origin
                )
                / len(assessment.per_origin),
                ForecastSource.ML: fsum(score.service_mae_kwh for score in assessment.per_origin)
                / len(assessment.per_origin),
            }
            admission_basis = "historical_forecast_backtest"
        preference = source.forecast_source
        chosen = (
            min(scores, key=scores.get)
            if preference == ForecastSource.AUTOMATIC and scores
            else ForecastSource.PVWATTS
            if preference == ForecastSource.AUTOMATIC
            else preference
        )
        if chosen not in models or eligibility_issue and chosen != ForecastSource.PVWATTS:
            chosen = ForecastSource.PVWATTS
        learning_issue = None
        if (
            chosen in (ForecastSource.PVWATTS, ForecastSource.ML)
            and preference in (ForecastSource.AUTOMATIC, ForecastSource.ML)
            and source_id in (ml_policies or {})
            and (
                not scores
                or scores[ForecastSource.PVWATTS] - scores[ForecastSource.ML]
                < max(MIN_ABSOLUTE_GAIN_KWH, scores[ForecastSource.PVWATTS] * MIN_RELATIVE_GAIN)
            )
        ):
            eligible_scores = {
                model: error for model, error in scores.items() if model != ForecastSource.ML
            }
            chosen = (
                min(eligible_scores, key=eligible_scores.get)
                if preference == ForecastSource.AUTOMATIC and eligible_scores
                else ForecastSource.PVWATTS
            )
            learning_issue = (
                (
                    "ML is collecting matched forecast accuracy. PVWatts remains active."
                    if not scores
                    else "ML has not improved forecast accuracy enough. PVWatts remains active."
                )
                if chosen == ForecastSource.PVWATTS
                else None
            )
        selected[source_id] = (chosen, models[chosen])
        statuses[source_id] = {
            "requested": preference,
            "active": chosen,
            "mae_kwh": scores,
            "complete_days": len(cohorts),
            "required_days": 0
            if admission_basis == "historical_forecast_backtest"
            else MIN_ACCURACY_DAYS,
            "admission_basis": admission_basis,
            "comparison_days": [record["day"] for record, _values in cohorts],
            "history_version": version(document(history)) if history else None,
            "issue": issue
            or learning_issue
            or (
                "Selected forecast unavailable. PVWatts is active"
                if preference != ForecastSource.AUTOMATIC and chosen != preference
                else None
            ),
            "policy": SOURCE_POLICY,
        }
    return selected, statuses
