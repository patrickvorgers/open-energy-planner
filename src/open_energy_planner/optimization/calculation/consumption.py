"""Independent grid-import accounting with a conservative terminal stock correction."""

from dataclasses import dataclass

from open_energy_planner.installation.model import InstallationPlan, InstallationProblem
from open_energy_planner.optimization.domain import Battery

CONSUMPTION_PRIORITY_TOLERANCE_KWH = 1e-8
CONSUMPTION_VALIDATION_TOLERANCE_KWH = 1e-7


def battery_inputs(problem: InstallationProblem) -> tuple[tuple[str, Battery, float], ...]:
    """Yield identity, internal battery state and grid-to-internal charging efficiency.

    The correction assumes replacement from the grid, including each converter
    exactly once. It is a terminal accounting assumption, not a charging command.
    """
    inverters = {inverter.inverter_id: inverter for inverter in problem.inverters}
    return tuple(
        (
            battery_item.device_id,
            battery_item.battery,
            battery_item.battery.charge_efficiency
            * battery_item.dc_efficiency
            * inverters[battery_item.inverter_id].efficiency,
        )
        for battery_item in problem.batteries
    )


@dataclass(frozen=True)
class ConsumptionMetrics:
    """Keep real import and hypothetical replacement separate. Every quantity is grid-side kWh."""

    grid_import_kwh: float
    terminal_replacement_kwh: float

    @property
    def corrected_import_kwh(self):
        """Return the primary objective. Surplus stock receives no negative-import credit."""
        return self.grid_import_kwh + self.terminal_replacement_kwh


def evaluate_consumption(
    problem: InstallationProblem, plan: InstallationPlan
) -> ConsumptionMetrics:
    """Recalculate the primary objective from detached flows, independent of native expressions."""
    last = plan.flows[-1]
    ends = {battery_item.device_id: battery_item.end_kwh for battery_item in last.batteries}
    replacement = sum(
        max(0, battery.initial_kwh - ends[key]) / efficiency
        for key, battery, efficiency in battery_inputs(problem)
    )
    return ConsumptionMetrics(sum(flow.grid_import for flow in plan.flows), replacement)
