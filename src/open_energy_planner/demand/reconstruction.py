"""Reconstruct residual household energy only where every required measurement is present."""

from dataclasses import dataclass
from datetime import timedelta

from open_energy_planner.demand.calculation.model import DemandHour

METER_ROUNDING_ALLOWANCE_KWH = 0.001
BALANCE_NUMERICAL_TOLERANCE_KWH = 1e-07
MAX_REJECTED_TRAINING_HOURS = 24
MIN_NOTIFICATION_HISTORY_HOURS = 24
REJECTED_HOURS_NOTIFICATION_PERCENT = 20


@dataclass(frozen=True)
class ReconstructionResult:
    """Keep training-only filtering counts separate from immutable measured histories."""

    samples: tuple[DemandHour, ...] = ()
    corrected_hours: int = 0
    rejected_hours: int = 0

    @property
    def substantial_rejection(self):
        """Require attention after losing a day of complete balances or every complete hour."""
        return self.rejected_hours >= MAX_REJECTED_TRAINING_HOURS or (
            self.rejected_hours > 0 and not self.samples
        )

    @property
    def rejected_percentage(self):
        """Return the excluded share of complete assessed hours, or None without evidence."""
        assessed_hours = len(self.samples) + self.rejected_hours
        return 100 * self.rejected_hours / assessed_hours if assessed_hours else None

    @property
    def notification_required(self):
        """Notify only for at least 20 percent rejection after 24 complete assessed hours.

        Missing hours are not assessed balances. Appliance model admission keeps
        its separate conservative substantial_rejection safeguard.
        """
        assessed_hours = len(self.samples) + self.rejected_hours
        return (
            assessed_hours >= MIN_NOTIFICATION_HISTORY_HOURS
            and 100 * self.rejected_hours >= REJECTED_HOURS_NOTIFICATION_PERCENT * assessed_hours
        )

    def document(self):
        """Expose disposable counts without copying measured samples into another archive."""
        return {
            "usable_hours": len(self.samples),
            "corrected_hours": self.corrected_hours,
            "rejected_hours": self.rejected_hours,
            "rejected_percentage": self.rejected_percentage,
        }


class DemandReconstructor:
    """Apply explicit AC energy balances without learning battery charging as household load."""

    def reconstruct(self, configuration, histories, *, before):
        """Return usable training hours, refusing substantially inconsistent component history."""
        result = self.analyze(configuration, histories, before=before)
        if result.substantial_rejection:
            raise ValueError("Inconsistent household boundary or overlapping exclusions")
        return result.samples

    def analyze(self, configuration, histories, *, before):
        """Filter complete balances independently without changing any measured history.

        Statistics sums handle historical resets. Adjacent cumulative samples
        provide each hour's energy. A decreasing sum invalidates both adjacent
        differences because the high endpoint may itself be a corrected spike.
        Exclusions use measured consumption, never proposed schedules.
        Each cumulative meter receives a declared 1 Wh endpoint-rounding budget
        per difference. This is an internal allowance, not measured accuracy.
        Larger negative balances are unavailable rather than fabricated zeros.
        """
        channels = {}
        if not configuration.entities:
            return ReconstructionResult()
        for entity in configuration.entities:
            history = next((item for item in histories if item.entity_id == entity), None)
            if history is None:
                return ReconstructionResult()
            values = {}
            for previous, current in zip(history.hours, history.hours[1:]):
                if current.start + timedelta(hours=1) > before:
                    continue
                delta = current.total_kwh - previous.total_kwh
                if delta < 0:
                    values.pop(previous.start, None)
                elif current.start - previous.start == timedelta(hours=1):
                    values[current.start] = delta
            channels[entity] = values
        complete = set.intersection(*(set(channel) for channel in channels.values()))
        result = []
        corrected_hours = rejected_hours = 0
        household_tolerance = len(configuration.terms) * METER_ROUNDING_ALLOWANCE_KWH
        residual_tolerance = len(configuration.entities) * METER_ROUNDING_ALLOWANCE_KWH
        for start in sorted(complete):
            total = sum(term.sign * channels[term.entity_id][start] for term in configuration.terms)
            excluded = sum(channels[term.entity_id][start] for term in configuration.exclusions)
            residual = total - excluded
            if (
                total < -household_tolerance - BALANCE_NUMERICAL_TOLERANCE_KWH
                or residual < -residual_tolerance - BALANCE_NUMERICAL_TOLERANCE_KWH
            ):
                rejected_hours += 1
                continue
            corrected_hours += int(total < 0 or residual < 0)
            total = max(0.0, total)
            result.append(DemandHour(start, total, max(0.0, residual)))
        return ReconstructionResult(tuple(result), corrected_hours, rejected_hours)
