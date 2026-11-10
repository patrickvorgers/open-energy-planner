"""Historical calculations remain independent of intentionally removed complete plans on every backend."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.savings.storage.adapters.sqlalchemy import SettlementStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS, PLAN_CONTEXTS, PLANS
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.savings.support.scenarios import END, START, contract


def test_settlement_replay_survives_removal_of_complete_original_plan(settlement):
    """Compact original physics reproduces .80 actual costs and -.40 benefit after a provisional tariff correction."""
    runtime, store, _documents, engine, plans = settlement
    runtime.process(contract(), END + timedelta(hours=1))
    expected_context = plans.get_context("original-plan")
    plans.preserve_history()
    plans.state["plan"] = None
    plans.save()
    with engine.begin() as connection:
        connection.execute(
            ACTIVATIONS.update()
            .where(ACTIVATIONS.c.plan_id == "original-plan")
            .values(plan_id=None)
        )
        connection.execute(PLANS.delete().where(PLANS.c.plan_id == "original-plan"))
    reopened = SqlPlanRepository(".", engine=engine)
    assert reopened.get_plan("original-plan") is None
    assert reopened.get_context("original-plan") == expected_context
    assert store.context_plan(START) == "original-plan"
    restored = SavingsRuntime(SettlementStore(engine), SqlDocuments(engine, "savings"), reopened)
    restored.process(contract(buy="0.4"), END + timedelta(hours=2))
    result = restored.overview(START, END + timedelta(minutes=15))
    assert result["totals"]["actual"]["purchase"] == pytest.approx(0.8)
    assert result["savings"] == pytest.approx(-0.4)
    assert result["coverage"] == {"evaluated": 2, "expected": 2, "estimated": 0}


def test_context_corruption_cannot_silently_change_historical_physics(settlement):
    """A stored projection is independently hash checked before use, even when its complete plan remains valid."""
    _runtime, _store, _documents, engine, plans = settlement
    with engine.begin() as connection:
        connection.execute(
            PLAN_CONTEXTS.update()
            .where(PLAN_CONTEXTS.c.plan_id == "original-plan")
            .values(payload='{"plan_id":"original-plan","input_snapshot":{}}')
        )
    with pytest.raises(ValueError, match="Historical calculation context hash mismatch"):
        plans.get_context("original-plan")
    assert plans.get_plan("original-plan") is not None


def test_expired_context_is_not_reconstructed_from_an_old_day_plan(settlement):
    """An explicitly expired detailed context stays unavailable rather than reviving it from retained forecasts."""
    _runtime, _store, _documents, engine, plans = settlement
    with engine.begin() as connection:
        connection.execute(PLAN_CONTEXTS.update().values(payload=None))
        assert connection.scalar(select(PLAN_CONTEXTS.c.payload)) is None
    assert plans.get_context("original-plan") is None
    assert plans.get_plan("original-plan") is not None


def test_boundary_context_survives_while_old_activation_backfill_is_pending(settlement):
    """An original activation FK protects the boundary installation before its permanent logical link is backfilled."""
    from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
    from open_energy_planner.storage.adapters.lifecycle.retention import expire_evidence
    from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATION_CONTEXTS

    _runtime, store, _documents, engine, plans = settlement
    expected = plans.get_context("original-plan")
    plans.state["plan"] = None
    plans.save()
    with engine.begin() as connection:
        connection.execute(ACTIVATION_CONTEXTS.delete())
        connection.execute(
            ACTIVATIONS.delete().where(
                ACTIVATIONS.c.plan_id.is_not(None), ACTIVATIONS.c.activated_at != stamp(START)
            )
        )
        expire_evidence(connection, (START.replace(hour=0) + timedelta(days=1)).isoformat())
    assert store.context_plan(START + timedelta(days=1)) == "original-plan"
    assert plans.get_context("original-plan") == expected
