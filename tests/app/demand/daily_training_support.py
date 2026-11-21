"""Shared completed demand measurements and known-at weather for daily-training proofs."""

from datetime import UTC, datetime, timedelta

from open_energy_planner.demand.calculation.model import DemandHour
from open_energy_planner.weather.model import TemperatureHour

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def samples_before(before, *, hours=40 * 24):
    """Provide independent complete measured hours with a recurring clock and temperature effect."""
    start = before.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours)
    samples = []
    for offset in range(hours):
        instant = start + timedelta(hours=offset)
        temperature = 10 + instant.hour / 2
        energy = 0.3 + max(0, 18 - temperature) * 0.05
        samples.append(DemandHour(instant, energy, energy))
    return tuple(samples)


def temperatures_for(samples, now):
    """Retain historical knowledge and append future weather known at the prediction time."""
    historical = tuple(
        TemperatureHour(sample.start, 10 + sample.start.hour / 2, sample.start + timedelta(hours=1))
        for sample in samples
    )
    future = tuple(
        TemperatureHour(now + timedelta(hours=offset), 12 + offset % 8, now) for offset in range(72)
    )
    return historical + future
