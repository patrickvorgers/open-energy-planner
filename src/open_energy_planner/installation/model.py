"""Immutable installation settings and solver-neutral multi-inverter planning contracts."""

import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta

from open_energy_planner.appliances.cycles.model import ApplianceWork
from open_energy_planner.appliances.ev.model import EVFlow, EVWork
from open_energy_planner.appliances.thermal.model import (
    ThermalFlow,
    ThermalWork,
)
from open_energy_planner.core.units import FULL_SOC_PERCENT, SECONDS_PER_HOUR
from open_energy_planner.solar.calculation.model import SolarInverter, identity, number, version

from ..optimization.domain import Battery, Costs, Interval, nonnegative
from ..optimization.export_bonus import ExportBonus
from .valuation import TerminalValuation

DEFAULT_MINIMUM_SOC_PERCENT = 10
DEFAULT_CELL_EFFICIENCY = 0.95
MIN_POSITIVE_BATTERY_SETTING = 1e-06
MAX_BATTERY_NUMERIC_SETTING = 1000000.0
MAX_DEVICE_POWER_W = 10000000.0
MAX_EXPORT_BONUSES = 8


@dataclass(frozen=True)
class InstalledBattery:
    """One battery's terminal ratings, sensors and explicit converter connection. Energies are kWh."""

    device_id: str
    inverter_id: str
    soc_entity: str
    power_entity: str
    capacity_kwh: float
    charge_limit_w: float
    discharge_limit_w: float
    minimum_soc: float = DEFAULT_MINIMUM_SOC_PERCENT
    charge_efficiency: float = DEFAULT_CELL_EFFICIENCY
    discharge_efficiency: float = DEFAULT_CELL_EFFICIENCY
    cycle_fee: float = 0
    power_inverted: bool = False
    dc_efficiency: float = 1
    can_follow_grid: bool = False
    energy_entity: str | None = None
    charge_energy_entity: str | None = None
    recover_reserve: bool = False

    def __post_init__(self):
        """Validate finite terminal ratings and keep investment-related wear disabled by default."""
        identity(self.device_id)
        identity(self.inverter_id)
        for energy_entity in (self.energy_entity, self.charge_energy_entity):
            if energy_entity is not None and (
                not isinstance(energy_entity, str)
                or not re.fullmatch(r"sensor\.[a-z0-9_]+", energy_entity)
            ):
                raise ValueError("Battery Energy association requires a sensor")
        if (
            self.charge_energy_entity is not None
            and self.charge_energy_entity == self.energy_entity
        ):
            raise ValueError("Battery charge and discharge meters must differ")
        for entity in (self.soc_entity, self.power_entity):
            if not isinstance(entity, str) or not re.fullmatch(r"sensor\.[a-z0-9_]+", entity):
                raise ValueError("Battery measurements require sensors")
        number(self.minimum_soc, 0, FULL_SOC_PERCENT)
        for name, lower, upper in (
            ("capacity_kwh", MIN_POSITIVE_BATTERY_SETTING, MAX_BATTERY_NUMERIC_SETTING),
            ("charge_limit_w", 0, MAX_DEVICE_POWER_W),
            ("discharge_limit_w", 0, MAX_DEVICE_POWER_W),
            ("charge_efficiency", MIN_POSITIVE_BATTERY_SETTING, 1),
            ("discharge_efficiency", MIN_POSITIVE_BATTERY_SETTING, 1),
            ("cycle_fee", 0, MAX_BATTERY_NUMERIC_SETTING),
            ("minimum_soc", 0, FULL_SOC_PERCENT),
            ("dc_efficiency", MIN_POSITIVE_BATTERY_SETTING, 1),
        ):
            number(getattr(self, name), lower, upper)
            object.__setattr__(self, name, float(getattr(self, name)))
        if type(self.power_inverted) is not bool:
            raise TypeError("Power inversion must be boolean")
        if type(self.can_follow_grid) is not bool:
            raise TypeError("Grid-following permission must be boolean")
        if type(self.recover_reserve) is not bool:
            raise TypeError("Reserve recovery must be boolean")
        self.state(self.minimum_soc)

    def state(self, soc):
        """Retain measured energy below reserve. The model forbids further discharge and owns recovery timing."""
        number(soc, 0, FULL_SOC_PERCENT)
        return Battery(
            self.capacity_kwh,
            self.capacity_kwh * soc / FULL_SOC_PERCENT,
            self.capacity_kwh * self.minimum_soc / FULL_SOC_PERCENT,
            self.charge_limit_w,
            self.discharge_limit_w,
            self.charge_efficiency,
            self.discharge_efficiency,
            self.cycle_fee,
            self.recover_reserve,
        )


