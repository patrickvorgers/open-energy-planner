"""Persist matched battery cash totals independently of missing physical-reference history."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from open_energy_planner.core.calendar import day_window
from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
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
    store.put_frames(
        measured,
        {
            "bindings": bindings(),
            "seeds": [],
            "location": None,
            "market_identity": {"currency": "EUR", "market_area": "NL"},
        },
    )
    monkeypatch.setattr(
        plans,
        "get_context",
        lambda *_: None,
    )
    with engine.begin() as connection:
        retain(
            connection,
            (
                PriceSeries(
                    "gap-price",
                    "NL",
                    "EUR",
                    START,
                    None,
                    None,
                    (PricePoint(measured[1].start, measured[1].end, 0.1, PriceKind.PUBLISHED, 15),),
                ),
            ),
        )
    day = DailySavings(runtime).calculate(
        START.date().isoformat(), "UTC", contract(), None, START + timedelta(days=2), START, None
    )
    assert day["cash"]["savings"] == pytest.approx(1.20)
    assert day["cash"]["coverage_seconds"] == 1800
    assert "reference" not in day["scenarios"]
    assert set(day["checkpoint"]) == {"end", "cash_counters"}


def test_cash_index_retains_matching_totals_and_does_not_mix_a_legacy_day(settlement):
    """Two import-only quarters cost .60 on both sides, and the independent cash index stores exact zero."""
    runtime, store, documents, _engine, _plans = settlement
    day = DailySavings(runtime).calculate(
        START.date().isoformat(), "UTC", contract(), None, START + timedelta(days=2), START, None
    )
    store.put_day(day, documents, (START.date() + timedelta(days=1)).isoformat())
    legacy = deepcopy(day)
    legacy.pop("cash")
    legacy.update(
        day="2026-10-23",
        start="2026-10-23T00:00:00+00:00",
        end="2026-10-24T00:00:00+00:00",
        policy="daily-savings-v1",
        finalized_at=(START + timedelta(days=7)).isoformat(),
        savings=99,
    )
    legacy["checkpoint"]["end"] = legacy["end"]
    store.put_day(legacy, documents, "2026-10-24")
    start, end = day_window(START.date().isoformat(), "UTC")
    result = DailySavings(runtime).overview(start, end + timedelta(days=1))
    assert len(result["days"]) == 2
    assert result["days"][1]["policy"] == "daily-savings-v1"
    assert result["cash"]["actual_cost"] == pytest.approx(0.60)
    assert result["cash"]["without_battery_cost"] == pytest.approx(0.60)
    assert result["cash"]["savings"] == 0
    assert result["cash"]["breakdown"] == {
        "actual": {"purchase": pytest.approx(0.60), "sale": 0, "bonus": 0},
        "without_battery": {"purchase": pytest.approx(0.60), "sale": 0, "bonus": 0},
    }
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
    assert store.day(finalized=True) == day


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
    with engine.begin() as connection:
        retain(
            connection,
            (
                PriceSeries(
                    "usd",
                    "NL",
                    "USD",
                    START,
                    None,
                    None,
                    tuple(
                        PricePoint(
                            moment, moment + timedelta(minutes=15), 0.4, PriceKind.PUBLISHED, 15
                        )
                        for moment in (START, END)
                    ),
                ),
            ),
        )
    report = DailySavings(runtime).calculate(
        "2026-10-22",
        "UTC",
        replace(contract(buy="market"), currency="USD"),
        None,
        START + timedelta(days=2),
        START,
        None,
    )
    assert report["cash"]["savings"] is None
    assert report["cash"]["coverage_seconds"] == 0
    assert report["energy"]["grid_import"]["kwh"] == 2
    assert runtime.issue is not None
    with engine.begin() as connection:
        connection.execute(SETTLEMENT_FRAMES.delete())
    store.put_frames(
        (frame(), frame(start=END)),
        {
            "bindings": bindings(),
            "seeds": [],
            "location": None,
            "market_identity": {"market_area": "NL", "currency": "USD"},
        },
    )
    matching = DailySavings(runtime).calculate(
        "2026-10-22",
        "UTC",
        replace(contract(buy="market"), currency="USD"),
        None,
        START + timedelta(days=2),
        START,
        None,
    )
    assert matching["cash"]["coverage_seconds"] == 1800
    assert matching["cash"]["actual_cost"] == pytest.approx(0.8)


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


@pytest.mark.parametrize("currency, area", [("EUR", "NL"), ("USD", "NL"), ("EUR", "BE")])
def test_unidentified_old_frames_never_infer_market_from_prices(
    settlement, monkeypatch, currency, area
):
    """Unknown historical identity cannot be inferred from current prices, even in the same market."""
    runtime, _store, _documents, engine, plans = settlement
    monkeypatch.setattr(plans, "get_context", lambda *_: None)
    with engine.begin() as connection:
        retain(
            connection,
            (
                PriceSeries(
                    "other-market",
                    area,
                    currency,
                    START,
                    None,
                    None,
                    tuple(
                        PricePoint(
                            moment, moment + timedelta(minutes=15), 0.4, PriceKind.PUBLISHED, 15
                        )
                        for moment in (START, END)
                    ),
                ),
            ),
        )
    report = DailySavings(runtime).calculate(
        "2026-10-22",
        "UTC",
        replace(contract(buy="market"), currency=currency, market_area=area),
        None,
        START + timedelta(days=2),
        START,
        None,
    )
    assert report["cash"]["coverage_seconds"] == 0
    assert report["cash"]["actual_cost"] is None


def test_missing_price_advances_known_bonus_family_without_establishing_unknown_family(settlement):
    """Unknown old A stays unknown. Known B advances across an unpriced export and still bills the next quarter."""
    from open_energy_planner.contracts.bonus import BonusRule
    from open_energy_planner.contracts.calculation.formula import Formula
    from open_energy_planner.storage.adapters.lifecycle.schema import PUBLISHED_PRICES

    runtime, store, _documents, engine, _plans = settlement
    old = BonusRule("old", START - timedelta(days=2), Formula("0"), 10, timezone="UTC")
    active = BonusRule("active", START - timedelta(days=1), Formula("0.1"), 1, timezone="UTC")
    selected = replace(contract(), bonuses=(old, active))
    key = "active|" + active.period_start(START).isoformat()
    measured = tuple(
        frame(start=START + timedelta(minutes=15 * index), exported_kwh=0.5) for index in range(3)
    )
    with engine.begin() as connection:
        connection.execute(SETTLEMENT_FRAMES.delete())
        connection.execute(PUBLISHED_PRICES.delete())
        retain(
            connection,
            (
                PriceSeries(
                    "priced-edges",
                    "NL",
                    "EUR",
                    START,
                    None,
                    None,
                    tuple(
                        PricePoint(
                            moment, moment + timedelta(minutes=15), 0.1, PriceKind.PUBLISHED, 15
                        )
                        for moment in (START, START + timedelta(minutes=30))
                    ),
                ),
            ),
        )
    store.put_frames(
        measured,
        {
            "bindings": bindings(),
            "seeds": [],
            "location": None,
            "market_identity": {"currency": "EUR", "market_area": "NL"},
        },
    )
    report = DailySavings(runtime).calculate(
        "2026-10-22",
        "UTC",
        selected,
        None,
        START + timedelta(days=2),
        START,
        {"cash_counters": {"actual": {key: 0.0}, "without_battery": {key: 0.0}}},
    )
    assert report["cash"]["coverage_seconds"] == 1800
    assert report["scenarios"]["without_battery"]["totals"]["bonus"] == pytest.approx(0.05)


@pytest.mark.parametrize("changed", [{"currency": "USD"}, {"market_area": "BE"}, {}])
def test_capture_splits_market_changes_but_batches_unchanged_markets(settlement, changed):
    """A catch-up request with unchanged meters stops at the first market or currency change only."""
    from types import SimpleNamespace

    from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
    from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import digest

    runtime, _store, _documents, engine, plans = settlement
    for identity, moment, market in (("market-a", START, {}), ("market-b", END, changed)):
        plans.state["plan"] = {
            "plan_id": identity,
            "generated_at": moment.isoformat(),
            "currency": "EUR",
            "market_area": "NL",
            "comparison_bindings": bindings(),
            "steps": [],
            **market,
        }
        plans.save()
        with engine.begin() as connection:
            connection.execute(
                ACTIVATIONS.delete().where(ACTIVATIONS.c.activated_at == stamp(moment))
            )
            connection.execute(
                ACTIVATIONS.insert().values(
                    activated_at=stamp(moment), plan_id=identity, context_hash=digest(bindings())
                )
            )
    request = runtime.request(
        START + timedelta(hours=2),
        SimpleNamespace(batteries=[]),
        SimpleNamespace(sources=[]),
        SimpleNamespace(terms=[]),
        (),
        None,
    )
    assert request["market_identity"] == {"currency": "EUR", "market_area": "NL"}
    assert (
        request["end"] == (END if changed else START + timedelta(hours=1, minutes=45)).isoformat()
    )


def test_legacy_market_lookup_respects_activation_changes_while_disabled(settlement):
    """A disabled predecessor still owns the first old quarter, and the next market owns only the following quarter."""
    from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
    from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATIONS

    runtime, _store, _documents, engine, plans = settlement
    midnight, _ = day_window("2026-10-22", "UTC")
    plans.state["plan"] = {
        "plan_id": "changed-market",
        "generated_at": END.isoformat(),
        "strategy": "minimize cost",
        "solution_status": "optimal",
        "currency": "EUR",
        "market_area": "BE",
        "steps": [],
    }
    plans.save()
    with engine.begin() as connection:
        connection.execute(ACTIVATIONS.delete().where(ACTIVATIONS.c.activated_at == stamp(START)))
        connection.execute(
            ACTIVATIONS.insert(),
            [
                {"activated_at": stamp(midnight - timedelta(hours=1)), "plan_id": "original-plan"},
                {"activated_at": stamp(midnight - timedelta(minutes=30)), "plan_id": None},
                {"activated_at": stamp(END), "plan_id": "changed-market"},
            ],
        )
        retain(
            connection,
            (
                PriceSeries(
                    "changed-market",
                    "BE",
                    "EUR",
                    START,
                    None,
                    None,
                    tuple(
                        PricePoint(
                            moment, moment + timedelta(minutes=15), 0.1, PriceKind.PUBLISHED, 15
                        )
                        for moment in (START, END)
                    ),
                ),
            ),
        )
    for area in ("NL", "BE"):
        report = DailySavings(runtime).calculate(
            "2026-10-22",
            "UTC",
            replace(contract(), market_area=area),
            None,
            START + timedelta(days=2),
            START,
            None,
        )
        assert report["energy"]["grid_import"]["kwh"] == 2
        assert report["cash"]["coverage_seconds"] == 900
        assert report["cash"]["actual_cost"] == pytest.approx(0.3)
        assert report["cash"]["without_battery_cost"] == pytest.approx(0.3)


@pytest.mark.parametrize("missing_detail", ["broader_time", "missing_component"])
def test_cash_breakdown_never_substitutes_components_from_broader_or_unknown_time(
    settlement, missing_detail
):
    """A matched .60 cost remains known while incompatible actual components stay unavailable independently."""
    runtime, store, documents, _engine, _plans = settlement
    day = DailySavings(runtime).calculate(
        START.date().isoformat(), "UTC", contract(), None, START + timedelta(days=2), START, None
    )
    if missing_detail == "broader_time":
        day["scenarios"]["actual"]["coverage_seconds"] += 900
        day["scenarios"]["actual"]["totals"]["purchase"] = 99
    else:
        day["scenarios"]["actual"]["totals"].pop("bonus")
    day["finalized_at"] = (START + timedelta(days=4)).isoformat()
    original = deepcopy(day)
    store.put_day(day, documents, "2026-10-23")
    start, end = day_window(START.date().isoformat(), "UTC")
    cash, _reports, _count = store.daily_overview(start, end)
    assert cash["actual_cost"] == pytest.approx(0.60)
    assert cash["without_battery_cost"] == pytest.approx(0.60)
    assert cash["savings"] == 0
    assert cash["breakdown"]["actual"] is None
    assert cash["breakdown"]["without_battery"] == {
        "purchase": pytest.approx(0.60),
        "sale": 0,
        "bonus": 0,
    }
    assert store.day(after=start) == original


def test_cash_breakdown_aggregates_signed_income_across_a_long_range_without_legacy_days(
    settlement, monkeypatch
):
    """Existing daily columns explain matched net totals across years without loading retained report payloads."""
    runtime, store, documents, _engine, _plans = settlement
    day = DailySavings(runtime).calculate(
        START.date().isoformat(), "UTC", contract(), None, START + timedelta(days=2), START, None
    )
    for offset in (0, 400):
        report = deepcopy(day)
        first = START.date() + timedelta(days=offset)
        start, end = day_window(first.isoformat(), "UTC")
        report.update(day=first.isoformat(), start=start.isoformat(), end=end.isoformat())
        report["scenarios"]["actual"]["totals"].update(purchase=12, sale=3, bonus=1, grid_cash=8)
        report["scenarios"]["without_battery"]["totals"].update(
            purchase=18, sale=-2, bonus=0, grid_cash=20
        )
        report["cash"].update(actual_cost=8, without_battery_cost=20, savings=12)
        store.put_day(report, documents, end.date().isoformat())
    monkeypatch.setattr(store, "days", lambda *_args, **_kwargs: [])
    first, _end = day_window(START.date().isoformat(), "UTC")
    cash, reports, count = store.daily_overview(first, first + timedelta(days=402))
    assert reports == [] and count == 2
    assert cash["aggregation"] == "month"
    assert cash["breakdown"] == {
        "actual": {"purchase": 24, "sale": 6, "bonus": 2},
        "without_battery": {"purchase": 36, "sale": -4, "bonus": 0},
    }
    assert cash["actual_cost"] == 16
    assert cash["without_battery_cost"] == 40
    assert cash["savings"] == 24
