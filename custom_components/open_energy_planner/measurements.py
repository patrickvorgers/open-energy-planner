"""Transport-independent source bindings and immutable normalized measurements."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite

MAX_READING_AGE_SECONDS = 300


class Quantity(StrEnum):
    """Identify supported normalized power, cumulative energy and state-of-charge measurements."""

    POWER = "power"
    ENERGY = "energy"
    SOC = "soc"


@dataclass(frozen=True)
class SourceBinding:
    """App-owned link. Direction is confirmed when the source is selected."""

    key: str
    entity_id: str
    quantity: Quantity
    required: bool = True
    multiplier: int = 1
    maximum_age_s: float = MAX_READING_AGE_SECONDS
    timestamp_attribute: str | None = None

    def __post_init__(self):
        """Validate sensor identity, quantity, sign, positive freshness and optional timestamp key."""
        if (
            not isinstance(self.key, str)
            or not self.key.strip()
            or not isinstance(self.entity_id, str)
            or not re.fullmatch(r"sensor\.[a-z0-9_]+", self.entity_id)
        ):
            raise ValueError("An explicit key and sensor entity ID are required")
        if not isinstance(self.quantity, Quantity) or type(self.required) is not bool:
            raise ValueError("Invalid quantity or required flag")
        if type(self.multiplier) is not int or self.multiplier not in (-1, 1):
            raise ValueError("Source direction must be +1 or -1")
        if self.quantity != Quantity.POWER and self.multiplier != 1:
            raise ValueError("Only signed power sources may invert direction")
        try:
            valid_age = (
                type(self.maximum_age_s) in (int, float)
                and isfinite(self.maximum_age_s)
                and self.maximum_age_s > 0
            )
        except OverflowError:
            valid_age = False
        if not valid_age:
            raise ValueError("Source maximum age must be finite and positive")
        if self.timestamp_attribute is not None and (
            not isinstance(self.timestamp_attribute, str) or not self.timestamp_attribute.strip()
        ):
            raise ValueError("Timestamp attribute must be named explicitly")


@dataclass(frozen=True)
class Measurement:
    """Preserve a normalized value and its sample/report times with explicit timestamp provenance."""

    key: str
    entity_id: str
    value: float
    unit: str
    observed_at: datetime
    reported_at: datetime | None = None
    timestamp_kind: str = "ha_state"


@dataclass(frozen=True)
class SourceIssue:
    """Describe a safe source fault and whether it blocks required-input readiness."""

    key: str
    entity_id: str
    reason: str
    required: bool


@dataclass(frozen=True)
class MeasurementBatch:
    """Freeze one HA capture's readings and issues without substituting missing values."""

    captured_at: datetime
    readings: tuple[Measurement, ...]
    issues: tuple[SourceIssue, ...]

    @property
    def ready(self):
        """Return whether the capture has no required-source issues. Optional faults remain visible."""
        return not any(issue.required for issue in self.issues)