@dataclass(frozen=True)
class Installation:
    """One grid with optional independent batteries. PV definitions remain owned by solar configuration."""

    grid_power_entity: str
    import_limit_w: float
    export_limit_w: float
    inverters: tuple[SolarInverter, ...] = ()
    batteries: tuple[InstalledBattery, ...] = ()
    grid_power_inverted: bool = False
    grid_export_entity: str | None = None
    additional_grid_power_entities: tuple[str, ...] = ()
    additional_grid_export_entities: tuple[str, ...] = ()

    def __post_init__(self):
        """Reject ambiguous shared converters, duplicated sensors and invented connection limits."""
        number(self.import_limit_w, 0, MAX_DEVICE_POWER_W)
        number(self.export_limit_w, 0, MAX_DEVICE_POWER_W)
        object.__setattr__(self, "import_limit_w", float(self.import_limit_w))
        object.__setattr__(self, "export_limit_w", float(self.export_limit_w))
        if not isinstance(self.grid_power_entity, str) or not self.grid_power_entity.startswith(
            "sensor."
        ):
            raise ValueError("Grid power sensor required")
        if type(self.grid_power_inverted) is not bool:
            raise TypeError("Grid inversion must be boolean")
        if not isinstance(self.inverters, tuple) or not isinstance(self.batteries, tuple):
            raise TypeError("Installation collections must be immutable")
        ids = {inverter_item.inverter_id: inverter_item for inverter_item in self.inverters}
        if len(ids) != len(self.inverters) or len(
            {battery_item.device_id for battery_item in self.batteries}
        ) != len(self.batteries):
            raise ValueError("Duplicate installation identity")
        if not isinstance(self.additional_grid_power_entities, tuple):
            raise TypeError("Invalid additional grid power sensors")
        if any(
            not isinstance(entity, str) or not re.fullmatch(r"sensor\.[a-z0-9_]+", entity)
            for entity in self.additional_grid_power_entities
        ):
            raise ValueError("Grid measurements require sensors")
        if not isinstance(self.additional_grid_export_entities, tuple) or any(
            not isinstance(entity, str) or not re.fullmatch(r"sensor\.[a-z0-9_]+", entity)
            for entity in self.additional_grid_export_entities
        ):
            raise ValueError("Invalid additional grid export sensors")
        if self.additional_grid_export_entities and self.grid_export_entity is None:
            raise ValueError("Grid export requires its first cumulative meter")
        entities = [
            self.grid_power_entity,
            *self.additional_grid_power_entities,
            *self.additional_grid_export_entities,
        ]
        if self.grid_export_entity is not None:
            if not isinstance(self.grid_export_entity, str) or not re.fullmatch(
                r"sensor\.[a-z0-9_]+", self.grid_export_entity
            ):
                raise ValueError("Grid export requires an energy sensor")
            entities.append(self.grid_export_entity)
        for battery in self.batteries:
            if battery.inverter_id not in ids:
                raise ValueError("Battery inverter reference missing")
            entities.extend((battery.soc_entity, battery.power_entity))
        if len(set(entities)) != len(entities):
            raise ValueError("Measurement roles must be disjoint")

    @property
    def version(self):
        """Bind measurements, settings and plans to the exact installation definition."""
        return version(self.record())

    def record(self):
        """Keep existing immutable configuration hashes unchanged when the new capability is absent."""
        data = asdict(self)
        data["additional_grid_power_entities"] = list(self.additional_grid_power_entities)
        data["additional_grid_export_entities"] = list(self.additional_grid_export_entities)
        if not data["additional_grid_export_entities"]:
            data.pop("additional_grid_export_entities")
        if not data["additional_grid_power_entities"]:
            data.pop("additional_grid_power_entities")
        for battery in data["batteries"]:
            if battery["energy_entity"] is None:
                battery.pop("energy_entity")
            if battery["charge_energy_entity"] is None:
                battery.pop("charge_energy_entity")
            if not battery["can_follow_grid"]:
                battery.pop("can_follow_grid")
            if not battery["recover_reserve"]:
                battery.pop("recover_reserve")
        return data


@dataclass(frozen=True)
class PVUnit:
    """One source's control permission and converter, separate from its interval DC forecast."""

    source_id: str
    inverter_id: str
    can_curtail: bool

    def __post_init__(self):
        """Require stable source/converter IDs and an explicit boolean control permission."""
        identity(self.source_id)
        identity(self.inverter_id)
        if type(self.can_curtail) is not bool:
            raise TypeError("Curtailment permission must be boolean")


