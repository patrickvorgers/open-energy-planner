"""Select solar forecasts using frozen matched daily accuracy, independently of prices."""

from dataclasses import asdict
from datetime import datetime, timedelta
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
from open_energy_planner.solar.learning import (
    ac_comparison_issue,
    learning_interval,
    production_hours,
    unrestricted_interval,
)
from open_energy_planner.solar.model_policy import MIN_ABSOLUTE_GAIN_KWH, MIN_RELATIVE_GAIN
from open_energy_planner.solar.wire import document
from open_energy_planner.weather.run import FORECAST_WEATHER_POLICY

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
SOURCE_POLICY = "matched-local-day-mae-ac-clipping-v3"
DAILY_TOTAL_COMPARISON_DAYS = 7
MIN_UNDERESTIMATED_DAYS = 5
MIN_UNDERESTIMATION_SHARE = 0.15


def ha_energies(configuration, baseline, predictions, now):
    """Project independent pure-PV AC candidates without spending a shared converter budget.

    Dividing metered AC by declared efficiency is a standalone-equivalent DC
    input. It does not claim to recover clipped panel DC or hybrid battery routes.
    Shared AC limits apply after model selection, not to these comparison candidates.
    Missing provider coverage remains absent. No provider extends another's tail.
    """
    result, issues = {}, {}
    inverters = {row.inverter_id: row for row in configuration.inverters}
    for source in configuration.sources:
        providers = predictions.get(source.entity_id, ())
        issue = None
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


def _production_control_issue(source, hours, control):
    """Identify excluded learning dates separately from unavailable unrestricted production."""
    if not all(learning_interval(source, instant, instant + HOUR) for instant in hours):
        return "outside_learning_period"
    if not all(
        unrestricted_interval(source, instant, instant + HOUR, control) for instant in hours
    ):
        return "restricted_or_unverified_production"
    return None


def _score_comparisons(
    documents,
    source,
    timezone,
    prefix,
    tomorrow,
    now,
    *,
    models,
    provider_revisions,
    inverter,
    siblings,
    meter_hours,
    control,
    evidence_issue,
):
    """Return matched days, hourly kWh errors, exclusions and any evidence issue.

    Skip days with incomplete or ineligible readings. Unreadable evidence
    stops the scan and suppresses scoring if the hourly cohort is incomplete.
    """
    actual = meter_hours.get(source.entity_id, {})
    cohorts, exclusions = [], []
    for day_index in range(1, MAX_ACCURACY_LOOKBACK_DAYS + 1):
        day = tomorrow - day_index * DAY
        hours, end = comparison_hours(day, timezone)
        path = prefix + day.isoformat() + ".json"
        if end > now:
            continue
        if evidence_issue:
            break
        try:
            record = _read_comparison(documents, path, source.source_id, day, timezone, hours)
        except (OSError, ValueError, TypeError, KeyError):
            exclusions.append({"day": day.isoformat(), "reason": "unreadable_capture"})
            if len(cohorts) < MIN_ACCURACY_DAYS:
                evidence_issue = "Stored solar accuracy evidence is unreadable. Automatic comparison is unavailable"
                cohorts = []
            break
        if record is None:
            exclusions.append({"day": day.isoformat(), "reason": "missing_capture"})
            continue
        if not set(models) <= set(record["models"]):
            exclusions.append({"day": day.isoformat(), "reason": "missing_models"})
            continue
        if (
            ForecastSource.HOME_ASSISTANT in models
            and record.get("ha_configuration_versions", []) != provider_revisions
        ):
            exclusions.append({"day": day.isoformat(), "reason": "ha_configuration_changed"})
            continue
        if reason := _production_control_issue(source, hours, control):
            exclusions.append({"day": day.isoformat(), "reason": reason})
            continue
        values = [actual.get(instant) for instant in hours]
        if any(value is None for value in values):
            exclusions.append({"day": day.isoformat(), "reason": "missing_measurements"})
            continue
        if any(
            ac_comparison_issue(
                source.measurement_boundary,
                inverter.ac_limit_w,
                tuple(
                    meter_hours.get(other.entity_id, {}).get(instant)
                    if other.measurement_boundary == "ac"
                    else None
                    for other in siblings
                ),
            )
            for instant in hours
        ):
            exclusions.append({"day": day.isoformat(), "reason": "inverter_limit_evidence"})
            continue
        cohorts.append((record, values))
        if len(cohorts) == DAILY_TOTAL_COMPARISON_DAYS:
            break
    hourly_cohorts = cohorts[:MIN_ACCURACY_DAYS]
    scores = {}
    if len(hourly_cohorts) == MIN_ACCURACY_DAYS and evidence_issue is None:
        scores = {
            model: mean_absolute_error(
                [
                    predicted
                    for record, _values in hourly_cohorts
                    for predicted in record["models"][model]
                ],
                [observed for _record, values in hourly_cohorts for observed in values],
            )
            for model in models
        }
    return cohorts, scores, exclusions, evidence_issue


