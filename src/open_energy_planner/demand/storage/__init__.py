"""Validated demand settings and original forecasts in the active database."""

import json
from dataclasses import asdict

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
        self.write_configuration(configuration)
        self.current, self.issue = configuration, None

    def write_configuration(self, configuration):
        """Write the durable revision without changing runtime state."""
        with self.documents.transaction():
            target = f"configuration-{configuration.version}.json"
            if self.documents.exists(target):
                previous = parse_configuration(self.documents.read(target))
                if previous.version != configuration.version:
                    raise ValueError("Existing demand archive hash mismatch")
            else:
                self.documents.write(target, asdict(configuration))
            self.documents.write("current.json", configuration.version)

    def archive(self, forecast):
        """Retain the exact issued prediction and its weather reference."""
        with self.documents.transaction():
            self._archive(
                "forecast-" + forecast.version + ".json", {"forecast": forecast_document(forecast)}
            )

    def _archive(self, path, document):
        """Compare original predictions, leaving historical attachments untouched."""
        document = json.loads(json.dumps(document, default=str, allow_nan=False))
        if self.documents.exists(path):
            previous = self.documents.read(path)
            if not isinstance(previous, dict):
                raise ValueError("Invalid original demand forecast archive")
            previous.pop("model_parameters", None)
            if isinstance(previous.get("components"), dict):
                previous["components"].pop("model_parameters", None)
            if previous != document:
                raise ValueError("Existing demand forecast archive differs")
        else:
            self.documents.write(path, document)

    def archive_composite(self, forecast, components):
        """Retain the exact aggregate forecast and its original component predictions."""
        with self.documents.transaction():
            self._archive(
                "consumer-forecast-" + forecast.version + ".json",
                {"forecast": forecast_document(forecast), "components": components},
            )

    def archive_components(self, forecast, composite, components=None):
        """Freeze same-issue device forecasts, including ready devices without a complete challenger."""
        with self.documents.transaction():
            component_reference = (
                {
                    "version": composite.version,
                    "selected": forecast.model == "consumer-composite-v1",
                }
                if composite is not None
                else {"selected": False, "components": components}
            )
            self._archive("quality-components-" + forecast.version + ".json", component_reference)
