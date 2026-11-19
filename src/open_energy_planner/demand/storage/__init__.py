"""Validated demand settings, model parameters and forecasts in the active database."""

import json
from dataclasses import asdict

from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.demand.wire import forecast_document, parse_configuration

ARCHIVE_HASH_LENGTH = 64


def _native_json(parameters):
    """Check native size, digest and JSON envelope before optional native libraries are loaded."""
    from open_energy_planner.appliances.weather_model import MAX_MODEL_JSON_BYTES

    native = parameters["model_json"]
    if (
        not isinstance(native, str)
        or len(native.encode()) > MAX_MODEL_JSON_BYTES
        or digest(native) != parameters.get("model_digest")
    ):
        raise ValueError("Demand native model checksum mismatch")
    value = json.loads(native)
    valid = isinstance(value, dict) and isinstance(value.get("learner"), dict)
    if not valid:
        raise ValueError("Invalid demand native model archive")
    return native


def share_native_weights(documents, parameters):
    """Share bounded native JSON independently of expiring fit reports and scalar parameters."""
    if isinstance(parameters, list):
        return [share_native_weights(documents, item) for item in parameters]
    if not isinstance(parameters, dict):
        return parameters
    if "model_json" not in parameters:
        return {key: share_native_weights(documents, value) for key, value in parameters.items()}
    native = _native_json(parameters)
    if "weights_reference" in parameters:
        raise ValueError("Invalid demand native model reference")
    weights = {"model_json": native}
    reference = digest(weights)
    target = "ml-weights-" + reference + ".json"
    if documents.exists(target):
        if documents.read(target) != weights:
            raise ValueError("Demand native model archive mismatch")
    else:
        documents.write(target, weights)
    return {
        **{key: value for key, value in parameters.items() if key != "model_json"},
        "weights_reference": reference,
    }


def resolve_native_weights(documents, parameters):
    """Verify shared native bodies before restoring metadata, preserving existing inline models."""
    if isinstance(parameters, list):
        return [resolve_native_weights(documents, item) for item in parameters]
    if not isinstance(parameters, dict):
        return parameters
    if "weights_reference" not in parameters:
        resolved = {
            key: resolve_native_weights(documents, value) for key, value in parameters.items()
        }
    else:
        reference = parameters["weights_reference"]
        if (
            not isinstance(reference, str)
            or len(reference) != ARCHIVE_HASH_LENGTH
            or any(character not in "0123456789abcdef" for character in reference)
            or "model_json" in parameters
        ):
            raise ValueError("Invalid demand native model reference")
        weights = documents.read("ml-weights-" + reference + ".json")
        if (
            not isinstance(weights, dict)
            or set(weights) != {"model_json"}
            or digest(weights) != reference
        ):
            raise ValueError("Demand native model checksum mismatch")
        resolved = {
            **{key: value for key, value in parameters.items() if key != "weights_reference"},
            "model_json": weights["model_json"],
        }
    if "model_json" in resolved:
        _native_json(resolved)
    return resolved


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
            if self._comparison(self.documents.read(path)) != self._comparison(document):
                raise ValueError("Existing demand forecast archive differs")
        else:
            self.documents.write(path, document)

    def share_parameters(self, parameters):
        """Store unchanged fitted parameters once while forecasts retain their original model reference."""
        parameters = json.loads(json.dumps(parameters, default=str, allow_nan=False))
        with self.documents.transaction():
            parameters = share_native_weights(self.documents, parameters)
            reference = digest(parameters)
            self._archive("model-parameters-" + reference + ".json", parameters)
        return {"reference": reference}

    def _resolve_parameters(self, parameters):
        """Compare parameter values independently of their original inline or shared representation."""
        if isinstance(parameters, dict) and set(parameters) == {"reference"}:
            reference = parameters["reference"]
            if (
                not isinstance(reference, str)
                or len(reference) != ARCHIVE_HASH_LENGTH
                or any(character not in "0123456789abcdef" for character in reference)
            ):
                raise ValueError("Invalid demand model parameter reference")
            parameters = self.documents.read("model-parameters-" + reference + ".json")
            if digest(parameters) != reference:
                raise ValueError("Demand model parameter checksum mismatch")
        return resolve_native_weights(self.documents, parameters)

    def _comparison(self, document):
        """Resolve only model parameters while keeping all original forecast and component fields exact."""
        if not isinstance(document, dict):
            return document
        compared = dict(document)
        if "model_parameters" in compared:
            compared["model_parameters"] = self._resolve_parameters(compared["model_parameters"])
        components = compared.get("components")
        if isinstance(components, dict) and "model_parameters" in components:
            compared["components"] = {
                **components,
                "model_parameters": self._resolve_parameters(components["model_parameters"]),
            }
        return compared

    def model_parameters(self, forecast_version):
        """Resolve original scalar coefficients for replay, including existing inline archives."""
        parameters = self.documents.read("forecast-" + forecast_version + ".json")[
            "model_parameters"
        ]
        return self._resolve_parameters(parameters)

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
                if self._comparison(self.documents.read(path)) != self._comparison(
                    component_reference
                ):
                    raise ValueError("Original component forecast reference changed")
            else:
                self.documents.write(path, component_reference)