def _daily_total_evidence(cohorts, models):
    """Summarize signed and absolute daily errors on identical completed days."""
    measured = [fsum(values) for _record, values in cohorts]
    errors = {}
    for model in models:
        daily = [
            fsum(record["models"][model]) - actual
            for (record, _values), actual in zip(cohorts, measured, strict=True)
        ]
        errors[model] = {
            "bias_kwh": fsum(daily),
            "absolute_error_kwh": fsum(abs(error) for error in daily),
            "materially_underestimated_days": sum(
                actual > 0 and error <= -actual * MIN_UNDERESTIMATION_SHARE
                for error, actual in zip(daily, measured, strict=True)
            ),
        }
    return fsum(measured), errors


def _persistent_ml_underestimate(cohorts, measured, errors):
    """Require repeated material underestimation and a better physical daily forecast."""
    ml = errors.get(ForecastSource.ML)
    if len(cohorts) < DAILY_TOTAL_COMPARISON_DAYS or measured <= 0 or ml is None:
        return False
    return (
        ml["materially_underestimated_days"] >= MIN_UNDERESTIMATED_DAYS
        and ml["bias_kwh"] <= -measured * MIN_UNDERESTIMATION_SHARE
        and errors[ForecastSource.PVWATTS]["absolute_error_kwh"] < ml["absolute_error_kwh"]
    )


def _select_model(source, models, scores, control, now, *, enforce_ml_policy):
    """Return an available model and its optional learning diagnostic.

    PVWatts is required as the fallback when the preferred model is unavailable.
    When the ML policy applies, a nonempty score set must include the matched
    PVWatts and ML errors. Stale curtailment feedback takes precedence over an
    ML diagnostic when PVWatts is selected.
    """
    preference = source.forecast_source
    chosen = (
        min(scores, key=scores.get)
        if preference == ForecastSource.AUTOMATIC and scores
        else ForecastSource.PVWATTS
        if preference == ForecastSource.AUTOMATIC
        else preference
    )
    if chosen not in models:
        chosen = ForecastSource.PVWATTS
    learning_issue = None
    if (
        chosen in (ForecastSource.PVWATTS, ForecastSource.ML)
        and preference in (ForecastSource.AUTOMATIC, ForecastSource.ML)
        and enforce_ml_policy
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
    if (
        chosen == ForecastSource.PVWATTS
        and source.curtailment_entity_id
        and (
            control is None
            or control.checked_until < now.replace(minute=0, second=0, microsecond=0)
        )
    ):
        learning_issue = "Curtailment feedback is still being collected. Check the linked sensor and its Recorder history. Background checks continue while PVWatts remains available."
    return chosen, learning_issue


def _save_comparison_capture(documents, prefix, capture, models, field, future_hours):
    """Freeze a local day only once, when all currently available models cover it."""
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
            and not documents.exists(target)
            and ForecastSource.PVWATTS in capture["models"]
        ):
            documents.write(target, capture)
    except OSError:
        evidence_issue = "Solar accuracy evidence could not be saved"
    return evidence_issue


