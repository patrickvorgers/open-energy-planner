"""Compile configured local physics and cached forecasts into complete live planning input."""

from bisect import bisect_right
from datetime import timedelta

from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.home_assistant.observations import InputsUnavailable, PlanningBasis
from open_energy_planner.installation.forecast_policy import FORECAST_MAX_AGE_MINUTES
from open_energy_planner.solar.calculation.model import horizon, version

from ..demand.basis import DemandEvidence
from ..demand.basis import forecast_usable as demand_forecast_usable
from ..optimization.domain import Interval
from ..solar.basis import SolarEvidence, forecast_usable
from .model import BatteryState, InstallationProblem, PVUnit

COVERAGE_TOLERANCE_HOURS = 1e-08


class EnergySeries:
    """Index interval means once so synchronous HA refreshes touch only overlapping forecast rows."""

    def __init__(self, rows, energy_field):
        """Precompute ends/rates and reject overlapping or unordered evidence rather than double-counting it."""
        values = []
        for row in rows:
            end = row.start + timedelta(hours=row.duration_h)
            if values and values[-1][1] > row.start:
                raise InputsUnavailable("installation_forecast_coverage_missing")
            values.append((row.start, end, getattr(row, energy_field) / row.duration_h))
        if not values:
            raise InputsUnavailable("installation_forecast_coverage_missing")
        self.rows = tuple(values)
        self.ends = tuple(row[1] for row in values)
        self.end = self.ends[-1]

    def integral(self, start, end):
        """Integrate declared energy rates exactly over a covered half-open interval, never extrapolating gaps."""
        total = covered = 0.0
        index = bisect_right(self.ends, start)
        while index < len(self.rows) and self.rows[index][0] < end:
            left, right, rate = self.rows[index]
            overlap = (min(end, right) - max(start, left)).total_seconds() / SECONDS_PER_HOUR
            if overlap > 0:
                total += rate * overlap
                covered += overlap
            index += 1
        if (
            start >= end
            or abs(covered - (end - start).total_seconds() / SECONDS_PER_HOUR)
            > COVERAGE_TOLERANCE_HOURS
        ):
            raise InputsUnavailable("installation_forecast_coverage_missing")
        return total


