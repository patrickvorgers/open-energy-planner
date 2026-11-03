"""Focused model components for common AC conservation and independent local DC devices."""

from dataclasses import dataclass

import highspy

from open_energy_planner.core.units import WATTS_PER_KW
from open_energy_planner.installation.model import BatteryState, InstallationProblem, PVUnit
from open_energy_planner.solar.calculation.model import SolarInverter


def allocate(context, identity, names, binaries=()):
    """Allocate one model-owned binding. Native expressions never escape this solver adapter."""
    binding = context.register(identity)
    for name in names:
        context.variables(binding, name, binary=name in binaries)
    return binding


def controls(intervals):
    """Resolve first billing segment per control interval without coupling equal-length unrelated steps."""
    groups = {}
    return tuple(
        groups.setdefault(step.control_group, interval_index)
        if step.control_group
        else interval_index
        for interval_index, step in enumerate(intervals)
    )


@dataclass(frozen=True)
class BusGrid:
    """Own total connection limits, import/export exclusivity and AC-bus conservation."""

    problem: InstallationProblem

    def declare_variables(self, context):
        """Declare exactly one pair of aggregate grid routes and direction selectors."""
        return allocate(context, "grid", ("import", "export", "direction"), ("direction",))

    def add_constraints(self, context, binding):
        """Join all inverter AC ports once while enforcing whole-installation power ratings."""
        from .appliances import energy
        from .ev import energy as ev_energy
        from .thermal import energy as thermal_energy

        context.require_binding(binding, "grid")
        grid = binding.variables
        add_constraint = context.native.addConstr
        converters = [
            context.binding("inv:" + inverter_item.inverter_id).variables
            for inverter_item in self.problem.inverters
        ]
        for interval_index, step in enumerate(context.intervals):
            duration_hours = step.duration_h
            add_constraint(
                grid["import"][interval_index]
                <= self.problem.grid_import_limit_w
                * duration_hours
                / WATTS_PER_KW
                * grid["direction"][interval_index]
            )
            add_constraint(
                grid["export"][interval_index]
                <= self.problem.grid_export_limit_w
                * duration_hours
                / WATTS_PER_KW
                * (1 - grid["direction"][interval_index])
            )
            add_constraint(
                grid["import"][interval_index]
                + sum(value["export"][interval_index] for value in converters)
                == step.load_kwh
                + energy(context, self.problem, interval_index)
                + ev_energy(context, self.problem, interval_index)
                + thermal_energy(context, self.problem, interval_index)
                + grid["export"][interval_index]
                + sum(value["import"][interval_index] for value in converters)
            )


@dataclass(frozen=True)
class BusPV:
    """Own source availability and permitted on/off commands, distinct from physical clipping."""

    source: PVUnit

    def declare_variables(self, context):
        """Declare one source's used DC energy and proposed enabled state."""
        return allocate(context, "pv:" + self.source.source_id, ("used", "on"), ("on",))

    def add_constraints(self, context, binding):
        """Allow reduced fixed-source production only when its actual converter/storage path saturates."""
        context.require_binding(binding, "pv:" + self.source.source_id)
        add_constraint = context.native.addConstr
        variables = binding.variables
        clipping = context.binding("inv:" + self.source.inverter_id).variables["clip"]
        for interval_index, (step, first) in enumerate(
            zip(context.intervals, controls(context.intervals), strict=True)
        ):
            available = dict(step.pv_sources)[self.source.source_id]
            add_constraint(
                variables["used"][interval_index] <= available * variables["on"][interval_index]
            )
            add_constraint(
                variables["used"][interval_index]
                >= available * (variables["on"][interval_index] - clipping[interval_index])
            )
            if not self.source.can_curtail:
                add_constraint(variables["on"][interval_index] == 1)
            if first != interval_index:
                add_constraint(variables["on"][interval_index] == variables["on"][first])


