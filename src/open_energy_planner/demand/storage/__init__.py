"""Validated demand settings, model parameters and forecasts in the active database."""

import json
from dataclasses import asdict

from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.demand.wire import forecast_document, parse_configuration

ARCHIVE_HASH_LENGTH = 64


class DemandStore:
    """Persist only selected demand evidence, not a full copy of HA sensor history."""

    def __init__(self, documents):
        """Load validated settings without treating unreadable configuration as an empty installation."""
        self.documents = documents
        self.current, self.issue = None, None
        pointer = "current.json"
        try:
            if self.documents.exists(pointer):
                version = self.documents.read(pointer)
                if (
                    not isinstance(version, str)
                    or len(version) != ARCHIVE_HASH_LENGTH
                    or any(candidate not in "0123456789abcdef" for candidate in version)
                ):
                    raise ValueError("Invalid demand configuration reference")
                self.current = parse_configuration(
                    self.documents.read(f"configuration-{version}.json")
                )
                if self.current.version != version:
                    raise ValueError("Demand configuration hash mismatch")
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            self.current, self.issue = None, "demand_configuration_unreadable"

    def save(self, configuration):
        """Archive settings before publishing their pointer. Callers serialize writes."""
        with self.documents.transaction():
            target = f"configuration-{configuration.version}.json"
            if self.documents.exists(target):
                previous = parse_configuration(self.documents.read(target))
                if previous.version != configuration.version:
                    raise ValueError("Existing demand archive hash mismatch")
            else:
                self.documents.write(target, asdict(configuration))
            self.documents.write("current.json", configuration.version)

        self.current, self.issue = configuration, None

    def archive(self, forecast, model):
        """Retain exact predictions and fitted coefficients without unsafe pickle or historical reoptimization."""
        path = f"forecast-{forecast.version}.json"
        with self.documents.transaction():
            document = {
                "forecast": forecast_document(forecast),
                "model_parameters": self.share_parameters(asdict(model)),
            }
            self._archive(path, document)

    def _archive(self, path, document):
        """Compare immutable normalized content before inserting a new original forecast."""
        document = json.loads(json.dumps(document, default=str, allow_nan=False))
        if self.documents.exists(path):
            if self.documents.read(path) != document:
                raise ValueError("Existing demand forecast archive differs")
        else:
            self.documents.write(path, document)

    def share_parameters(self, parameters):
        """Store unchanged fitted parameters once while forecasts retain their original model reference."""
        parameters = json.loads(json.dumps(parameters, default=str, allow_nan=False))
        reference = digest(parameters)
        self._archive("model-parameters-" + reference + ".json", parameters)
        return {"reference": reference}

    def model_parameters(self, forecast_version):
        """Resolve original scalar coefficients for replay, including existing inline archives."""
        parameters = self.documents.read("forecast-" + forecast_version + ".json")[
            "model_parameters"
        ]
        if isinstance(parameters, dict) and set(parameters) == {"reference"}:
            reference = parameters["reference"]
            parameters = self.documents.read("model-parameters-" + reference + ".json")
            if digest(parameters) != reference:
                raise ValueError("Demand model parameter checksum mismatch")
        return parameters

    def archive_composite(self, forecast, components):
        """Keep original component predictions while sharing their otherwise repeated native parameters."""
        with self.documents.transaction():
            components = {
                **components,
                "model_parameters": self.share_parameters(components["model_parameters"]),
            }
            self._archive(
                "consumer-forecast-" + forecast.version + ".json",
                {"forecast": forecast_document(forecast), "components": components},
            )

    def archive_components(self, forecast, composite, components=None):
        """Freeze same-issue individual evidence, including ready records without a complete challenger.

        Args:
            forecast: Household forecast whose version identifies this immutable binding.
            composite: Complete composite forecast, or None when no complete challenger exists.
            components: Original ready-device forecasts and fitted parameters. Required
                when composite is None. Complete challengers use their existing archive.

        Raises:
            ValueError: The same household forecast already has different component evidence.
        """
        with self.documents.transaction():
            path = "quality-components-" + forecast.version + ".json"
            component_reference = (
                {
                    "version": composite.version,
                    "selected": forecast.model == "consumer-composite-v1",
                }
                if composite is not None
                else {
                    "selected": False,
                    "components": {
                        **components,
                        "model_parameters": self.share_parameters(components["model_parameters"]),
                    },
                }
            )
            component_reference = json.loads(json.dumps(component_reference))
            if self.documents.exists(path):
                if self.documents.read(path) != component_reference:
                    raise ValueError("Original component forecast reference changed")
            else:
                self.documents.write(path, component_reference)