class InstallationBasisSource:
    """Own no network/model work: compose explicit settings, measured state and immutable cached forecasts."""

    def __init__(self, store, demand, solar):
        """Borrow app-owned settings and independent runtime caches."""
        self.store, self.demand, self.solar = store, demand, solar

    def current(self, configuration, now, *, report=None):
        """Reject missing core inputs and compile every converter only at its own AC/DC boundary."""
        settings = self.store.current
        if self.store.issue:
            raise InputsUnavailable(self.store.issue)
        if settings is None:
            return None
        if configuration.revision != settings.version or report is None:
            raise InputsUnavailable("installation_measurements_pending")
        demand, solar = self.demand, self.solar
        if demand.store.issue or demand.configuration is None or demand.forecast is None:
            raise InputsUnavailable("demand_forecast_pending")
        load = demand.forecast
        if not demand_forecast_usable(demand.configuration, load, now):
            raise InputsUnavailable("demand_forecast_expired")
        if load.scope != "residual_household" or load.excluded_devices:
            raise InputsUnavailable("separate_load_planning_not_configured")
        if solar.store.issue or solar.configuration is None:
            raise InputsUnavailable("solar_configuration_required")
        pv = solar.forecast
        if not forecast_usable(solar.configuration, pv, report.location, now):
            raise InputsUnavailable("solar_forecast_unavailable")
        converters = {
            inverter_item.inverter_id: inverter_item for inverter_item in settings.inverters
        }
        for inverter in solar.configuration.inverters:
            if inverter.inverter_id in converters and converters[inverter.inverter_id] != inverter:
                raise InputsUnavailable("inverter_configuration_conflict")
            converters[inverter.inverter_id] = inverter
        readings = {reading.key: reading for reading in report.readings}
        specs = {binding.key: binding for binding in configuration.bindings}
        states = []
        for battery in settings.batteries:
            for name, entity, unit in (
                ("soc", battery.soc_entity, "%"),
                ("power", battery.power_entity, "W"),
            ):
                key = f"battery.{battery.device_id}.{name}"
                if (
                    key not in readings
                    or key not in specs
                    or not specs[key].required
                    or readings[key].entity_id != entity
                    or readings[key].unit != unit
                ):
                    raise InputsUnavailable("battery_sources_required")
            try:
                state = battery.state(readings[f"battery.{battery.device_id}.soc"].value)
            except ValueError:
                raise InputsUnavailable("battery_state_outside_configured_bounds") from None
            if battery.calibration_enabled and self.store.calibration.issue:
                raise InputsUnavailable("battery_calibration_unreadable")
            converter = converters[battery.inverter_id]
            charge, hold_until = (
                self.store.calibration.directive(
                    battery,
                    now,
                    min(
                        battery.charge_limit_w,
                        converter.ac_limit_w * converter.efficiency * battery.dc_efficiency,
                        settings.import_limit_w
                        * converter.efficiency
                        * battery.dc_efficiency
                        / max(1, len(settings.batteries)),
                    ),
                )
                if battery.calibration_enabled
                else (False, None)
            )
            states.append(
                BatteryState(
                    battery.device_id,
                    battery.inverter_id,
                    state,
                    battery.dc_efficiency,
                    battery.can_follow_grid,
                    calibration_charge=charge,
                    calibration_hold_until=hold_until,
                )
            )
        rows = {source.source_id: [] for source in solar.configuration.sources}
        if rows:
            for row in pv.energies:
                if row.source_id in rows:
                    rows[row.source_id].append(row)
        if any(not values for values in rows.values()):
            raise InputsUnavailable("installation_forecast_coverage_missing")
        load_series = EnergySeries(load.steps, "kwh")
        pv_series = {key: EnergySeries(values, "dc_kwh") for key, values in rows.items()}
        end = min([load_series.end, *(series.end for series in pv_series.values())])
        intervals = []
        for start, duration in horizon(now):
            left, right = max(now, start), min(end, start + timedelta(hours=duration))
            if left >= right:
                continue
            values = tuple((key, series.integral(left, right)) for key, series in pv_series.items())
            intervals.append(
                Interval(
                    left,
                    (right - left).total_seconds() / SECONDS_PER_HOUR,
                    0,
                    0,
                    load_series.integral(left, right),
                    sum(value for _, value in values),
                    pv_sources=values,
                )
            )
        if not intervals:
            raise InputsUnavailable("installation_forecast_coverage_missing")
        problem = InstallationProblem(
            tuple(intervals),
            tuple(converters.values()),
            tuple(states),
            tuple(
                PVUnit(source.source_id, source.inverter_id, source.can_curtail)
                for source in solar.configuration.sources
            ),
            settings.import_limit_w,
            settings.export_limit_w,
        )
        solar_evidence = (
            (
                SolarEvidence(
                    pv.version,
                    pv.configuration_version,
                    pv.weather_version,
                    tuple(rows),
                    pv.model,
                    pv.quality,
                ),
            )
            if rows
            else ()
        )
        deadline = min(
            load.issued_at + timedelta(minutes=FORECAST_MAX_AGE_MINUTES),
            pv.issued_at + timedelta(minutes=FORECAST_MAX_AGE_MINUTES)
            if rows
            else load.issued_at + timedelta(minutes=FORECAST_MAX_AGE_MINUTES),
        )
        return PlanningBasis(
            configuration.revision,
            version(
                (
                    settings.version,
                    load.version,
                    pv.version if rows else solar.configuration.version,
                    tuple(
                        (
                            unit.device_id,
                            unit.calibration_charge,
                            unit.calibration_hold_until.isoformat()
                            if unit.calibration_hold_until
                            else None,
                        )
                        for unit in states
                    ),
                )
            ),
            "contract_required",
            deadline,
            problem,
            demand_evidence=(
                DemandEvidence(
                    load.version,
                    load.configuration_version,
                    load.training_version,
                    load.model,
                    load.quality,
                    load.excluded_devices,
                ),
            ),
            solar_evidence=solar_evidence,
        )