@dataclass(frozen=True)
class BatteryState:
    """Bind independent battery state to one physical inverter without aggregating SoC."""

    device_id: str
    inverter_id: str
    battery: Battery
    dc_efficiency: float = 1
    can_follow_grid: bool = False

    def __post_init__(self):
        """Keep each battery's measured state typed and independently identified."""
        identity(self.device_id)
        identity(self.inverter_id)
        if not isinstance(self.battery, Battery):
            raise TypeError("Validated battery state required")
        number(self.dc_efficiency, MIN_POSITIVE_BATTERY_SETTING, 1)
        if type(self.can_follow_grid) is not bool:
            raise TypeError("Grid-following permission must be boolean")


@dataclass(frozen=True)
class InstallationProblem:
    """A common AC bus with local DC buses. Every converter and battery retains its own hard limits."""

    intervals: tuple[Interval, ...]
    inverters: tuple[SolarInverter, ...]
    batteries: tuple[BatteryState, ...]
    pv_sources: tuple[PVUnit, ...]
    grid_import_limit_w: float
    grid_export_limit_w: float
    export_bonuses: tuple[ExportBonus, ...] = ()
    terminal_valuations: tuple[TerminalValuation, ...] = ()
    appliances: tuple[ApplianceWork, ...] = ()
    ev_work: tuple[EVWork, ...] = ()
    reserve_checkpoints: tuple[datetime, ...] = ()
    thermal_work: tuple[ThermalWork, ...] = ()

    def __post_init__(self):
        """Validate immutable installation structure, limits and aligned billing inputs.

        Intervals form a nonempty tuple contiguous in absolute time. Interval and
        physical value objects are assumed to have passed their own validation.
        Grid import/export limits are finite nonnegative watts. Export bonuses
        form a tuple bounded by MAX_EXPORT_BONUSES, containing ExportBonus rules
        with unique IDs and one rate per interval. Each rule owns validation of
        its caps, periods and initial export counter.

        Physical collections are tuples with unique inverter identities, resolved
        inverter references and unambiguous battery/PV command IDs. Every interval
        supplies exactly the declared PV source quantities. Shared PV/battery
        converters require hybrid capability. Work tuples enforce their respective
        type and identity constraints, including interval-aligned thermal
        demand. Reserve checkpoints are unique ordered timezone-aware interval
        ends. Optional terminal valuations match all batteries, configured reserves
        and usable capacity. Validation builds no native model or persistent state.

        Raises:
            ValueError: Interval coverage, grid bounds, bonus shape/alignment/IDs,
                work collections, reserve checkpoints, physical links, PV quantities
                or terminal valuation coverage/bounds violate these contracts.
            TypeError: Physical collections or terminal valuations are not tuples,
                a terminal valuation has the wrong type, or a grid limit is not numeric.
        """
        if not isinstance(self.intervals, tuple) or not self.intervals:
            raise ValueError("At least one immutable interval is required")
        if (
            not isinstance(self.export_bonuses, tuple)
            or len(self.export_bonuses) > MAX_EXPORT_BONUSES
        ):
            raise ValueError("At most eight immutable export bonus rules are supported")
        if any(
            not isinstance(rule, ExportBonus) or len(rule.rates) != len(self.intervals)
            for rule in self.export_bonuses
        ):
            raise ValueError("Export bonuses require aligned billing segments")
        if len({rule.rule_id for rule in self.export_bonuses}) != len(self.export_bonuses):
            raise ValueError("Duplicate export bonus rule")
        for left, right in zip(self.intervals[:-1], self.intervals[1:], strict=True):
            if left.start + timedelta(hours=left.duration_h) != right.start:
                raise ValueError("Intervals must be contiguous in absolute time")
        for name in ("grid_import_limit_w", "grid_export_limit_w"):
            nonnegative(name, getattr(self, name))
        ends = {step.start + timedelta(hours=step.duration_h) for step in self.intervals}
        if (
            not isinstance(self.thermal_work, tuple)
            or any(not isinstance(work, ThermalWork) for work in self.thermal_work)
            or len({work.device_id for work in self.thermal_work}) != len(self.thermal_work)
            or any(len(work.demand_kwh) != len(self.intervals) for work in self.thermal_work)
        ):
            raise ValueError("Invalid thermal storage work")
        if (
            not isinstance(self.reserve_checkpoints, tuple)
            or any(
                not isinstance(end, datetime) or end.utcoffset() is None or end not in ends
                for end in self.reserve_checkpoints
            )
            or tuple(sorted(set(self.reserve_checkpoints))) != self.reserve_checkpoints
        ):
            raise ValueError("Invalid original reserve checkpoints")
        if (
            not isinstance(self.ev_work, tuple)
            or any(not isinstance(work, EVWork) for work in self.ev_work)
            or any(
                len({getattr(work, identity) for work in self.ev_work}) != len(self.ev_work)
                for identity in ("session_id", "point_id", "vehicle_id")
            )
        ):
            raise ValueError("Invalid or multiply connected EV work")
        if (
            not isinstance(self.appliances, tuple)
            or any(not isinstance(work, ApplianceWork) for work in self.appliances)
            or len({work.task_id for work in self.appliances}) != len(self.appliances)
            or len({work.entity_id for work in self.appliances}) != len(self.appliances)
        ):
            raise ValueError("Invalid or overlapping appliance work")
        if not all(
            isinstance(values, tuple)
            for values in (self.inverters, self.batteries, self.pv_sources)
        ):
            raise TypeError("Physical collections must be immutable")
        ids = {inverter_item.inverter_id for inverter_item in self.inverters}
        if len(ids) != len(self.inverters) or any(
            connected_device.inverter_id not in ids
            for connected_device in (*self.batteries, *self.pv_sources)
        ):
            raise ValueError("Invalid physical inverter links")
        if len({battery_item.device_id for battery_item in self.batteries}) != len(self.batteries):
            raise ValueError("Duplicate battery state")
        if not isinstance(self.terminal_valuations, tuple) or any(
            not isinstance(curve, TerminalValuation) for curve in self.terminal_valuations
        ):
            raise TypeError("Immutable terminal valuations required")
        if self.terminal_valuations and (
            len(self.terminal_valuations) != len(self.batteries)
            or {terminal_valuation.device_id for terminal_valuation in self.terminal_valuations}
            != {battery_item.device_id for battery_item in self.batteries}
            or any(
                terminal_valuation.reserve_kwh
                != next(
                    battery_item.battery.minimum_kwh
                    for battery_item in self.batteries
                    if battery_item.device_id == terminal_valuation.device_id
                )
                or terminal_valuation.usable_kwh
                > next(
                    battery_item.battery.capacity_kwh - battery_item.battery.minimum_kwh
                    for battery_item in self.batteries
                    if battery_item.device_id == terminal_valuation.device_id
                )
                for terminal_valuation in self.terminal_valuations
            )
        ):
            raise ValueError("Terminal valuations must match all batteries and reserves")
        if len({pv_source.source_id for pv_source in self.pv_sources}) != len(self.pv_sources) or {
            pv_source.source_id for pv_source in self.pv_sources
        } & {battery_item.device_id for battery_item in self.batteries}:
            raise ValueError("Ambiguous command identities")
        for step in self.intervals:
            if set(dict(step.pv_sources)) != {
                pv_source.source_id for pv_source in self.pv_sources
            } or len(step.pv_sources) != len(self.pv_sources):
                raise ValueError("Per-source PV quantities required")
        for inverter in self.inverters:
            if (
                not inverter.hybrid
                and any(
                    battery_item.inverter_id == inverter.inverter_id
                    for battery_item in self.batteries
                )
                and any(
                    pv_source.inverter_id == inverter.inverter_id for pv_source in self.pv_sources
                )
            ):
                raise ValueError("PV and battery sharing requires a hybrid inverter")


