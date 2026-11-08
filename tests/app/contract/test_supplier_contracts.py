"""Independent supplier changes, dated terms and bonus counter boundaries."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from open_energy_planner.contracts.bonus_history import BonusHistory
from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.contracts.wire import contract_document, parse_contract
from open_energy_planner.installation.validation import validate
from open_energy_planner.optimization.adapters.highs import HighsSolver
from open_energy_planner.optimization.domain import Interval
from open_energy_planner.optimization.export_bonus import settle
from tests.app.contract.support.settings import settings
from tests.app.contract.test_contract_bonuses import measured_inputs
from tests.app.installation.support.problems import installation_problem


def supplier_terms():
    """Supply two May-contract revisions followed by a March supplier change, all with a 10-kWh cap."""
    document = settings()
    first = document["periods"][0]
    first.update(
        contract_start="2026-05-01T00:00:00+00:00",
        effective_from="2026-05-01T00:00:00+00:00",
        effective_until="2027-01-01T00:00:00+00:00",
    )
    second = {
        **first,
        "effective_from": first["effective_until"],
        "effective_until": "2027-03-01T00:00:00+00:00",
    }
    third = {
        **second,
        "contract_start": second["effective_until"],
        "effective_from": second["effective_until"],
        "effective_until": None,
    }
    document["periods"] = [first, second, third]
    document["bonuses"] = [
        {
            "effective_from": period["effective_from"],
            "rate_formula": "0.04",
            "cap_kwh": 10,
            "timezone": "UTC",
        }
        for period in document["periods"]
    ]
    return document


def test_supplier_start_belongs_to_each_price_rule_and_owns_opaque_bonus_identity():
    """January changed terms keep May's identity. March's new supplier receives a separate counter and round-trips unchanged."""
    now = datetime(2027, 2, 28, 23, tzinfo=UTC)
    contract = parse_contract(supplier_terms(), now)
    first, second, third = contract.bonuses
    assert first.rule_id == second.rule_id != third.rule_id
    assert UUID(first.rule_id).version == 5
    assert [period.contract_start for period in contract.periods] == [
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2027, 3, 1, tzinfo=UTC),
    ]
    document = contract_document(contract)
    for key in ("status", "version", "recorded_at", "preview", "bonus_preview"):
        document.pop(key)
    assert "contract_start" not in document
    assert "contract_start" not in document["bonuses"][0]
    restored = parse_contract(document, now)
    assert restored == contract and restored.version == contract.version


def test_new_supplier_does_not_use_exports_before_its_start_or_require_a_future_seed():
    """Nine old-contract exports before midnight cannot consume the new supplier's 10-kWh budget: three later exports earn 0.12 EUR."""
    now = datetime(2027, 2, 28, 23, tzinfo=UTC)
    contract = parse_contract(supplier_terms(), now)
    config, report, book = measured_inputs(now=now, used=9)
    old = contract.bonuses[0]
    book = replace(
        book,
        seeds=(replace(book.seeds[0], rule_id=old.rule_id, period_start=old.period_start(now)),),
    )
    prices = tuple(
        SimpleNamespace(
            start=now + timedelta(hours=index),
            end=now + timedelta(hours=index + 1),
            market_price=0.1,
        )
        for index in range(2)
    )
    compiled, evidence = BonusCompiler().compile(
        contract, (None, None), prices, now, book, config, report
    )
    future = next(bonus for bonus in compiled if bonus.rule_id == contract.bonuses[-1].rule_id)
    assert future.initial_kwh == 0
    assert future.rates == (0, 0.04)
    assert future.periods[0] != future.periods[1]
    result = settle(future, (9, 3))
    assert result.quota_kwh == (0, 3)
    assert sum(
        rate * volume for rate, volume in zip(future.rates, result.quota_kwh)
    ) == pytest.approx(0.12)
    assert [item.rule_id for item in evidence] == [old.rule_id]
    steps = tuple(
        Interval(price.start, 1, 0, 0, pv_kwh=volume) for price, volume in zip(prices, (9, 3))
    )
    problem = installation_problem(steps, grid_export_limit_w=20000, export_bonuses=compiled)
    result = HighsSolver().solve(problem)
    costs = validate(problem, result.plan)
    assert costs.bonus_income == pytest.approx(
        0.16
    )  # 1 remaining old kWh plus 3 new kWh, each at 0.04.
    assert (
        BonusCompiler().current_evidence(
            replace(contract, bonuses=(contract.bonuses[-1],)), now, None, None, None
        )
        == ()
    )


