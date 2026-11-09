"""Exact native aggregate-export cap model. No supplier, clock or storage knowledge."""

from dataclasses import dataclass

from open_energy_planner.core.units import WATTS_PER_KW

from ...export_bonus import ExportBonus


@dataclass(frozen=True)
class ExportBonusModel:
    """Count all grid export and credit remaining quota with exact max/min constraints."""

    bonus: ExportBonus
    export_limit_w: float

    @property
    def device_id(self):
        """Namespace native bindings independently of physical device identities."""
        return "export_bonus:" + self.bonus.rule_id

    def build(self, context, routes):
        """Build chronological per-segment counters and return the separate extra revenue expression."""
        binding = context.register(self.device_id)
        volumes = tuple(
            self.export_limit_w * step.duration_h / WATTS_PER_KW for step in context.intervals
        )
        context.variables(binding, "counter", ub=self.bonus.initial_kwh + sum(volumes))
        context.variables(binding, "remaining", ub=max(self.bonus.caps_kwh))
        context.variables(binding, "credit", ub=max(volumes))
        for name in ("positive", "minimum"):
            context.variables(binding, name, binary=True)
        variables, add_constraint = binding.variables, context.native.addConstr
        previous_low = previous_high = self.bonus.initial_kwh
        for interval_index, volume_bound in enumerate(volumes):
            if (
                interval_index
                and self.bonus.periods[interval_index] != self.bonus.periods[interval_index - 1]
            ):
                previous_low = previous_high = 0
            previous = (
                variables["counter"][interval_index - 1]
                if interval_index
                and self.bonus.periods[interval_index] == self.bonus.periods[interval_index - 1]
                else self.bonus.initial_kwh
                if interval_index == 0
                else 0
            )
            volume = routes["pv_grid"][interval_index] + routes["battery_grid"][interval_index]
            add_constraint(variables["counter"][interval_index] == previous + volume)
            remaining, positive = (
                variables["remaining"][interval_index],
                variables["positive"][interval_index],
            )
            cap = self.bonus.caps_kwh[interval_index]
            delta = cap - previous
            credit, branch = (
                variables["credit"][interval_index],
                variables["minimum"][interval_index],
            )
            if cap <= previous_low:
                add_constraint(positive == 0)
                add_constraint(remaining == 0)
                add_constraint(credit == 0)
            elif cap >= previous_high + volume_bound:
                # Every reachable export fits. No annual-cap selector is needed.
                add_constraint(positive == 1)
                add_constraint(remaining == delta)
                add_constraint(credit == volume)
            else:
                # Bounds are constants from reachable period volume, never
                # max/min of a native expression or the annual cap plus horizon.
                remaining_bound = max(0, cap - previous_low)
                negative_delta_bound = max(0, previous_high - cap)
                add_constraint(remaining >= delta)
                add_constraint(remaining <= delta + negative_delta_bound * (1 - positive))
                add_constraint(remaining <= remaining_bound * positive)
                add_constraint(credit <= volume)
                add_constraint(credit <= remaining)
                # Keep near-zero selector slack on the interval scale rather
                # than amplifying it by the remaining annual quota.
                add_constraint(credit <= volume_bound * positive)
                add_constraint(credit >= volume - volume_bound * (1 - branch))
                add_constraint(credit >= remaining - remaining_bound * branch)
            previous_high += volume_bound
        return sum(
            self.bonus.rates[interval_index_entry] * variables["credit"][interval_index_entry]
            for interval_index_entry in range(len(context.intervals))
        )