@dataclass(frozen=True)
class BatteryFlow:
    """Charge/discharge at the DC battery terminals and final internal stored energy, all in kWh."""

    device_id: str
    charge_kwh: float
    discharge_kwh: float
    end_kwh: float


@dataclass(frozen=True)
class InverterFlow:
    """AC import/export at one converter. Clipping is physical saturation rather than a curtailment command."""

    inverter_id: str
    import_kwh: float
    export_kwh: float
    clipping: bool


@dataclass(frozen=True)
class PVFlow:
    """A source's used DC energy and explicit on/off command permission outcome."""

    source_id: str
    used_kwh: float
    enabled: bool


@dataclass(frozen=True)
class InstallationFlow:
    """One AC-balanced interval with per-device physical evidence."""

    grid_import: float
    grid_export: float
    batteries: tuple[BatteryFlow, ...]
    inverters: tuple[InverterFlow, ...]
    solar: tuple[PVFlow, ...]
    ev: tuple[EVFlow, ...] = ()
    thermal: tuple[ThermalFlow, ...] = ()


@dataclass(frozen=True)
class InstallationPlan:
    """Retain physical outcomes and independently checkable financial totals."""

    flows: tuple[InstallationFlow, ...]
    costs: Costs
    export_bonus_results: tuple = ()
    appliance_starts: tuple[tuple[str, object], ...] = ()


def slice_interval(step, start, end):
    """Scale every forecast energy at the interval's declared mean rate when billing/time boundaries split it."""
    duration = (end - start).total_seconds() / SECONDS_PER_HOUR
    fraction = duration / step.duration_h
    return replace(
        step,
        start=start,
        duration_h=duration,
        load_kwh=step.load_kwh * fraction,
        pv_kwh=step.pv_kwh * fraction,
        pv_sources=tuple((key, value * fraction) for key, value in step.pv_sources),
    )
