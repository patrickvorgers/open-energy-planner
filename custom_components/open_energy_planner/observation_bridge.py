"""Map the app's selected sources to in-process readings and plain wire reports."""

from dataclasses import asdict

from .measurement_sources import HomeAssistantMeasurementSource
from .measurements import Quantity, SourceBinding
from .protocol import identifier

MAX_OBSERVATION_BINDINGS = 64


class ObservationBridge:
    """Bind one app source revision to selected in-process HA measurements and wire reports."""

    def __init__(self, states, document):
        """Validate source configuration and create an entry-local in-process reader."""
        if not isinstance(document, dict) or not isinstance(document.get("bindings"), list):
            raise TypeError("Invalid app measurement configuration")
        self.revision = identifier(document.get("revision"))
        self.bindings = tuple(
            SourceBinding(
                key=raw["key"],
                entity_id=raw["entity_id"],
                quantity=Quantity(raw["quantity"]),
                required=raw["required"],
                multiplier=raw["multiplier"],
                maximum_age_s=raw["maximum_age_s"],
                timestamp_attribute=raw["timestamp_attribute"],
            )
            for raw in document["bindings"]
        )
        if not 1 <= len(self.bindings) <= MAX_OBSERVATION_BINDINGS:
            raise ValueError("Invalid source count")
        self.source = HomeAssistantMeasurementSource(states)

    def capture(self, now, *, location=None):
        """Return required-input readiness and a report retaining timestamp provenance.

        Args:
            now: Aware capture time used to check source freshness.
            location: Optional HA configuration with latitude, longitude and
                timezone keys. The app validates it for daylight bonus windows.
                None omits the location field.
        """
        batch = self.source.read(self.bindings, now)
        readings = []
        for reading in batch.readings:
            raw = asdict(reading)
            raw["observed_at"] = reading.observed_at.isoformat()
            raw["reported_at"] = reading.reported_at.isoformat()
            readings.append(raw)
        document = {
            "config_revision": self.revision,
            "captured_at": batch.captured_at.isoformat(),
            "readings": readings,
            "issues": [asdict(issue) for issue in batch.issues],
        }
        if location is not None:
            document["location"] = location
        return batch.ready, document
