"""Immutable solar configuration, weather/history blocks and model evidence in the active database.

Daily blocks retain exact source values and knowledge times without a full copy
per receipt. Retention remains indefinite. Capacity monitoring and storage sizing
remain release work, and no SD-card capacity guarantee is implied.
"""

from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.wire import configuration_document, parse_configuration
from open_energy_planner.storage.series import SeriesArchive

ARCHIVE_HASH_LENGTH = 64


class SolarStore(SeriesArchive):
    """Keep immutable configuration/weather/model evidence with an atomic current-settings pointer."""

    def __init__(self, documents):
        """Restore validated settings. Corrupt retained settings are an explicit fault, not absent PV."""
        super().__init__(documents)
        self.current, self.issue = None, None
        try:
            pointer = "current.json"
            if self.documents.exists(pointer):
                ref = self.documents.read(pointer)
                if (
                    not isinstance(ref, str)
                    or len(ref) != ARCHIVE_HASH_LENGTH
                    or any(candidate not in "0123456789abcdef" for candidate in ref)
                ):
                    raise ValueError("Invalid solar reference")
                self.current = parse_configuration(self.documents.read(f"configuration-{ref}.json"))
                if self.current.version != ref:
                    raise ValueError("Solar configuration hash mismatch")
        except (OSError, ValueError, TypeError, KeyError):
            self.current, self.issue = None, "solar_configuration_unreadable"

    def save(self, configuration):
        """Archive configuration before atomically installing its active pointer."""
        self.write_configuration(configuration)
        self.current, self.issue = configuration, None

    def write_configuration(self, configuration):
        """Write the durable revision without changing runtime state."""
        with self.transaction():
            self.archive(
                "configuration", configuration.version, configuration_document(configuration)
            )
            self.documents.write("current.json", configuration.version)

    def archive_model(self, model):
        """Retain validated native weights once and return detached source-bound model metadata."""
        from open_energy_planner.solar.wire import document

        metadata = document(model)
        weights = {"schema": model.schema, "model_json": metadata.pop("model_json")}
        reference = version(weights)
        self.archive("ml-weights", reference, weights)
        metadata["weights_reference"] = reference
        return metadata
