"""Forecast Energy-owned consumers separately, retaining the total-household safety baseline."""

import json
from dataclasses import asdict, replace
from datetime import timedelta

from open_energy_planner.appliances.sources import SOURCE_OWNERSHIP_UNAVAILABLE, validate_membership
from open_energy_planner.demand.calculation.forecasting import (
    RidgeDemandModel,
    WeekdayProfile,
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
from open_energy_planner.demand.wire import forecast_document
from open_energy_planner.history.importer import HistoryImport
from open_energy_planner.history.storage import HistoryArchive
from open_energy_planner.home_assistant.names import source_name

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
        """Bind worker admission and model comparisons to choices and current Energy membership."""
        return digest(
            [
                self.selected,
                [
                    {key: value for key, value in row.items() if key != "name"}
                    for row in self.options
                ],
                self.catalogue_known,
                self.issue,
            ]
        )

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
        """Preserve unreadable ownership, otherwise archive checked choices before changing the pointer."""
        if self.issue == "consumer_configuration_unreadable":
            raise ValueError(SOURCE_OWNERSHIP_UNAVAILABLE)
        if expected_version != self.version:
            raise ValueError("Consumer settings changed. Refresh this page")
        if not self.catalogue_known:
            raise ValueError("Select consumers configured in Home Assistant Energy")
        validate_membership(selected, self.options)
        with self.documents.transaction():
            self.documents.write("consumer-settings-" + digest(selected) + ".json", list(selected))
            self.documents.write(CONSUMER_SETTINGS_POINTER, list(selected))
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

    def document(self):
        """Expose configured choices, actual membership and explicitly shadow component predictions."""
        return {
            "version": self.version,
            "selected": list(self.selected),
            "options": list(self.options),
            "issue": self.issue,
            "history_issues": dict(self.failures),
            "result": {
                key: value for key, value in self.result.items() if key != "model_parameters"
            }
            if self.result is not None
            else None,
        }


def build_components(
    configuration,
    household_histories,
    temperatures,
    now,
    inputs,
    *,
    documents=None,
    controlled_days=None,
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
    available = {item["entity_id"] for item in options}
    if not {item["entity_id"] for item in selected} <= available:
        return None, {"status": "energy_consumer_removed", "devices": []}
    if configuration.exclusions:
        return None, {"status": "existing_exclusions_unsupported", "devices": []}
    if not configuration.terms:
        return None, {"status": "household_boundary_missing", "devices": []}
    if {item["entity_id"] for item in selected} & set(configuration.entities):
        return None, {"status": "consumer_overlaps_household_boundary", "devices": []}
    try:
        validate_membership(selected, options)
    except ValueError:
        return None, {"status": "energy_consumer_hierarchy_inconsistent", "devices": []}
    controlled_days = controlled_days or {}
    control_history_unavailable = any("*" in days for days in controlled_days.values())
    profiles, forecasts, records, ready = [], [], [], []
    has_unavailable_components = False
    for item in selected:
        try:
            single = DemandConfiguration(
                configuration.timezone, 0, (EnergyTerm(item["entity_id"], 1),)
            )
            samples = DemandReconstructor().reconstruct(single, histories, before=now)
            model, forecast, selection = component(
                single,
                samples,
                temperatures,
                now,
                item["weather_sensitive"],
                documents=documents,
                excluded_dates=("*",)
                if control_history_unavailable
                else controlled_days.get(item["entity_id"], ()),
            )
            baseline_forecast = forecast_document(
                predict(single, samples, now, WeekdayProfile.fit(single, samples))
            )
        except ValueError:
            has_unavailable_components = True
        else:
            ready.append(item)
            profiles.append((item["entity_id"], asdict(model)))
            forecasts.append(forecast)
            records.append(
                {
                    **item,
                    "status": selection["status"],
                    "model_selection": selection,
                    "weather_features_used": bool(getattr(model, "temperature_enabled", False)),
                    "forecast": forecast_document(forecast),
                    "baseline_forecast": baseline_forecast,
                }
            )
    configuration = replace(
        configuration,
        exclusions=tuple(
            ExcludedLoad(digest(item["entity_id"])[:CONSUMER_ID_HEX_LENGTH], item["entity_id"])
            for item in ready
        ),
    )
    try:
        residual = DemandReconstructor().reconstruct(
            configuration, (*household_histories, *histories), before=now
        )
        residual_configuration = replace(configuration, exclusions=())
        model, remaining, residual_selection = component(
            residual_configuration,
            residual,
            temperatures,
            now,
            True,
            documents=documents,
            boundary=configuration.version,
            excluded_dates=("*",)
            if control_history_unavailable
            else sorted(
                {
                    day
                    for entity, days in controlled_days.items()
                    if entity not in {item["entity_id"] for item in ready}
                    for day in days
                }
            ),
        )
        baseline = forecast_document(
            predict(
                residual_configuration,
                residual,
                now,
                WeekdayProfile.fit(residual_configuration, residual),
            )
        )
    except ValueError:
        return None, {
            "status": "history_or_weather_not_ready",
            "detail": COMPONENT_FORECAST_UNAVAILABLE,
            "devices": records,
            "model_parameters": json.loads(
                json.dumps(profiles, default=lambda value: value.isoformat())
            ),
        }
    profiles.append(("residual", asdict(model)))
    parameters = json.loads(json.dumps(profiles, default=lambda value: value.isoformat()))
    if has_unavailable_components and not ready:
        return None, {
            "status": "history_or_weather_not_ready",
            "detail": COMPONENT_FORECAST_UNAVAILABLE,
            "devices": records,
            "residual": forecast_document(remaining),
            "residual_baseline": baseline,
            "residual_selection": residual_selection,
            "configuration_version": settings_version,
            "model_parameters": parameters,
        }
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
            replace(configuration, exclusions=()).version,
            digest(
                [
                    settings_version,
                    profiles,
                    remaining.training_version,
                    [part.version for part in forecasts],
                ]
            ),
            now,
            COMPOSITE_MODEL_ID,
            total,
            (),
            remaining.weather_version,
            quality="estimated",
        )
        return forecast, {
            "status": "partial_history" if has_unavailable_components else "active",
            "detail": COMPONENT_FORECAST_UNAVAILABLE if has_unavailable_components else None,
            "separated_entities": [item["entity_id"] for item in ready],
            "devices": records,
            "residual": forecast_document(remaining),
            "residual_baseline": baseline,
            "residual_selection": residual_selection,
            "configuration_version": settings_version,
            "model_parameters": parameters,
        }
    except ValueError:
        return None, {
            "status": "history_or_weather_not_ready",
            "detail": COMPONENT_FORECAST_UNAVAILABLE,
            "devices": records,
            "model_parameters": parameters,
        }


def component(
    configuration,
    samples,
    temperatures,
    now,
    weather_sensitive,
    *,
    documents=None,
    boundary=None,
    excluded_dates=(),
):
    """Select suitable models by preserved daily accuracy, retaining a measured weekday baseline.

    Args:
        configuration: Demand configuration with the HA timezone and measurement boundary.
        samples: Completed hourly samples containing nonnegative residual AC kWh.
        temperatures: Available temperature evidence for optional weather features.
        now: Aware prediction time and knowledge cutoff.
        weather_sensitive: Whether temperature features and a weather candidate apply.
        documents: Optional demand archive used for original daily comparisons.
        boundary: Identity of the separated measurement component.
        excluded_dates: Local ISO dates of controlled use, or '*' to exclude all days.

    Returns:
        Three values: the selected fitted model, its forecast in AC kWh, and daily
        selection evidence. Unusable Ridge or weather candidate fits are omitted
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

    baseline = WeekdayProfile.fit(configuration, samples)
    if not baseline.residual_profile:
        raise ValueError("At least seven complete days covering every hour are required")
    models = {"baseline": baseline}
    forecasts = {"baseline": predict(configuration, samples, now, baseline)}
    weather = temperatures if weather_sensitive else ()
    try:
        candidate = RidgeDemandModel.fit(configuration, samples, temperatures=weather)
        prediction = predict(configuration, samples, now, candidate, temperatures=weather)
        models["ridge"], forecasts["ridge"] = candidate, prediction
    except ValueError:
        pass
    if weather_sensitive:
        from open_energy_planner.appliances.weather_model import WeatherDemandModel

        try:
            candidate = WeatherDemandModel.fit(
                configuration, samples, temperatures=weather, known_at=now
            )
            prediction = predict(configuration, samples, now, candidate, temperatures=weather)
            models["weather"], forecasts["weather"] = candidate, prediction
        except (ValueError, ImportError, OSError):
            pass
    selection = choose_daily(
        documents,
        [
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
        ],
        configuration.timezone,
        {name: forecast_document(forecast) for name, forecast in forecasts.items()},
        {sample.start: sample.residual_kwh for sample in samples},
        now,
        excluded_dates=excluded_dates,
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
    return models[chosen], forecasts[chosen], selection
