"""Immutable installation location supplied by the Home Assistant boundary."""

from dataclasses import dataclass
from math import isfinite
from zoneinfo import ZoneInfo

MAX_LATITUDE_DEGREES = 90
MAX_LONGITUDE_DEGREES = 180


@dataclass(frozen=True)
class SiteLocation:
    """Preserve configured coordinates/timezone used for a plan's local solar calculations."""

    latitude: float
    longitude: float
    timezone: str

    def __post_init__(self):
        """Reject missing, non-finite, boolean or out-of-range location metadata."""
        if any(
            type(value) not in (int, float) or not isfinite(value)
            for value in (self.latitude, self.longitude)
        ):
            raise ValueError("Location requires finite coordinates")
        if (
            not -MAX_LATITUDE_DEGREES <= self.latitude <= MAX_LATITUDE_DEGREES
            or not -MAX_LONGITUDE_DEGREES <= self.longitude <= MAX_LONGITUDE_DEGREES
        ):
            raise ValueError("Location coordinates are outside Earth bounds")
        if not isinstance(self.timezone, str):
            raise TypeError("Location requires a timezone")
        ZoneInfo(self.timezone)