def _historical_scores(assessment, configuration, source, location, model_policy, now):
    """Compare eligible replay scores for exactly the current source and forecast schedule."""
    if not (
        isinstance(assessment, HistoricalForecastAssessment)
        and assessment.status == BacktestStatus.ELIGIBLE
        and assessment.created_at <= now
        and assessment.run_initializations == scheduled_runs(now)
        and assessment.source_family
        == evidence_family(configuration, source, location, model_policy)
        and assessment.model_policy == model_policy
    ):
        return None
    return {
        ForecastSource.PVWATTS: fsum(score.baseline_mae_kwh for score in assessment.per_origin)
        / len(assessment.per_origin),
        ForecastSource.ML: fsum(score.service_mae_kwh for score in assessment.per_origin)
        / len(assessment.per_origin),
    }


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
    feedback=None,
):
    """Freeze next-day forecasts before outcomes, then rank identical completed days by hourly MAE.

    At most one immutable capture per source/day is retained. Candidates cannot
    gain an advantage from a shorter horizon, missing hours or later revisions.
    All available candidates share three complete days for hourly accuracy and
    seven for the Automatic daily-total safeguard.
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
    meter_hours = {entity: production_hours(history, now) for entity, history in histories.items()}
    inverters = {inverter.inverter_id: inverter for inverter in configuration.inverters}
    fitted = {row.source_id for row in calibrations}
    tomorrow = now.astimezone(ZoneInfo(baseline.location.timezone)).date() + DAY
    future_hours, _end = comparison_hours(tomorrow, baseline.location.timezone)
    for source in configuration.sources:
        source_id = source.source_id
        siblings = tuple(
            other for other in configuration.sources if other.inverter_id == source.inverter_id
        )
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
        evidence_issue = _save_comparison_capture(
            store.documents, prefix, capture, models, field, future_hours
        )
        history = histories.get(source.entity_id)
        cohorts, scores, exclusions, issue = _score_comparisons(
            store.documents,
            source,
            baseline.location.timezone,
            prefix,
            tomorrow,
            now,
            models=models,
            provider_revisions=provider_revisions,
            inverter=inverters[source.inverter_id],
            siblings=siblings,
            meter_hours=meter_hours,
            control=(feedback or {}).get(source.curtailment_entity_id),
            evidence_issue=evidence_issue,
        )
        admission_basis = "captured_forecasts"
        assessment = (historical_assessments or {}).get(source_id)
        if not scores and issue is None and ForecastSource.ML in models:
            historical_scores = _historical_scores(
                assessment,
                configuration,
                source,
                baseline.location,
                (ml_policies or {}).get(source_id),
                now,
            )
            if historical_scores is not None:
                scores = historical_scores
                admission_basis = "historical_forecast_backtest"
        preference = source.forecast_source
        chosen, learning_issue = _select_model(
            source,
            models,
            scores,
            (feedback or {}).get(source.curtailment_entity_id),
            now,
            enforce_ml_policy=source_id in (ml_policies or {}),
        )
        measured, daily_errors = _daily_total_evidence(cohorts, models)
        ml_held = (
            preference == ForecastSource.AUTOMATIC
            and chosen == ForecastSource.ML
            and _persistent_ml_underestimate(cohorts, measured, daily_errors)
        )
        if ml_held:
            chosen = ForecastSource.PVWATTS
            learning_issue = "PVWatts is active because machine learning repeatedly underestimated daily production."
        selected[source_id] = (chosen, models[chosen])
        comparison_days = [record["day"] for record, _values in cohorts]
        statuses[source_id] = {
            "requested": preference,
            "active": chosen,
            "mae_kwh": scores,
            "complete_days": min(len(comparison_days), MIN_ACCURACY_DAYS),
            "required_days": 0
            if admission_basis == "historical_forecast_backtest"
            else MIN_ACCURACY_DAYS,
            "admission_basis": admission_basis,
            "comparison_days": comparison_days[:MIN_ACCURACY_DAYS],
            "comparison_exclusions": exclusions,
            "daily_total_comparison": {
                "days": comparison_days,
                "required_days": DAILY_TOTAL_COMPARISON_DAYS,
                "measured_kwh": measured,
                "models": daily_errors,
                "ml_held": ml_held,
            },
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