def test_first_calendar_bonus_period_starts_at_supplier_contract_then_resets_in_january():
    """A May contract excludes January-April exports. The next calendar year starts at 1 January without changing supplier identity."""
    contract = parse_contract(supplier_terms(), datetime(2026, 5, 1, tzinfo=UTC))
    rule = contract.bonuses[0]
    assert rule.period_start(datetime(2026, 8, 1, tzinfo=UTC)) == datetime(2026, 5, 1, tzinfo=UTC)
    assert rule.period_start(datetime(2027, 1, 2, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=UTC)


def test_history_after_supplier_switch_does_not_require_the_retired_suppliers_exports():
    """Only March's active supplier needs a baseline. Missing January data for the retired supplier cannot block it."""
    now = datetime(2027, 3, 3, 12, tzinfo=UTC)
    contract = parse_contract(supplier_terms(), now)
    config, _report, _book = measured_inputs(now=now)
    request = BonusHistory().request(contract, config, None, now)
    assert request.periods == ((contract.bonuses[-1].rule_id, datetime(2027, 3, 1, tzinfo=UTC)),)


def test_retired_legacy_bonus_without_contract_metadata_cannot_block_current_history():
    """Only the active ID needs a seed from its first activation, even without supplier-start metadata."""
    now = datetime(2027, 3, 3, 12, tzinfo=UTC)
    document = supplier_terms()
    for period in document["periods"]:
        period.pop("contract_start")
    for index, bonus in enumerate(document["bonuses"]):
        bonus["rule_id"] = "current" if index == 2 else "retired"
    contract = parse_contract(document, now)
    config, _report, _book = measured_inputs(now=now)
    request = BonusHistory().request(contract, config, None, now)
    assert request.periods == (("current", datetime(2027, 3, 1, tzinfo=UTC)),)


def test_new_actual_supplier_requires_evidence_instead_of_assuming_zero_from_a_late_reading():
    """One minute after a supplier starts, the fresh meter cannot prove the opening volume, so it must not invent a zero baseline."""
    now = datetime(2027, 3, 1, 0, 1, tzinfo=UTC)
    contract = parse_contract(supplier_terms(), now)
    config, report, _book = measured_inputs(now=now)
    with pytest.raises(ValueError, match="bonus_counter_baseline_required"):
        BonusCompiler().current_evidence(
            replace(contract, bonuses=(contract.bonuses[-1],)), now, None, config, report
        )
    assert BonusHistory().request(contract, config, None, now) is None
    later = now.replace(hour=2)
    request = BonusHistory().request(contract, config, None, later)
    assert request.periods == ((contract.bonuses[-1].rule_id, datetime(2027, 3, 1, tzinfo=UTC)),)


def test_rule_rejects_contract_start_after_its_own_effective_date():
    """A supplier contract cannot begin after the terms already take effect. The original draft remains unchanged."""
    document = supplier_terms()
    invalid = deepcopy(document)
    invalid["periods"][1]["contract_start"] = "2027-01-02T00:00:00+00:00"
    with pytest.raises(ValueError, match="contract start"):
        parse_contract(invalid, datetime(2026, 5, 1, tzinfo=UTC))
    assert document["periods"][1]["contract_start"] == "2026-05-01T00:00:00+00:00"
