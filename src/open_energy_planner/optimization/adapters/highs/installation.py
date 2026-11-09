"""Compose multi-inverter DC buses behind the existing HiGHS solver lifecycle."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from open_energy_planner.core.units import WATTS_PER_KW

from ....installation.model import (
    BatteryFlow,
    InstallationFlow,
    InstallationPlan,
    InverterFlow,
    PVFlow,
)
from ...domain import Costs
from ...export_bonus import ExportBonusResult
from .context import HighsModelContext

if TYPE_CHECKING:
    from ....installation.model import InstallationProblem
from .export_bonus import ExportBonusModel


@dataclass
class BuiltInstallationModel:
    """Own native bindings for one installation. Extraction never certifies its physical correctness."""

    context: HighsModelContext
    problem: "InstallationProblem"
    financial: tuple
    bonus_ids: tuple

    def extract(self):
        """Detach finite candidate energies into plain per-device flow records."""
        model_context, installation_problem = self.context, self.problem
        values = model_context.native.getSolution().col_value

        def value(device, field, interval_index):
            """Resolve one native variable only within this model's owned binding."""
            return float(
                values[int(model_context.binding(device).variables[field][interval_index])]
            )

        from .ev import extract as extract_ev
        from .thermal import extract as extract_thermal

        flows = tuple(
            InstallationFlow(
                value("grid", "import", interval_index),
                value("grid", "export", interval_index),
                tuple(
                    BatteryFlow(
                        battery_item.device_id,
                        value("bat:" + battery_item.device_id, "charge", interval_index),
                        value("bat:" + battery_item.device_id, "discharge", interval_index),
                        value("bat:" + battery_item.device_id, "energy", interval_index),
                    )
                    for battery_item in installation_problem.batteries
                ),
                tuple(
                    InverterFlow(
                        inverter.inverter_id,
                        value("inv:" + inverter.inverter_id, "import", interval_index),
                        value("inv:" + inverter.inverter_id, "export", interval_index),
                        value("inv:" + inverter.inverter_id, "clip", interval_index) > 0.5,
                    )
                    for inverter in installation_problem.inverters
                ),
                tuple(
                    PVFlow(
                        pv_source.source_id,
                        value("pv:" + pv_source.source_id, "used", interval_index),
                        value("pv:" + pv_source.source_id, "on", interval_index) > 0.5,
                    )
                    for pv_source in installation_problem.pv_sources
                ),
                extract_ev(model_context, installation_problem, interval_index, values),
                extract_thermal(model_context, installation_problem, interval_index, values),
            )
            for interval_index in range(len(installation_problem.intervals))
        )
        costs = Costs(
            *(
                float(input_value)
                if isinstance(input_value, (float, int))
                else float(model_context.native.val(input_value))
                for input_value in self.financial
            )
        )
        # Complete continuous valuation allocation for feasible time-limited
        # incumbents too. It cannot change any physical command or cash flow.
        final = {
            battery_item.device_id: battery_item.end_kwh for battery_item in flows[-1].batteries
        }
        terminal = sum(
            curve.value(final[curve.device_id])
            for curve in installation_problem.terminal_valuations
        )
        costs = Costs(
            costs.import_cost,
            costs.export_income,
            costs.bonus_income,
            costs.wear_cost,
            costs.cash_cost,
            terminal,
            costs.cash_cost - terminal,
        )
        bonuses = tuple(
            ExportBonusResult(
                identity.removeprefix("export_bonus:"),
                tuple(
                    value(identity, "credit", interval_index)
                    for interval_index in range(len(installation_problem.intervals))
                ),
                tuple(
                    value(identity, "counter", interval_index)
                    for interval_index in range(len(installation_problem.intervals))
                ),
            )
            for identity in self.bonus_ids
        )
        from .appliances import extract

        return InstallationPlan(flows, costs, bonuses, extract(model_context, installation_problem))


