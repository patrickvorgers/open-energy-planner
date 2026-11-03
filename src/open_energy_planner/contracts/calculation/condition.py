"""Small immutable arithmetic comparisons, independent of bonus payment and solver models."""

import operator
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from open_energy_planner.contracts.calculation.formula import MAX_FORMULA_MAGNITUDE, Formula


class ComparisonOperator(StrEnum):
    """Stable wire identifiers for the six supported numerical comparisons."""

    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    EQUAL = "equal"
    NOT_EQUAL = "not_equal"


COMPARISONS = {
    ComparisonOperator.GREATER_THAN: operator.gt,
    ComparisonOperator.GREATER_THAN_OR_EQUAL: operator.ge,
    ComparisonOperator.LESS_THAN: operator.lt,
    ComparisonOperator.LESS_THAN_OR_EQUAL: operator.le,
    ComparisonOperator.EQUAL: operator.eq,
    ComparisonOperator.NOT_EQUAL: operator.ne,
}


@dataclass(frozen=True)
class ComparisonCondition:
    """Compare a bounded Formula result with a finite Decimal threshold, without epsilon or float rounding."""

    expression: Formula
    operator: ComparisonOperator
    threshold: Decimal

    def __post_init__(self):
        """Require validated types and the same finite magnitude supported by price expressions."""
        if not isinstance(self.expression, Formula) or not isinstance(
            self.operator, ComparisonOperator
        ):
            raise TypeError("Comparison requires a validated expression and operator")
        if (
            not isinstance(self.threshold, Decimal)
            or not self.threshold.is_finite()
            or self.threshold.copy_abs() > MAX_FORMULA_MAGNITUDE
        ):
            raise ValueError("Comparison threshold must be a finite supported decimal price")

    def matches(self, market: float) -> bool:
        """Compare a currency/kWh market price with the threshold using Decimal arithmetic."""
        return COMPARISONS[self.operator](self.expression.evaluate_decimal(market), self.threshold)
