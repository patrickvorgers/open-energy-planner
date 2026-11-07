"""Normalized hourly energy history, independent of HA storage and transport."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from math import isfinite

from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.prices.model import aware

MAX_CUMULATIVE_ENERGY_KWH = 1000000000.0
MAX_HISTORY_HOURS = 9000
COMPONENT_METER_TOLERANCE_KWH = 1e-6


@dataclass(frozen=True)
class EnergyHour:
    """One completed UTC hour: cumulative statistics sum and physical meter, in kWh."""

    start: datetime
    total_kwh: float
    meter_kwh: float

    def __post_init__(self):
        """Reject missing/nonfinite values and non-hourly timestamps rather than filling gaps."""
        object.__setattr__(self, "start", aware(self.start))
        if self.start.minute or self.start.second or self.start.microsecond:
            raise ValueError("History must use UTC hour boundaries")
        for value in (self.total_kwh, self.meter_kwh):
            if (
                type(value) not in (int, float)
                or abs(value) > MAX_CUMULATIVE_ENERGY_KWH
                or not isfinite(value)
            ):
                raise ValueError("Invalid energy history value")
        if self.meter_kwh < 0:
            raise ValueError("Invalid export meter")


@dataclass(frozen=True)
class EnergyHistory:
    """Ordered aggregate-energy statistics. Gaps remain gaps, never synthetic zeros.

    component_meters holds each physical meter's final-hour cumulative endpoint
    in kWh. Empty means component evidence was not supplied, not zero-valued meters.
    """

    entity_id: str
    hours: tuple[EnergyHour, ...]
    component_meters: tuple[tuple[str, float], ...] = ()

    def __post_init__(self):
        """Bound annual transfers and require strictly ordered, typed rows."""
        if not isinstance(self.entity_id, str) or not self.entity_id.startswith("sensor."):
            raise ValueError("History requires a sensor identity")
        if not isinstance(self.hours, tuple) or not 1 <= len(self.hours) <= MAX_HISTORY_HOURS:
            raise ValueError("Expected bounded immutable hourly history")
        if any(not isinstance(hour, EnergyHour) for hour in self.hours):
            raise TypeError("Expected normalized energy hours")
        if any(
            previous_item.start >= next_item.start
            for previous_item, next_item in pairwise(self.hours)
        ):
            raise ValueError("Unordered or duplicate history")
        if not isinstance(self.component_meters, (tuple, list)):
            raise TypeError("Invalid history components")
        components = []
        for item in self.component_meters:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0].startswith("sensor.")
            ):
                raise ValueError("Invalid history component")
            EnergyHour(self.hours[-1].start, item[1], item[1])
            components.append(tuple(item))
        if len({entity for entity, _value in components}) != len(components) or (
            components
            and abs(sum(value for _entity, value in components) - self.hours[-1].meter_kwh)
            > COMPONENT_METER_TOLERANCE_KWH
        ):
            raise ValueError("History components do not match aggregate meter")
        object.__setattr__(self, "component_meters", tuple(components))

    def baseline(self, start, end):
        """Return measured period volume and final meter only with complete hourly coverage.

        HA sum accounts for historical meter resets. The anchor is the hour ending
        at the period boundary. Hourly sums follow HA's recorded sampling semantics.
        Never interpolate an unaligned contractual boundary or missing hour.
        """
        start, end = aware(start), aware(end)
        first = start - timedelta(hours=1)
        rows = tuple(hour for hour in self.hours if first <= hour.start < end)
        count = int((end - first).total_seconds() / SECONDS_PER_HOUR)
        if end <= start or (end - first).total_seconds() != count * SECONDS_PER_HOUR:
            raise ValueError("History boundary is not a completed hour")
        if len(rows) != count or any(
            hour.start != first + timedelta(hours=index) for index, hour in enumerate(rows)
        ):
            raise ValueError("Incomplete export history. Confirm a manual baseline")
        if any(
            previous_item.total_kwh > next_item.total_kwh
            for previous_item, next_item in pairwise(rows)
        ):
            raise ValueError("Export statistics decrease. Confirm a manual baseline")
        return rows[-1].total_kwh - rows[0].total_kwh, rows[-1].meter_kwh
