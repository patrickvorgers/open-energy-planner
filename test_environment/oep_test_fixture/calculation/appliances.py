"""Evaluate fixed example cycles and elapsed energy without HA state or Recorder access."""

from ..profiles import (
    HISTORY_CYCLE_HOUR_UTC,
    PHASE_SECONDS,
    POWERS_W,
    SECONDS_PER_HOUR,
    WATTS_PER_KILOWATT,
)


def historical_energy(instant):
    """Return the independently specified historical quarter energy, .83 kWh per daily complete cycle."""
    phases_per_hour = SECONDS_PER_HOUR // PHASE_SECONDS
    phase = (instant.hour - HISTORY_CYCLE_HOUR_UTC) * phases_per_hour + (
        instant.minute * 60 // PHASE_SECONDS
    )
    if not 0 <= phase < len(POWERS_W):
        return 0
    return POWERS_W[phase] / (WATTS_PER_KILOWATT * SECONDS_PER_HOUR / PHASE_SECONDS)


def integral(seconds, powers_w=POWERS_W):
    """Integrate actual simulated progress. Zero-power pauses remain unfinished phases."""
    return sum(
        power
        * min(PHASE_SECONDS, max(0, seconds - index * PHASE_SECONDS))
        / SECONDS_PER_HOUR
        / WATTS_PER_KILOWATT
        for index, power in enumerate(powers_w)
    )
