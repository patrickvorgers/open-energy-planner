"""The same transactional evidence and selection contract on real configured database backends."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from open_energy_planner.prices.selection import ForecastPair, ForecastSelection, PublishedHour
from open_energy_planner.prices.storage.adapters.selection_store import (
    ACTUALS,
    DECISIONS,
    PAIRS,
    SqlAlchemySelectionStore,
)

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def evidence():
    """Seven complete zero-price days make independent MAEs exactly 0.02 and 0.01 EUR/kWh."""
    pairs, actuals = [], []
    for hour in range(168):
        target = NOW - timedelta(days=7) + timedelta(hours=hour)
        pairs.append(
            ForecastPair(1, target - timedelta(hours=30), target, 0.02, -0.01, "a" * 64, "b" * 64)
        )
        actuals.append(PublishedHour(target, 0.0, "c" * 64))
    return tuple(pairs), tuple(actuals)


def test_backend_preserves_pairs_and_daily_decisions_across_reopen(selection_store):
    """All backends switch only after three daily 50%-better assessments and retain the immutable paired sample count."""
    adapter, area = selection_store
    selector = ForecastSelection(adapter, area, "primary", "alternative")
    pairs, actuals = evidence()
    # Record predictions before actual prices are known, as in the runtime.
    adapter.update(
        area, pairs, (), NOW - timedelta(days=8), NOW - timedelta(days=38), selector.decide
    )
    for day in range(3):
        clock = NOW + timedelta(days=day)
        selector.states = adapter.update(
            area, (), actuals, clock, clock - timedelta(days=30), selector.decide
        )
    restored = ForecastSelection(
        SqlAlchemySelectionStore(adapter.engine), area, "primary", "alternative"
    )
    assert restored.preferred[1] == "alternative"
    assert restored.states[1]["primary_mae"] == pytest.approx(0.02)
    assert restored.states[1]["alternative_mae"] == pytest.approx(0.01)
    with adapter.engine.connect() as connection:
        assert (
            connection.scalar(select(func.count()).select_from(PAIRS).where(PAIRS.c.area == area))
            == 168
        )
        assert (
            connection.scalar(
                select(func.count()).select_from(ACTUALS).where(ACTUALS.c.area == area)
            )
            == 168
        )


def test_backend_rolls_back_evidence_when_assessment_fails(selection_store):
    """An exception after evidence insertion rolls back actuals and decisions in the same transaction on every backend."""
    adapter, area = selection_store
    assert adapter.load(area) == {}
    _, actuals = evidence()

    def fail(_old, _score, _day):
        """Simulate an application failure while the owned transaction is still open."""
        raise ValueError("fixture assessment failure")

    with pytest.raises(ValueError, match="fixture assessment failure"):
        adapter.update(area, (), actuals, NOW, NOW - timedelta(days=30), fail)
    with adapter.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(ACTUALS).where(ACTUALS.c.area == area)
            )
            == 0
        )
        assert (
            connection.scalar(
                select(func.count()).select_from(DECISIONS).where(DECISIONS.c.area == area)
            )
            == 0
        )


def test_backend_never_pairs_known_actuals_or_overwrites_a_receipt(selection_store):
    """Once truth is known a new pair is excluded. An existing cohort keeps its original forecast values."""
    adapter, area = selection_store
    selector = ForecastSelection(adapter, area, "primary", "alternative")
    pairs, actuals = evidence()
    adapter.update(
        area, pairs[:1], (), NOW - timedelta(days=8), NOW - timedelta(days=38), selector.decide
    )
    changed = ForecastPair(**{**vars(pairs[0]), "primary_price": 999})
    adapter.update(area, (changed,), actuals[:2], NOW, NOW - timedelta(days=30), selector.decide)
    adapter.update(area, pairs[1:2], (), NOW, NOW - timedelta(days=30), selector.decide)
    with adapter.engine.connect() as connection:
        rows = connection.execute(select(PAIRS.c.primary_price).where(PAIRS.c.area == area)).all()
        assert rows == [(0.02,)]


def test_backend_area_isolation_and_schema_reopen_preserve_history(selection_store):
    """Creating another adapter/schema view neither leaks market evidence nor deletes the existing area's records."""
    adapter, area = selection_store
    selector = ForecastSelection(adapter, area, "primary", "alternative")
    pairs, _ = evidence()
    adapter.update(
        area, pairs, (), NOW - timedelta(days=8), NOW - timedelta(days=38), selector.decide
    )
    assert SqlAlchemySelectionStore(adapter.engine).load(area + "X") == {}
    with adapter.engine.connect() as connection:
        assert (
            connection.scalar(select(func.count()).select_from(PAIRS).where(PAIRS.c.area == area))
            == 168
        )
