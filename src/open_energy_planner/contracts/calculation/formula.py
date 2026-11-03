"""Interpret a bounded arithmetic grammar without executing user-supplied Python."""

import ast
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from functools import cached_property
from math import isfinite

MAX_FORMULA_AST_NODES = 128
DECIMAL_PRECISION = 28
DECIMAL_ROUNDING = ROUND_HALF_EVEN
MAX_FORMULA_MAGNITUDE = 1000000
MAX_FORMULA_LENGTH = 512
MAX_LITERAL_EXPONENT = 100
MAX_FORMULA_FUNCTION_ARGUMENTS = 4


@dataclass(frozen=True)
class Formula:
    """An arithmetic price expression in currency/kWh using only ``market``.

    Permit decimal literals, parentheses, +, -, *, /, unary signs and min/max/abs.
    No attributes, indexing, imports, powers, comparisons or arbitrary calls are
    executable. Parse at most 512 characters and 128 nodes. Decimal arithmetic
    uses 28 significant digits. Conversion to the solver's float happens once.
    Invoice rounding and volume-dependent rules are separate responsibilities.
    """

    text: str

    def __post_init__(self):
        """Validate every branch before any price evaluation or configuration save."""
        if not isinstance(self.text, str) or not 1 <= len(self.text) <= MAX_FORMULA_LENGTH:
            raise ValueError("Formula requires 1-512 characters")
        try:
            tree = self._tree
        except (SyntaxError, RecursionError) as error:
            raise ValueError("Invalid formula syntax") from error
        if len(tuple(ast.walk(tree))) > MAX_FORMULA_AST_NODES:
            raise ValueError("Formula is too complex")
        self._validate(tree.body)

    @cached_property
    def _tree(self):
        """Cache the private parse tree. Never compile or evaluate it as Python code."""
        return ast.parse(self.text.strip(), mode="eval")

    def _validate(self, node):
        """Accept only the finite arithmetic grammar and fixed function arities."""
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float):
                raise ValueError("Only finite numeric literals are allowed")
            try:
                literal = Decimal(ast.get_source_segment(self.text.strip(), node).replace("_", ""))
            except DecimalException as error:
                raise ValueError("Only decimal literals are supported") from error
            if (
                not literal.is_finite()
                or not -MAX_LITERAL_EXPONENT <= literal.as_tuple().exponent <= MAX_LITERAL_EXPONENT
                or literal.copy_abs() > MAX_FORMULA_MAGNITUDE
            ):
                raise ValueError("Numeric literal exceeds the supported range")
        elif isinstance(node, ast.Name) and node.id == "market":
            return
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            self._validate(node.operand)
        elif isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            self._validate(node.left)
            self._validate(node.right)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("min", "max", "abs")
            and not node.keywords
            and (
                len(node.args) == 1
                if node.func.id == "abs"
                else 2 <= len(node.args) <= MAX_FORMULA_FUNCTION_ARGUMENTS
            )
        ):
            for argument in node.args:
                self._validate(argument)
        else:
            raise ValueError("Unsupported formula construct")

    def evaluate(self, market: float) -> float:
        """Convert a validated Decimal price to the solver's finite float boundary."""
        return float(self.evaluate_decimal(market))

    def evaluate_decimal(self, market: float) -> Decimal:
        """Keep 28-digit decimal arithmetic for exact condition comparisons, rejecting invalid prices."""
        if type(market) not in (int, float) or not isfinite(market):
            raise ValueError("Market price must be finite")
        try:
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                context.rounding = DECIMAL_ROUNDING
                result = self._number(self._tree.body, Decimal(str(market)))
        except (DecimalException, OverflowError) as error:
            raise ValueError("Formula arithmetic failed") from error
        if not result.is_finite() or result.copy_abs() > MAX_FORMULA_MAGNITUDE:
            raise ValueError("Effective price exceeds the supported finite range")
        return result

    def _number(self, node, market):
        """Evaluate previously validated arithmetic nodes with decimal operands only."""
        if isinstance(node, ast.Constant):
            return Decimal(ast.get_source_segment(self.text.strip(), node).replace("_", ""))
        if isinstance(node, ast.Name):
            return market
        if isinstance(node, ast.UnaryOp):
            value = self._number(node.operand, market)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.Call):
            values = [self._number(argument, market) for argument in node.args]
            return {"min": min, "max": max, "abs": lambda items: abs(items[0])}[node.func.id](
                values
            )
        left, right = self._number(node.left, market), self._number(node.right, market)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left / right
