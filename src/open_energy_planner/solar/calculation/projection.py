"""Project independent measured-production models onto the shared planning grid."""

from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from math import fsum

from open_energy_planner.core.units import MINUTES_PER_HOUR, SECONDS_PER_HOUR, WATTS_PER_KW
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES
from open_energy_planner.solar.calculation.model import SolarEnergy, SolarForecast, horizon, version

QUARTERS_PER_HOUR = MINUTES_PER_HOUR // FINE_INTERVAL_MINUTES
QUARTER_HOURS = FINE_INTERVAL_MINUTES / MINUTES_PER_HOUR
COVERAGE_TOLERANCE_HOURS = 1e-8


def limit_ac_production(configuration, energies):
    """Keep selected PV-only AC forecasts within each shared inverter rating.

    Mixing independently selected models must not give every source the whole
    converter budget. Preserve measured-boundary conversion and existing clipping
    by reducing AC only. Available DC and original candidate archives are unchanged.
    """
    inverters = {inverter.inverter_id: inverter for inverter in configuration.inverters}
    sources = {source.source_id: source.inverter_id for source in configuration.sources}
    totals = defaultdict(float)
    for energy in energies:
        totals[sources[energy.source_id], energy.start, energy.duration_h] += energy.ac_kwh
    ratios = {
        interval: min(1, inverters[interval[0]].ac_limit_w / WATTS_PER_KW * interval[2] / total)
        if total
        else 1
        for interval, total in totals.items()
    }
    return tuple(
        replace(
            energy,
            ac_kwh=energy.ac_kwh
            * ratios[sources[energy.source_id], energy.start, energy.duration_h],
        )
        for energy in energies
    )


def integrate_intervals(rows, now, source_ids):
    """Require complete quarter coverage and conserve DC/AC kWh on the planning grid."""
    energies = []
    for source_id in source_ids:
        source_rows = [row for row in rows if row.source_id == source_id]
        for start, duration in horizon(now):
            end = start + timedelta(hours=duration)
            overlaps = [
                (
                    row,
                    max(
                        0,
                        (
                            min(end, row.start + timedelta(hours=row.duration_h))
                            - max(start, row.start)
                        ).total_seconds()
                        / SECONDS_PER_HOUR,
                    ),
                )
                for row in source_rows
            ]
            if (
                abs(fsum(overlap for _row, overlap in overlaps) - duration)
                > COVERAGE_TOLERANCE_HOURS
            ):
                raise ValueError("solar_weather_coverage_missing")
            energies.append(
                SolarEnergy(
                    source_id,
                    start,
                    duration,
                    fsum(row.dc_kwh * overlap / row.duration_h for row, overlap in overlaps),
                    fsum(row.ac_kwh * overlap / row.duration_h for row, overlap in overlaps),
                )
            )
    return tuple(energies)


def learned_forecast(configuration, location, weather, now, models, *, planning_origin=None):
    """Predict boundary production without PVWatts, then apply declared converter limits.

    AC predictions imply standalone-equivalent DC through declared efficiency.
    This does not reconstruct curtailed/clipped panel potential or hybrid routes.
    A shared inverter receives one common cap after every source prediction.
    Missing model or weather coverage never borrows another model's production.
    Historical replay may supply a separate simulated planning origin. The
    forecast creation and provider knowledge times remain the actual ``now``.
    """
    if not models:
        return None
    origin = planning_origin if planning_origin is not None else now
    grid = horizon(origin)
    first, end = grid[0][0], grid[-1][0] + timedelta(hours=grid[-1][1])
    usable = tuple(row for row in weather if row.received_at <= now)
    future = tuple(
        row for row in usable if row.start < end and row.start + timedelta(hours=1) > first
    )
    quarters = tuple(
        row.start + timedelta(minutes=index * FINE_INTERVAL_MINUTES)
        for row in future
        for index in range(QUARTERS_PER_HOUR)
    )
    values = {
        source_id: {
            instant: energy
            for index in range(QUARTERS_PER_HOUR)
            for instant, energy in model.predict(
                future,
                location,
                known_at=now,
                duration_h=QUARTER_HOURS,
                offset_minutes=index * FINE_INTERVAL_MINUTES,
            ).items()
        }
        for source_id, model in models.items()
    }
    rows = []
    for inverter in configuration.inverters:
        sources = [
            source
            for source in configuration.sources
            if source.inverter_id == inverter.inverter_id and source.source_id in models
        ]
        for quarter in quarters:
            dc = {
                source.source_id: values[source.source_id][quarter]
                / (inverter.efficiency if source.measurement_boundary == "ac" else 1)
                for source in sources
            }
            converted = fsum(dc.values()) * inverter.efficiency
            ratio = (
                min(1, inverter.ac_limit_w / 1000 * QUARTER_HOURS / converted) if converted else 1
            )
            rows.extend(
                SolarEnergy(
                    source_id,
                    quarter,
                    QUARTER_HOURS,
                    energy,
                    energy * inverter.efficiency * ratio,
                )
                for source_id, energy in dc.items()
            )
    return SolarForecast(
        configuration.version,
        location,
        now,
        version([row.__dict__ for row in usable]),
        integrate_intervals(rows, origin, tuple(models)),
        model="solar-xgboost-production-v2",
        quality="shadow",
        calibration_versions=tuple(model.version for model in models.values()),
    )
