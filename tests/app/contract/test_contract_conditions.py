"""Exact price eligibility at arithmetic, wire and immutable archive boundaries."""

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest

from open_energy_planner.contracts.calculation.condition import (
    ComparisonCondition,
    ComparisonOperator,
)
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.contracts.wire import contract_document, parse_contract
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_bonuses import rule
from tests.app.contract.test_contract_prices import NOW
from tests.database.support.archive import make_store


def test_negative_ungated_bonus_is_rejected_instead_of_clamped():
    """At market -0.03 the combined price is -0.01, so its -0.001 bonus is invalid. Eligibility returns zero before evaluating it."""
    with pytest.raises(ValueError, match="bounded nonnegative"):
        replace(rule(), rate_formula=Formula("0.10 * (market + 0.02)"), price_condition=None)
    gated_bonus = replace(
        rule(),
        rate_formula=Formula("0.10 * (market + 0.02)"),
        price_condition=ComparisonCondition(
            Formula("market + 0.02"), ComparisonOperator.GREATER_THAN, Decimal(0)
        ),
    )
    assert gated_bonus.extra_rate(-0.03, NOW, check_window=False) == 0


@pytest.mark.parametrize(
    "market,expected", [(-0.03, 0), (-0.02, 0), (-0.01, 0.001), (0, 0.002), (0.18, 0.02)]
)
def test_zonneplan_example_uses_explicit_condition_with_equivalent_formula_only_alternative(
    market, expected
):
    """A positive combined price gates the 10% rate. -0.02 is exactly excluded, and max-based alternative pays the same at every independent example."""
    explicit = replace(
        rule(),
        rate_formula=Formula("0.10 * (market + 0.02)"),
        price_condition=ComparisonCondition(
            Formula("market + 0.02"), ComparisonOperator.GREATER_THAN, Decimal(0)
        ),
    )
    alternative = replace(rule(), rate_formula=Formula("0.10 * max(market + 0.02, 0)"))
    assert explicit.extra_rate(market, NOW, check_window=False) == pytest.approx(expected)
    assert alternative.extra_rate(market, NOW, check_window=False) == pytest.approx(expected)


@pytest.mark.parametrize("market,expected", [(-0.01, 0), (0, 0), (0.06, 0.03), (0.1, 0.05)])
def test_nextenergy_example_separates_positive_price_eligibility_from_its_half_market_rate(
    market, expected
):
    """A 50% rate pays 0.03 on market 0.06, while zero/negative prices earn zero. The max alternative is equivalent."""
    explicit = replace(
        rule(),
        rate_formula=Formula("0.50 * market"),
        price_condition=ComparisonCondition(
            Formula("market"), ComparisonOperator.GREATER_THAN, Decimal(0)
        ),
    )
    alternative = replace(rule(), rate_formula=Formula("0.50 * max(market, 0)"))
    assert explicit.extra_rate(market, NOW, check_window=False) == pytest.approx(expected)
    assert alternative.extra_rate(market, NOW, check_window=False) == pytest.approx(expected)


@pytest.mark.parametrize(
    "operator,expected",
    [
        ("greater_than", (False, False, True)),
        ("greater_than_or_equal", (False, True, True)),
        ("less_than", (True, False, False)),
        ("less_than_or_equal", (True, True, False)),
        ("equal", (False, True, False)),
        ("not_equal", (True, False, True)),
    ],
)
@pytest.mark.parametrize("threshold", ["-0.05", "0", "0.05"])
def test_comparisons_cover_each_side_and_exact_boundary(operator, expected, threshold):
    """Every operator distinguishes below/equal/above at negative, zero and positive thresholds."""
    boundary = Decimal(threshold)
    condition = ComparisonCondition(Formula("market"), ComparisonOperator(operator), boundary)
    assert (
        tuple(
            condition.matches(float(boundary + offset))
            for offset in map(Decimal, ("-0.01", "0", "0.01"))
        )
        == expected
    )


def test_shifted_expression_compares_before_float_rounding():
    """0.03+0.02 equals 0.05 exactly. Adjacent decimal thresholds must remain distinguishable."""
    condition = ComparisonCondition(
        Formula("market + 0.02"), ComparisonOperator.EQUAL, Decimal("0.05")
    )
    assert condition.matches(0.03)
    assert not replace(condition, threshold=Decimal("0.0500000000000000000000000001")).matches(0.03)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        assert condition.matches(0.03)


@pytest.mark.parametrize(
    "condition",
    [
        {},
        {"expression": "market", "operator": ">", "threshold": "0"},
        {"expression": "__import__('os')", "operator": "equal", "threshold": "0"},
        *[
            {"expression": "market", "operator": "equal", "threshold": threshold}
            for threshold in (True, None, [], "NaN", "Infinity", "1000001", "invalid")
        ],
    ],
)
def test_invalid_comparisons_are_rejected_at_wire_boundary(condition):
    """Malformed structures, executable expressions and unsupported thresholds cannot enter a saved contract."""
    body = settings()
    body["bonuses"] = [
        {
            "rule_id": "example",
            "effective_from": NOW.isoformat(),
            "rate_formula": "0.03",
            "cap_kwh": 100,
            "price_condition": condition,
        }
    ]
    with pytest.raises((ValueError, TypeError)):
        parse_contract(body, NOW)


def test_comparison_preview_and_archive_preserve_exact_threshold(tmp_path):
    """The wire and SQLAlchemy archive retain the decimal string and reproduce eligibility after restart."""
    body = settings()
    condition = {
        "expression": "market + 0.02",
        "operator": "less_than_or_equal",
        "threshold": "0.0200000000000000000000000001",
    }
    body["bonuses"] = [
        {
            "rule_id": "example",
            "effective_from": NOW.isoformat(),
            "rate_formula": "0.03",
            "cap_kwh": 100,
            "price_condition": condition,
        }
    ]
    contract = parse_contract(body, NOW)
    store = make_store(tmp_path, ContractStore)
    store.save(contract)
    restored = make_store(tmp_path, ContractStore).current
    document = contract_document(restored)
    assert document["bonuses"][0]["price_condition"] == condition
    assert [sample["extra_rate"] for sample in document["bonus_preview"][0]["samples"]] == [
        0.03,
        0.03,
        0.03,
        0,
        0,
    ]
    assert (
        parse_contract({key: value for key, value in document.items() if key in body}, NOW)
        == contract
    )


def test_disabled_condition_and_clock_window_remain_independent():
    """No price condition permits negative prices. An enabled gate cannot override a closed clock window."""
    current = rule(window="clock", timezone="UTC", start_time="06:00", end_time="22:00")
    assert current.extra_rate(-0.1, NOW.replace(hour=12)) == 0.04
    gated = replace(
        current,
        price_condition=ComparisonCondition(
            Formula("market + 0.02"), ComparisonOperator.LESS_THAN_OR_EQUAL, Decimal(0)
        ),
    )
    assert gated.extra_rate(-0.02, NOW.replace(hour=12)) == 0.04
    assert gated.extra_rate(-0.01, NOW.replace(hour=12)) == 0
    assert gated.extra_rate(-0.02, NOW.replace(hour=23)) == 0
