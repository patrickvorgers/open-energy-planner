"""Compatible original converter bounds expressed through current physical devices."""

from dataclasses import dataclass
from fractions import Fraction

from open_energy_planner.installation.model import InstallationProblem
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.domain import Battery
from tests.app.solver_regression.cases import installation, intervals


@dataclass(frozen=True)
class TopologyCase:
    """Retain T01/T04/T05/T07/T08's independently derived converter limits and losses.

    T01 exports .9 cubed=.729. T04 exports .9 PV plus .9 battery=1.8.
    T05 stores .98 without AC exchange, worth .49. T07 converts .9 PV
    AC plus .1 imported AC to .9 terminal energy. T08 reserves .25 load
    from a 1-kWh converter output, leaving .75 export.
    """

    name: str
    problem: InstallationProblem
    cash: Fraction
    terminal: Fraction
    imports: Fraction
    exports: Fraction
    final_energy: Fraction | None = None

    @property
    def objective(self):
        """Keep the original independently specified cash minus usable stock value."""
        return self.cash - self.terminal


TOPOLOGY_CASES = (
    TopologyCase(
        "T01-separate-ac-losses",
        installation(
            (intervals((0.6,), (0,), pv=1)[0], intervals((0.6, 0.6), (0, 0.4))[1]),
            battery=Battery(2, 0),
            pv_efficiency=0.9,
            battery_efficiency=0.9,
        ),
        Fraction("-.2916"),
        Fraction(0),
        Fraction(0),
        Fraction(".729"),
        Fraction(0),
    ),
    TopologyCase(
        "T04-separate-output",
        installation(
            intervals((0.6,), (0.4,), pv=1),
            battery=Battery(1, 1, charge_limit_w=0),
            pv_efficiency=0.9,
            battery_efficiency=0.9,
            export_limit=8000,
            can_curtail=True,
        ),
        Fraction("-.72"),
        Fraction(0),
        Fraction(0),
        Fraction("1.8"),
        Fraction(0),
    ),
    TopologyCase(
        "T05-current-direct-dc-without-ac-exchange",
        installation(
            intervals((0.6,), (0.4,), pv=1),
            battery=Battery(2, 0),
            hybrid=True,
            pv_efficiency=0.9,
            dc_efficiency=0.98,
            pv_rating=1,
            can_curtail=True,
            terminal_rate=0.5,
            import_limit=0,
            export_limit=0,
        ),
        Fraction(0),
        Fraction(".49"),
        Fraction(0),
        Fraction(0),
        Fraction(".98"),
    ),
    TopologyCase(
        "T07-separate-battery-input",
        installation(
            intervals((-0.2,), (-0.3,), pv=1),
            battery=Battery(2, 0, charge_limit_w=8000),
            pv_efficiency=0.9,
            battery_efficiency=0.9,
        ),
        Fraction("-.02"),
        Fraction(0),
        Fraction(".1"),
        Fraction(0),
        Fraction(".9"),
    ),
    TopologyCase(
        "T08-pv-rating",
        installation(
            intervals((0.6,), (0.4,), pv=2, load=0.25), pv_efficiency=0.9, can_curtail=True
        ),
        Fraction("-.3"),
        Fraction(0),
        Fraction(0),
        Fraction(".75"),
    ),
)


def check_topology_plan(case, plan):
    """Check actual AC totals and terminal energy against the unchanged original scenario constants."""
    costs = validate(case.problem, plan)
    for actual, expected in (
        (costs.cash_cost, case.cash),
        (costs.terminal_value, case.terminal),
        (costs.objective, case.objective),
        (sum(flow.grid_import for flow in plan.flows), case.imports),
        (sum(flow.grid_export for flow in plan.flows), case.exports),
    ):
        assert abs(actual - float(expected)) <= 1e-6, (case.name, actual, expected)
    if case.final_energy is not None:
        assert abs(plan.flows[-1].batteries[0].end_kwh - float(case.final_energy)) <= 1e-6
    return costs
