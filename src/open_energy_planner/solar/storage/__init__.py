"""Immutable solar configuration, weather/history blocks and model evidence in the active database.

Daily blocks retain exact source values and knowledge times without a full copy
per receipt. Retention remains indefinite. Capacity monitoring and storage sizing
remain release work, and no SD-card capacity guarantee is implied.
"""

from contextlib import contextmanager
from threading import RLock

from open_energy_planner.solar.calculation.model import version
from open_energy_planner.solar.wire import configuration_document, parse_configuration

ARCHIVE_HASH_LENGTH = 64
ISO_DATE_PREFIX_LENGTH = 10


class SolarStore:
    """Keep immutable configuration/weather/model evidence with an atomic current-settings pointer."""

    def __init__(self, documents):
        """Restore validated settings. Corrupt retained settings are an explicit fault, not absent PV."""
        self.documents = documents
        self._writes = RLock()
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

    def archive(self, kind, ref, value):
        """Reuse exact immutable evidence. Never overwrite a conflicting saved version."""
        with self.transaction():
            target = f"{kind}-{ref}.json"
            if self.documents.exists(target):
                if self.documents.read(target) != value:
                    raise ValueError("Solar evidence archive mismatch")
            else:
                self.documents.write(target, value)

    @contextmanager
    def transaction(self):
        """Serialize weather-cache and model-worker writes before starting their shared SQL transaction."""
        with self._writes, self.documents.transaction():
            yield

    def save(self, configuration):
        """Archive configuration before atomically installing its active pointer."""
        with self.transaction():
            self.archive(
                "configuration", configuration.version, configuration_document(configuration)
            )
            self.documents.write("current.json", configuration.version)

        self.current, self.issue = configuration, None

    def archive_model(self, model):
        """Retain validated native weights once and return detached source-bound model metadata."""
        from open_energy_planner.solar.wire import document

        metadata = document(model)
        weights = {"schema": model.schema, "model_json": metadata.pop("model_json")}
        reference = version(weights)
        self.archive("ml-weights", reference, weights)
        metadata["weights_reference"] = reference
        return metadata

    def archive_series(self, kind, ref, rows, *, entity=None):
        """Reuse unchanged daily evidence blocks instead of copying an annual history on each refresh.

        Weather values and receipt runs are separate: repeated retrieval does not
        duplicate historical values, while their actual knowledge times remain
        reconstructible. Source identity stays in each history block.
        """
        days = {}
        for row in rows:
            days.setdefault(row["start"][:ISO_DATE_PREFIX_LENGTH], []).append(row)
        blocks = []
        for day, items in days.items():
            values, receipts = [], []
            for index, item in enumerate(items):
                value = dict(item)
                received = value.pop("received_at", None)
                if received is not None and (not receipts or receipts[-1][1] != received):
                    receipts.append([index, received])
                values.append(value)
            body = {"entity_id": entity, "hours": values}
            block = version(body)
            self.archive("block", block, body)
            blocks.append({"day": day, "block": block, "receipts": receipts})
        self.archive(kind, ref, {"entity_id": entity, "blocks": blocks})