class InstallationModelFactory:
    """Compose independent converters, storage and PV around one bounded AC grid connection."""

    def build(self, problem: "InstallationProblem") -> BuiltInstallationModel:
        """Declare model-local variables, then add bus conservation, saturation, command and cost constraints."""
        context = HighsModelContext(problem.intervals)
        from .appliances import declare

        declare(context, problem)
        from .ev import declare as declare_ev

        declare_ev(context, problem)
        from .thermal import declare as declare_thermal

        declare_thermal(context, problem)
        from .installation_components import BusBattery, BusGrid, BusInverter, BusPV

        components = [BusGrid(problem)]
        components.extend(
            BusInverter(
                inverter,
                tuple(
                    battery_item
                    for battery_item in problem.batteries
                    if battery_item.inverter_id == inverter.inverter_id
                ),
                tuple(
                    pv_source
                    for pv_source in problem.pv_sources
                    if pv_source.inverter_id == inverter.inverter_id
                ),
            )
            for inverter in problem.inverters
        )
        components.extend(BusBattery(battery_item) for battery_item in problem.batteries)
        components.extend(BusPV(pv_source) for pv_source in problem.pv_sources)
        bindings = [(component, component.declare_variables(context)) for component in components]
        for component, binding in bindings:
            component.add_constraints(context, binding)
        grid = context.binding("grid").variables
        batteries = {
            battery_item.device_id: context.binding("bat:" + battery_item.device_id).variables
            for battery_item in problem.batteries
        }
        models = tuple(
            ExportBonusModel(export_bonus, problem.grid_export_limit_w)
            for export_bonus in problem.export_bonuses
        )
        revenue = sum(
            bonus_model.build(
                context, {"pv_grid": grid["export"], "battery_grid": (0,) * len(problem.intervals)}
            )
            for bonus_model in models
        )
        for bonus_model in models:
            positive = context.binding(bonus_model.device_id).variables["positive"]
            for interval_index, protected in enumerate(bonus_model.bonus.prevent_battery_export):
                if not protected:
                    continue
                # Starting quota prevents battery export from exhausting its own guard.
                for unit in problem.batteries:
                    context.native.addConstr(
                        batteries[unit.device_id]["discharge"][interval_index]
                        <= unit.battery.discharge_limit_w
                        * problem.intervals[interval_index].duration_h
                        / WATTS_PER_KW
                        * (grid["direction"][interval_index] + 1 - positive[interval_index])
                    )
        purchase = sum(
            interval.buy_price * grid["import"][interval_index]
            for interval_index, interval in enumerate(problem.intervals)
        )
        sale = sum(
            interval.sell_price * grid["export"][interval_index]
            for interval_index, interval in enumerate(problem.intervals)
        )
        wear = sum(
            battery_item.battery.cycle_fee
            / battery_item.battery.capacity_kwh
            / battery_item.battery.discharge_efficiency
            * batteries[battery_item.device_id]["discharge"][interval_index]
            for battery_item in problem.batteries
            for interval_index in range(len(problem.intervals))
        )
        cash = purchase - sale - revenue + wear
        terminal = 0
        for valuation in problem.terminal_valuations:
            energy = context.native.addVariable(
                lb=0,
                ub=valuation.usable_kwh,
                name=f"terminal:{valuation.device_id}",
            )
            battery = next(
                battery_item.battery
                for battery_item in problem.batteries
                if battery_item.device_id == valuation.device_id
            )
            end = batteries[valuation.device_id]["energy"][-1]
            if battery.initial_kwh < battery.minimum_kwh:
                eligible = context.native.addBinary()
                context.native.addConstr(energy <= valuation.usable_kwh * eligible)
                context.native.addConstr(
                    energy <= end - valuation.reserve_kwh + valuation.reserve_kwh * (1 - eligible)
                )
            else:
                context.native.addConstr(energy <= end - valuation.reserve_kwh)
            terminal += valuation.rate * energy
        context.native.setObjective(cash - terminal)
        return BuiltInstallationModel(
            context,
            problem,
            (purchase, sale, revenue, wear, cash, terminal, cash - terminal),
            tuple(bonus_model.device_id for bonus_model in models),
        )
