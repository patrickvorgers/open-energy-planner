"""pvlib adapter: physical solar yield, hourly weather projection and shared AC clipping."""

from datetime import timedelta

import numpy as np
import pandas as pd
from pvlib import irradiance, pvsystem, solarposition, temperature

from open_energy_planner.core.forecast_timing import limited_slope
from open_energy_planner.core.units import MINUTES_PER_HOUR, WATTS_PER_KW
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES
from open_energy_planner.solar.calculation.model import SolarEnergy, SolarForecast, horizon, version
from open_energy_planner.solar.calculation.projection import integrate_intervals
from open_energy_planner.solar.model_policy import (
    DEFAULT_GROUND_ALBEDO,
    DEFAULT_TEMPERATURE_COEFFICIENT_MAGNITUDE,
)

HORIZON_ZENITH_DEGREES = 90
COVERAGE_TOLERANCE_HOURS = 1e-08
MIN_CONVERSION_DENOMINATOR = 1e-12
SOLAR_INTERVAL_HOURS = FINE_INTERVAL_MINUTES / MINUTES_PER_HOUR
SOLAR_STEPS_PER_HOUR = MINUTES_PER_HOUR // FINE_INTERVAL_MINUTES


class PVWattsPredictor:
    """Compute available DC independently of dispatched battery/curtailment commands.

    AC output is for comparison with standalone production meters. Hybrid plans
    must use DC and their shared routes rather than treating this AC as additional
    production. Tilt 30 degrees, temperature coefficient -0.004/C, isotropic sky,
    albedo 0.2 and equal east/west capacity are declared startup assumptions.
    """

    def hourly(self, configuration, location, weather, *, factors=None):
        """Aggregate quarter kW arrays into hourly kWh before constructing calibration rows."""
        dc_power_kw, ac_power_kw = self._power_series(
            configuration, location, weather, factors=factors
        )
        dc_energy_kwh = {
            source_id: quarter_power_kw.reshape(-1, SOLAR_STEPS_PER_HOUR).sum(axis=1)
            * SOLAR_INTERVAL_HOURS
            for source_id, quarter_power_kw in dc_power_kw.items()
        }
        ac_energy_kwh = {
            source_id: quarter_power_kw.reshape(-1, SOLAR_STEPS_PER_HOUR).sum(axis=1)
            * SOLAR_INTERVAL_HOURS
            for source_id, quarter_power_kw in ac_power_kw.items()
        }
        return tuple(
            SolarEnergy(
                source.source_id,
                weather_hour.start,
                1,
                float(dc_energy_kwh[source.source_id][hour_index]),
                float(ac_energy_kwh[source.source_id][hour_index]),
            )
            for source in configuration.sources
            for hour_index, weather_hour in enumerate(weather)
        )

    def quarters(self, configuration, location, weather, *, factors=None, hourly_factors=None):
        """Integrate quarter kW arrays into kWh only for callers requiring detailed intervals."""
        dc_power_kw, ac_power_kw = self._power_series(
            configuration, location, weather, factors=factors, hourly_factors=hourly_factors
        )
        return tuple(
            SolarEnergy(
                source.source_id,
                weather_hour.start + timedelta(minutes=quarter_index * FINE_INTERVAL_MINUTES),
                SOLAR_INTERVAL_HOURS,
                float(
                    dc_power_kw[source.source_id][hour_index * SOLAR_STEPS_PER_HOUR + quarter_index]
                )
                * SOLAR_INTERVAL_HOURS,
                float(
                    ac_power_kw[source.source_id][hour_index * SOLAR_STEPS_PER_HOUR + quarter_index]
                )
                * SOLAR_INTERVAL_HOURS,
            )
            for source in configuration.sources
            for hour_index, weather_hour in enumerate(weather)
            for quarter_index in range(SOLAR_STEPS_PER_HOUR)
        )

    def _power_series(self, configuration, location, weather, *, factors=None, hourly_factors=None):
        """Keep quarter kW arrays inside pvlib's adapter. Hourly history avoids temporary energy objects."""
        if not weather:
            return {}, {}
        samples = tuple(
            (weather_item, quarter_index)
            for weather_item in weather
            for quarter_index in range(SOLAR_STEPS_PER_HOUR)
        )
        times = pd.DatetimeIndex(
            [
                row.start + timedelta(minutes=(quarter_index + 0.5) * FINE_INTERVAL_MINUTES)
                for row, quarter_index in samples
            ]
        )
        sun = solarposition.get_solarposition(times, location.latitude, location.longitude)
        by_hour = {row.start: row for row in weather}

        def irradiance_values(key):
            """Estimate quarter irradiance from adjacent hourly means without creating cloud observations."""
            values = []
            for row, quarter_index in samples:
                previous = by_hour.get(row.start - timedelta(hours=1), row)
                following = by_hour.get(row.start + timedelta(hours=1), row)
                value = getattr(row, key)
                slope = limited_slope(getattr(previous, key), value, getattr(following, key))
                values.append(value + slope * ((quarter_index + 0.5) / SOLAR_STEPS_PER_HOUR - 0.5))
            return np.array(values)

        ghi, dni, dhi = (irradiance_values(key) for key in ("ghi", "dni", "dhi"))
        air, wind = (
            np.array([getattr(row, key) for row, _ in samples]) for key in ("celsius", "wind_ms")
        )
        dc_power_kw = {}
        for source in configuration.sources:
            powers = np.zeros(len(samples))
            for panel in source.geometry:
                poa = irradiance.get_total_irradiance(
                    panel.tilt,
                    panel.azimuth,
                    sun.apparent_zenith,
                    sun.azimuth,
                    dni,
                    ghi,
                    dhi,
                    model="isotropic",
                    albedo=DEFAULT_GROUND_ALBEDO,
                )["poa_global"]
                poa = np.where(sun.apparent_zenith < HORIZON_ZENITH_DEGREES, np.maximum(0, poa), 0)
                cell = temperature.faiman(poa, air, wind)
                powers += np.maximum(
                    0,
                    pvsystem.pvwatts_dc(
                        poa,
                        cell,
                        panel.capacity_w,
                        -DEFAULT_TEMPERATURE_COEFFICIENT_MAGNITUDE,
                    ),
                )
            if not np.isfinite(powers).all():
                raise ValueError("Nonfinite solar model output")
            correction = (factors or {}).get(source.source_id, 1)
            if hourly_factors:
                correction = np.array(
                    [
                        hourly_factors.get((source.source_id, row.start), correction)
                        for row, _quarter in samples
                    ]
                )
            dc_power_kw[source.source_id] = powers / WATTS_PER_KW * correction
        ac_power_kw = {}
        for inverter in configuration.inverters:
            sources = [
                source_item
                for source_item in configuration.sources
                if source_item.inverter_id == inverter.inverter_id
            ]
            total = sum(
                (dc_power_kw[source_item.source_id] for source_item in sources),
                np.zeros(len(samples)),
            )
            converted = total * inverter.efficiency
            ratio = np.minimum(
                1,
                inverter.ac_limit_w
                / WATTS_PER_KW
                / np.maximum(converted, MIN_CONVERSION_DENOMINATOR),
            )
            for source in sources:
                ac_power_kw[source.source_id] = (
                    dc_power_kw[source.source_id] * inverter.efficiency * ratio
                )
        return dc_power_kw, ac_power_kw

    def forecast(
        self, configuration, location, weather, now, *, calibrations=(), hourly_factors=None
    ):
        """Integrate physical quarter estimates onto the complete mixed grid without inventing finer weather."""
        usable = tuple(weather_item for weather_item in weather if weather_item.received_at <= now)
        grid = horizon(now)
        first, end = grid[0][0], grid[-1][0] + timedelta(hours=grid[-1][1])
        prediction_weather = tuple(
            weather_item
            for weather_item in usable
            if weather_item.start < end and weather_item.start + timedelta(hours=1) > first
        )
        hours = self.quarters(
            configuration,
            location,
            prediction_weather,
            factors={calibration.source_id: calibration.factor for calibration in calibrations},
            hourly_factors=hourly_factors,
        )
        energies = integrate_intervals(
            hours, now, tuple(source.source_id for source in configuration.sources)
        )
        return SolarForecast(
            configuration.version,
            location,
            now,
            version([weather_item.__dict__ for weather_item in usable]),
            tuple(energies),
            model="pvwatts-calibrated-v2" if calibrations else "pvwatts-v2",
            quality="shadow" if calibrations else "provisional",
            calibration_versions=tuple(
                version(calibration.__dict__) for calibration in calibrations
            ),
        )
