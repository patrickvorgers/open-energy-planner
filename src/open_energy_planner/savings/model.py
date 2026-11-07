"""Finite, timezone-aware settlement inputs. No HA, ORM or solver objects."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import isfinite

from open_energy_planner.history.model import MAX_CUMULATIVE_ENERGY_KWH

from ..prices.model import aware

SETTLEMENT_MINUTES = 15
SETTLEMENT_INTERVAL = timedelta(minutes=SETTLEMENT_MINUTES)
MAX_BATCH_INTERVALS = 96
MAX_SETTLEMENT_FRAME_BYTES = 192 * 1024
MAX_ENERGY_KWH = 1_000_000
MAX_SETTLEMENT_ID_LENGTH = 128
ENERGY_TOLERANCE_KWH = 1e-7
POLICY_VERSION = "self-consumption-v1"


class PendingSettlement(ValueError):
    """Await routine external evidence without asking the user to repair a valid configuration."""


def energy(value, maximum=MAX_ENERGY_KWH):
    """Reject bools, negative, missing and nonfinite energy instead of inventing zero."""
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= maximum:
        raise ValueError("Invalid settlement energy")
    return float(value)


@dataclass(frozen=True)
class Frame:
    """One quarter or requested boundary fragment with AC deltas and battery kWh.

    Channels retain HA entity IDs. Missing channels remain absent. Estimated
    reconstruction is explicitly labelled and never presented as exact metering.

    Channel values are finite non-negative AC energy deltas in kWh over the
    interval. Direction comes from the source binding, not a negative delta.
    Battery start/end maps hold non-negative internal stored kWh by virtual
    battery ID, not SoC percentages. Optional meter_start values are non-negative
    cumulative meter readings in kWh at the interval start, keyed by HA entity ID.
    """

    start: datetime
    end: datetime
    channels: dict
    battery_start: dict
    battery_end: dict
    meter_start: dict
    quality: str

    def __post_init__(self):
        """Require aware up-to-quarter intervals, bounded detached maps and declared quality."""
        object.__setattr__(self, "start", aware(self.start))
        object.__setattr__(self, "end", aware(self.end))
        if not timedelta(0) < self.end - self.start <= SETTLEMENT_INTERVAL or self.quality not in (
            "recorded",
            "estimated_hourly",
            "estimated_boundary",
            "estimated_soc",
        ):
            raise ValueError("Invalid settlement interval")
        for name in ("channels", "battery_start", "battery_end", "meter_start"):
            values = getattr(self, name)
            if not isinstance(values, dict):
                raise TypeError("Invalid settlement channels")
            if any(
                not isinstance(key, str) or not 1 <= len(key) <= MAX_SETTLEMENT_ID_LENGTH
                for key in values
            ):
                raise ValueError("Invalid settlement identity")
            maximum = MAX_CUMULATIVE_ENERGY_KWH if name == "meter_start" else MAX_ENERGY_KWH
            object.__setattr__(
                self, name, {key: energy(value, maximum) for key, value in values.items()}
            )
        channels = {
            name: getattr(self, name)
            for name in ("channels", "battery_start", "battery_end", "meter_start")
        }
        if len(json.dumps(channels, allow_nan=False).encode()) > MAX_SETTLEMENT_FRAME_BYTES:
            raise ValueError("Settlement frame exceeds size limit")

    def record(self):
        """Return transport-safe immutable evidence without dropping timestamps or quality."""
        return {**asdict(self), "start": self.start.isoformat(), "end": self.end.isoformat()}


def parse_frame(body):
    """Accept only declared finite frame fields at the API boundary."""
    if not isinstance(body, dict) or set(body) != {
        "start",
        "end",
        "channels",
        "battery_start",
        "battery_end",
        "meter_start",
        "quality",
    }:
        raise ValueError("Invalid settlement frame")
    return Frame(
        **{
            **body,
            "start": datetime.fromisoformat(body["start"]),
            "end": datetime.fromisoformat(body["end"]),
        }
    )
