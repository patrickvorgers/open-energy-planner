"""Calculate conserved hourly and quarter-hour fixture energy without HA initialization."""

from math import pi, sin
from zoneinfo import ZoneInfo

from ..policy import ENERGY, QUARTER_HOURS, QUARTER_MINUTES, QUARTER_WEIGHTS


def hour_energy(instant):
    """Make a conserved hourly AC balance: household + export = grid import + two PV sources. Battery stays idle."""
    hour = instant.astimezone(ZoneInfo("Europe/Amsterdam")).hour + 0.5
    sun = max(0, sin((hour - 7) / 12 * pi)) if 7 <= hour <= 19 else 0
    roof, garage = 2 * sun, 0.8 * sun
    load = 0.6 if 17 <= hour < 23 else 0.4 if 6 <= hour < 10 else 0.2
    return dict(
        zip(
            ENERGY,
            (max(0, load - roof - garage), max(0, roof + garage - load), roof, garage, 0, 0),
            strict=True,
        )
    )


def quarter_energy(instant, *, include_dishwasher=True):
    """Vary each quarter while preserving hourly totals and the conserved AC balance."""
    weight = QUARTER_WEIGHTS[instant.minute // QUARTER_MINUTES]
    values = {key: value * QUARTER_HOURS * weight for key, value in hour_energy(instant).items()}
    if include_dishwasher:
        from .appliances import historical_energy

        extra = historical_energy(instant)
        net = values["grid_import"] - values["grid_export"] + extra
        values["grid_import"], values["grid_export"] = max(0, net), max(0, -net)
    return values
