"""Persist matched battery cash totals independently of missing physical-reference history."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.core.calendar import day_window
from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.storage.adapters.lifecycle.schema import SETTLEMENT_FRAMES
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame


def test_gap_does_not_block_later_cash_or_require_soc_or_a_simulated_battery(
    settlement, monkeypatch
):
    """Two measured quarters separated by a hole each save .60. No forecast or battery stock is read."""
    runtime, store, _documents, engine, plans = settlement
    with engine.begin() as connection:
        connection.execute(SETTLEMENT_FRAMES.delete())
    measured = tuple(
        replace(frame(start=instant, discharged_kwh=2), battery_start={}, battery_end={})
        for instant in (START, END + timedelta(minutes=15))
    )
    store.put_frames(measured, {"bindings": bindings(), "seeds": [], "location": None})
    monkeypatch.setattr(
        plans,
        "get_context",
        lambda *_: None,
    )
    # The second quarter's missing published price remains uncovered, rather than blocking a future read.
    day = DailySavings(runtime).calculate(
        START.date().isoformat(), "UTC", contract(), None, START + timedelta(days=2), START, None
    )
    assert day["cash"]["savings"] == pytest.approx(0.60)
    assert day["cash"]["coverage_seconds"] == 900
    assert "reference" not in day["scenarios"]
    assert set(day["checkpoint"]) == {"end", "cash_counters"}


def test_cash_index_retains_matching_totals_and_does_not_mix_a_legacy_day(settlement):
    """Two import-only quarters cost .60 on both sides, and the independent cash index stores exact zero."""
    runtime, store, documents, _engine, _plans = settlement
    day = DailySavings(runtime).calculate(
        START.date().isoformat(), "UTC", contract(), None, START + timedelta(days=2), START, None
    )
    store.put_day(day, documents, (START.date() + timedelta(days=1)).isoformat())
    start, end = day_window(START.date().isoformat(), "UTC")
    result = DailySavings(runtime).overview(start, end)
    assert result["cash"]["actual_cost"] == pytest.approx(0.60)
    assert result["cash"]["without_battery_cost"] == pytest.approx(0.60)
    assert result["cash"]["savings"] == 0
    assert result["cash"]["coverage_seconds"] == 1800
    assert len(result["cash"]["chart"]) == 1
    assert result["cash"]["chart"][0]["savings"] == 0
    assert "checkpoint" not in result["days"][0]


def test_finalized_cash_day_is_immutable_with_its_matching_index(settlement):
    """A rejected rewrite cannot change either the retained report or the associated cash totals."""
    runtime, store, documents, _engine, _plans = settlement
    day = DailySavings(runtime).calculate(
        START.date().isoformat(), "UTC", contract(), None, START + timedelta(days=7), START, None
    )
    day["finalized_at"] = (START + timedelta(days=5)).isoformat()
    store.put_day(day, documents, (START.date() + timedelta(days=1)).isoformat())
    altered = deepcopy(day)
    altered["cash"]["savings"] = 99
    with pytest.raises(ValueError, match="immutable"):
        store.put_day(altered, documents, (START.date() + timedelta(days=1)).isoformat())
    start, end = day_window(START.date().isoformat(), "UTC")
    assert DailySavings(runtime).overview(start, end)["cash"]["savings"] == 0


def test_legacy_report_is_retained_without_fabricating_new_policy_totals(settlement):
    """A finalized legacy day remains readable but cannot be relabelled as measured-cash savings."""
    runtime, store, documents, _engine, _plans = settlement
    day = DailySavings(runtime).calculate(
        START.date().isoformat(), "UTC", contract(), None, START + timedelta(days=7), START, None
    )
    day.pop("cash")
    day["policy"] = "daily-savings-v1"
    day["finalized_at"] = (START + timedelta(days=5)).isoformat()
    store.put_day(day, documents, (START.date() + timedelta(days=1)).isoformat())
    start, end = day_window(START.date().isoformat(), "UTC")
    result = DailySavings(runtime).overview(start, end)
    assert result["days"][0]["policy"] == "daily-savings-v1"
    assert result["cash"]["savings"] is None
    assert result["cash"]["coverage_seconds"] == 0


def test_frozen_market_identity_never_bills_an_old_frame_under_another_currency(settlement):
    """Two retained EUR quarters cannot become USD costs after current contract identity changes."""
    runtime, store, _documents, engine, _plans = settlement
    with engine.begin() as connection:
        connection.execute(SETTLEMENT_FRAMES.delete())
    store.put_frames(
        (frame(), frame(start=END)),
        {
            "bindings": bindings(),
            "seeds": [],
            "location": None,
            "market_identity": {"market_area": "NL", "currency": "EUR"},
        },
    )
    report = DailySavings(runtime).calculate(
        "2026-10-22",
        "UTC",
        replace(contract(), currency="USD"),
        None,
        START + timedelta(days=2),
        START,
        None,
    )
    assert report["cash"]["savings"] is None
    assert report["cash"]["coverage_seconds"] == 0
    assert report["energy"]["grid_import"]["kwh"] == 2
    assert runtime.issue is not None


def test_dc_solar_preserves_actual_cash_without_fabricating_an_ac_comparison(settlement):
    """Two measured import quarters still cost .60, while unavailable AC solar cannot produce a paired saving."""
    runtime, store, _documents, engine, _plans = settlement
    selected = bindings()
    selected["solar"][0]["measurement_boundary"] = "dc"
    selected["terms"] = [term for term in selected["terms"] if term["entity_id"] != "sensor.pv"]
    with engine.begin() as connection:
        connection.execute(SETTLEMENT_FRAMES.delete())
    store.put_frames(
        (frame(), frame(start=END)), {"bindings": selected, "seeds": [], "location": None}
    )
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, START + timedelta(days=2), START, None
    )
    assert report["scenarios"]["actual"]["totals"]["grid_cash"] == pytest.approx(0.60)
    assert report["energy"]["household"]["kwh"] == 2
    assert report["energy"]["solar_ac"]["kwh"] is None
    assert report["cash"]["savings"] is None
    assert report["cash"]["coverage_seconds"] == 0
    assert runtime.issue == "Without-battery comparison requires measured AC solar energy"
