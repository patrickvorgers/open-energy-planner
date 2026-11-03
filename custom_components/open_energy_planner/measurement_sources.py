"""In-process HA measurement bridge. No app-to-HA connection or HA API token."""

from datetime import UTC, datetime
from math import isfinite

from .measurements import Measurement, MeasurementBatch, Quantity, SourceIssue

FULL_SOC_PERCENT = 100

_UNITS = {
    Quantity.POWER: {"W": (1, "W"), "kW": (1000, "W")},
    Quantity.ENERGY: {"Wh": (0.001, "kWh"), "kWh": (1, "kWh"), "MWh": (1000, "kWh")},
    Quantity.SOC: {"%": (1, "%")},
}


class SourceReadError(ValueError):
    """Safe diagnostics omit URLs, tokens, response bodies and raw state values."""


class StateNormalizer:
    """Convert selected HA states into immutable normalized readings and explicit source issues."""

    def normalize(self, states, bindings, now):
        """Validate state identity and emit one reading or issue per selected source."""
        if now.utcoffset() is None:
            raise ValueError("Capture time requires a timezone")
        if not isinstance(states, list):
            raise SourceReadError("invalid_state_response")
        bindings = tuple(bindings)
        if len({binding.key for binding in bindings}) != len(bindings):
            raise ValueError("Source keys must be unique")
        indexed = {}
        for state in states:
            if not isinstance(state, dict) or not isinstance(state.get("entity_id"), str):
                raise SourceReadError("invalid_state_response")
            entity_id = state["entity_id"]
            if entity_id in indexed:
                raise SourceReadError("duplicate_entity")
            indexed[entity_id] = state
        readings, issues = [], []
        for binding in bindings:
            try:
                readings.append(self._reading(indexed.get(binding.entity_id), binding, now))
            except SourceReadError as error:
                issues.append(
                    SourceIssue(binding.key, binding.entity_id, str(error), binding.required)
                )
        return MeasurementBatch(now, tuple(readings), tuple(issues))

    def _reading(self, state, binding, now):
        """Normalize units/signs and preserve valid original sample/report timestamps.

        An explicitly configured device timestamp takes precedence over HA report time.
        Neither state age nor an old sample attribute proves that a current available HA value is invalid.
        """
        if state is None:
            raise SourceReadError("missing_entity")
        raw = state.get("state")
        if raw in (None, "unknown", "unavailable"):
            raise SourceReadError("unavailable")
        attributes = state.get("attributes")
        if not isinstance(attributes, dict):
            raise SourceReadError("invalid_attributes")
        try:
            factor, unit = _UNITS[binding.quantity][attributes.get("unit_of_measurement")]
        except (KeyError, TypeError):
            raise SourceReadError("unsupported_unit") from None
        if isinstance(raw, bool):
            raise SourceReadError("invalid_number")
        try:
            value = float(raw) * factor * binding.multiplier
        except (ValueError, TypeError, OverflowError):
            raise SourceReadError("invalid_number") from None
        if not isfinite(value):
            raise SourceReadError("invalid_number")
        if binding.quantity == Quantity.SOC and not 0 <= value <= FULL_SOC_PERCENT:
            raise SourceReadError("invalid_soc")
        if binding.quantity == Quantity.ENERGY and value < 0:
            raise SourceReadError("negative_energy")
        # An explicit device sample time takes precedence. Never replace it with receipt time.
        stamp = (
            attributes.get(binding.timestamp_attribute)
            if binding.timestamp_attribute
            else state.get("last_reported", state.get("last_updated"))
        )
        try:
            observed = datetime.fromisoformat(stamp)
            if observed.utcoffset() is None:
                raise ValueError
            observed = observed.astimezone(UTC)
            reported = datetime.fromisoformat(state.get("last_reported", state.get("last_updated")))
            if reported.utcoffset() is None:
                raise ValueError
            reported = reported.astimezone(UTC)
        except (ValueError, TypeError, OverflowError):
            raise SourceReadError("invalid_timestamp") from None
        if observed > now or reported > now:
            raise SourceReadError("future_measurement")
        kind = (
            "device_sample"
            if binding.timestamp_attribute
            else ("ha_report" if "last_reported" in state else "ha_state")
        )
        return Measurement(binding.key, binding.entity_id, value, unit, observed, reported, kind)


class HomeAssistantMeasurementSource:
    """Read only selected HA states inside the integration. Never call HA over HTTP."""

    def __init__(self, states):
        """Read from the supplied in-process HA state registry without network credentials."""
        self._states = states

    def read(self, bindings, now):
        """Copy only selected states, preserve last_reported and normalize without mutating HA state."""
        bindings = tuple(bindings)
        selected = []
        for entity_id in dict.fromkeys(binding.entity_id for binding in bindings):
            state = self._states.get(entity_id)
            if state is not None:
                document = dict(state.as_dict())
                reported = getattr(state, "last_reported", None)
                if isinstance(reported, datetime):
                    document["last_reported"] = reported.isoformat()
                selected.append(document)
        return StateNormalizer().normalize(selected, bindings, now)
