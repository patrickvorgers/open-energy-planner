"""Forecast Energy-owned consumers separately, retaining the total-household safety baseline."""

import json
from dataclasses import asdict, replace
from datetime import timedelta

from open_energy_planner.appliances.sources import validate_membership
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
from open_energy_planner.home_assistant.names import source_name

CONSUMER_SETTINGS_POINTER = "consumer-settings.json"
COMPOSITE_MODEL_ID = "consumer-composite-v1"
CONSUMER_ID_HEX_LENGTH = 16


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
        self.histories, self.ranges, self.failures = {}, {}, {}
        self.result = None
        self.issue = None
        try:
            if documents.exists(CONSUMER_SETTINGS_POINTER):
                self.selected = parse_selection(documents.read(CONSUMER_SETTINGS_POINTER))
        except (OSError, ValueError, TypeError, KeyError):
            self.issue = "consumer_configuration_unreadable"

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
        """Validate membership/overlap and atomically retain old settings before changing the pointer."""
        if expected_version != self.version:
            raise ValueError("Consumer settings changed. Refresh this page")
        if not self.catalogue_known:
            raise ValueError("Select consumers configured in Home Assistant Energy")
        validate_membership(selected, self.options)
        with self.documents.transaction():
            self.documents.write("consumer-settings-" + digest(selected) + ".json", list(selected))
            self.documents.write(CONSUMER_SETTINGS_POINTER, list(selected))
        self.selected, self.issue, self.result = selected, None, None
        self.histories, self.ranges, self.failures = {}, {}, {}

    def request(self, configuration, now):
        """Request each selected meter once per daily cohort. Failures retry in the next cohort."""
        from open_energy_planner.demand.runtime import HISTORY_LOOKBACK_DAYS

        if configuration is None or self.issue or not self.catalogue_known:
            return None
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(days=HISTORY_LOOKBACK_DAYS, hours=1)
        available = {item["entity_id"] for item in self.options}
        for item in self.selected:
            entity = item["entity_id"]
            if entity in available and self.ranges.get(entity) != (start, end):
                body = {
                    "entity_id": entity,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "configuration_version": digest([configuration.version, self.version]),
                }
                return {**body, "version": digest(body)}
        return None

    def receive(self, pending, history=None, error=None):
        """Install only the admitted range, or record missing evidence without inventing zero."""
        from datetime import datetime

        start, end = (datetime.fromisoformat(pending[key]) for key in ("start", "end"))
        entity = pending["entity_id"]
        if history is not None and (
            history.entity_id != entity
            or any(not start <= hour.start < end for hour in history.hours)
        ):
            raise ValueError("Individual consumer history outside requested range")
        self.ranges[entity] = start, end
        if error:
            self.histories.pop(entity, None)
            self.failures[entity] = error
        else:
            self.histories[entity] = history
            self.failures.pop(entity, None)

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
    configuration, household_histories, temperatures, now, inputs, *, documents=None
):
    """Build a whole-house challenger as residual plus disjoint consumer forecasts exactly once.

    The existing total-household model remains active until the shared decision
    evaluator admits this complete challenger. Missing component evidence never
    silently removes household demand. Unavailable components remain inside the
    residual while independent valid components remain available for planning.

    Returns:
        The complete composite and its status document, or two None values when
        no consumers are selected. Component failures retain only ready device
        records and fitted parameters, without a composite candidate. Residual
        forecasts, their baseline and configuration version are included only
        when residual training succeeds. Failure details identify the affected
        source. The appliance preparation chain requires complete residual and
        controlled-component coverage before changing the household boundary.
    """
    settings_version, selected, histories, options = inputs
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
    profiles, forecasts, records, ready, failures = [], [], [], [], []
    for item in selected:
        try:
            single = DemandConfiguration(
                configuration.timezone, 0, (EnergyTerm(item["entity_id"], 1),)
            )
            samples = DemandReconstructor().reconstruct(single, histories, before=now)
            model, forecast = component(
                single, samples, temperatures, now, item["weather_sensitive"]
            )
            selection = None
            if item["weather_sensitive"]:
                from open_energy_planner.appliances.weather_learning import select_weather_model

                model, forecast, selection = select_weather_model(
                    single, samples, temperatures, now, model, forecast, documents
                )
            baseline_forecast = forecast_document(
                predict(single, samples, now, WeekdayProfile.fit(single, samples))
            )
        except ValueError as error:
            failures.append(item["entity_id"] + ": " + str(error))
        else:
            ready.append(item)
            profiles.append((item["entity_id"], asdict(model)))
            forecasts.append(forecast)
            records.append(
                {
                    **item,
                    "status": "shadow_waiting_for_decision_evidence",
                    "weather_features_used": bool(getattr(model, "temperature_enabled", False)),
                    "weather_learning": selection,
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
        model, remaining = component(residual_configuration, residual, temperatures, now, True)
        baseline = forecast_document(
            predict(
                residual_configuration,
                residual,
                now,
                WeekdayProfile.fit(residual_configuration, residual),
            )
        )
    except ValueError as error:
        failures.append("residual: " + str(error))
        return None, {
            "status": "history_or_weather_not_ready",
            "detail": "\n".join(failures),
            "devices": records,
            "model_parameters": json.loads(
                json.dumps(profiles, default=lambda value: value.isoformat())
            ),
        }
    profiles.append(("residual", asdict(model)))
    parameters = json.loads(json.dumps(profiles, default=lambda value: value.isoformat()))
    if failures:
        return None, {
            "status": "history_or_weather_not_ready",
            "detail": "\n".join(failures),
            "devices": records,
            "residual": forecast_document(remaining),
            "residual_baseline": baseline,
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
            "status": "shadow_waiting_for_decision_evidence",
            "devices": records,
            "residual": forecast_document(remaining),
            "residual_baseline": baseline,
            "configuration_version": settings_version,
            "model_parameters": parameters,
        }
    except ValueError as error:
        return None, {
            "status": "history_or_weather_not_ready",
            "detail": str(error),
            "devices": records,
            "model_parameters": parameters,
        }


def component(configuration, samples, temperatures, now, weather_sensitive):
    """Reuse existing profile/Ridge models. Incomplete evidence stays unavailable, not a zero startup."""
    baseline = WeekdayProfile.fit(configuration, samples)
    if not baseline.residual_profile:
        raise ValueError("At least seven complete days covering every hour are required")
    model = baseline
    weather = temperatures if weather_sensitive else ()
    try:
        candidate = RidgeDemandModel.fit(configuration, samples, temperatures=weather)
        if weather_sensitive and not candidate.temperature_enabled:
            raise ValueError("Weather training coverage missing")
        prediction = predict(configuration, samples, now, candidate, temperatures=weather)
        return candidate, prediction
    except ValueError:
        return model, predict(configuration, samples, now, model, temperatures=weather)
