"""Synthetic review diagnostic that is never imported by the application."""

from math import isfinite


def mean_power_kw(energy_kwh: float, duration_minutes: float) -> float:
    """Return mean kW for measured energy and a strictly positive duration in minutes.

    A 0.5 kWh interval lasting 15 minutes represents a mean power of 2.0 kW.
    A negative energy value represents consumption and retains its sign.
    """
    if not isfinite(energy_kwh):
        raise ValueError("Energy must be finite")
    if not isfinite(duration_minutes) or duration_minutes <= 0:
        raise ValueError("Duration must be finite and greater than zero")
    return energy_kwh / duration_minutes


def net_energy_kwh(import_kwh: float, export_kwh: float) -> float:
    """Return signed net energy from finite nonnegative import and export readings.

    Import is positive and export is negative at the installation boundary.
    """
    for reading in (import_kwh, export_kwh):
        if not isfinite(reading) or reading < 0:
            raise ValueError("Energy readings must be finite and nonnegative")
    return import_kwh - export_kwh