@dataclass(frozen=True)
class BusBattery:
    """Own terminal limits, internal energy, reserve and exact whole-watt dispatch for one battery."""

    unit: BatteryState

    def declare_variables(self, context):
        """Keep all storage variables isolated under this battery's stable identity."""
        return allocate(
            context,
            "bat:" + self.unit.device_id,
            ("charge", "discharge", "energy", "direction", "full"),
            ("direction", "full"),
        )

    def add_constraints(self, context, binding):
        """Apply storage losses once, and distinguish full storage from a voluntary charging stop during clipping."""
        context.require_binding(binding, "bat:" + self.unit.device_id)
        battery, variables = self.unit.battery, binding.variables
        add_constraint = context.native.addConstr
        clipping = context.binding("inv:" + self.unit.inverter_id).variables["clip"]
        for interval_index, (step, first) in enumerate(
            zip(context.intervals, controls(context.intervals), strict=True)
        ):
            duration_hours = step.duration_h
            previous = (
                variables["energy"][interval_index - 1] if interval_index else battery.initial_kwh
            )
            add_constraint(
                variables["charge"][interval_index]
                <= battery.charge_limit_w
                * duration_hours
                / WATTS_PER_KW
                * variables["direction"][interval_index]
            )
            add_constraint(
                variables["discharge"][interval_index]
                <= battery.discharge_limit_w
                * duration_hours
                / WATTS_PER_KW
                * (1 - variables["direction"][interval_index])
            )
            add_constraint(
                variables["energy"][interval_index]
                == previous
                + battery.charge_efficiency * variables["charge"][interval_index]
                - variables["discharge"][interval_index] / battery.discharge_efficiency
            )
            add_constraint(
                variables["energy"][interval_index] >= min(battery.minimum_kwh, battery.initial_kwh)
            )
            if battery.initial_kwh < battery.minimum_kwh:
                add_constraint(
                    variables["discharge"][interval_index] / battery.discharge_efficiency
                    <= previous
                    - battery.minimum_kwh
                    + battery.minimum_kwh * variables["direction"][interval_index]
                )
            add_constraint(variables["energy"][interval_index] <= battery.capacity_kwh)
            add_constraint(
                variables["energy"][interval_index]
                >= battery.capacity_kwh * variables["full"][interval_index]
            )
            add_constraint(
                variables["charge"][interval_index]
                >= battery.charge_limit_w
                * duration_hours
                / WATTS_PER_KW
                * (clipping[interval_index] - variables["full"][interval_index])
            )
            power = context.native.addVariable(
                lb=-battery.charge_limit_w,
                ub=battery.discharge_limit_w,
                type=highspy.HighsVarType.kInteger,
                name=f"power:{self.unit.device_id}[{interval_index}]",
            )
            add_constraint(
                power
                == (variables["discharge"][interval_index] - variables["charge"][interval_index])
                * WATTS_PER_KW
                / duration_hours
            )
            if first != interval_index:
                for name in ("charge", "discharge"):
                    add_constraint(
                        variables[name][interval_index] * context.intervals[first].duration_h
                        == variables[name][first] * duration_hours
                    )


@dataclass(frozen=True)
class BusInverter:
    """Own a local DC-bus balance, one-time conversion and a shared bidirectional AC rating."""

    inverter: SolarInverter
    batteries: tuple[BatteryState, ...]
    sources: tuple[PVUnit, ...]

    def declare_variables(self, context):
        """Declare physical AC ports, converter direction and saturation evidence."""
        return allocate(
            context,
            "inv:" + self.inverter.inverter_id,
            ("import", "export", "direction", "clip"),
            ("direction", "clip"),
        )

    def add_constraints(self, context, binding):
        """Direct PV-to-battery DC stays local and never consumes shared AC throughput."""
        context.require_binding(binding, "inv:" + self.inverter.inverter_id)
        variables = binding.variables
        add_constraint = context.native.addConstr
        pv = [
            context.binding("pv:" + source_item.source_id).variables for source_item in self.sources
        ]
        batteries = [
            (context.binding("bat:" + battery.device_id).variables, battery.dc_efficiency)
            for battery in self.batteries
        ]
        for interval_index, step in enumerate(context.intervals):
            cap = self.inverter.ac_limit_w * step.duration_h / WATTS_PER_KW
            add_constraint(
                variables["import"][interval_index] <= cap * variables["direction"][interval_index]
            )
            add_constraint(
                variables["export"][interval_index]
                <= cap * (1 - variables["direction"][interval_index])
            )
            add_constraint(
                variables["export"][interval_index] >= cap * variables["clip"][interval_index]
            )
            add_constraint(
                sum(source_item["used"][interval_index] for source_item in pv)
                + sum(
                    battery_variables["discharge"][interval_index] * efficiency
                    for battery_variables, efficiency in batteries
                )
                + variables["import"][interval_index] * self.inverter.efficiency
                == sum(
                    battery_variables["charge"][interval_index] / efficiency
                    for battery_variables, efficiency in batteries
                )
                + variables["export"][interval_index] / self.inverter.efficiency
            )
