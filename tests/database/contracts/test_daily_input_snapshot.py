"""Daily replay preserves activation boundaries and rereads corrected evidence on its next run."""

from copy import deepcopy
from datetime import timedelta

import pytest

from open_energy_planner.prices.model import PriceKind, PricePoint, PriceSeries
from open_energy_planner.prices.storage.adapters.published import retain
from open_energy_planner.savings import daily as daily_module
from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.savings.storage.adapters.sqlalchemy import stamp
from open_energy_planner.storage.adapters.lifecycle.schema import ACTIVATION_CONTEXTS, ACTIVATIONS
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame


def test_day_replay_resolves_initial_disabled_midquarter_and_next_plan(settlement, monkeypatch):
    """Five/ten-minute advice portions are .4/.8 kWh. Disabled advice never deletes the last physical context."""
    runtime, store, _documents, engine, plans = settlement
    original = plans.get_plan("original-plan")
    for identity, amount in (("first", 2.4), ("next", 4.8)):
        document = deepcopy(original)
        document.update(
            plan_id=identity, expected_flows=[{"grid_import": amount, "grid_export": 0}]
        )
        interval = document["input_snapshot"]["problem"]["intervals"][0]
        interval.update(start=START.isoformat(), duration_h=1)
        plans.state["plan"] = document
        plans.save()
    timeline = [
        (START - timedelta(days=1), "first", True),
        (START - timedelta(minutes=5), None, False),
        (START + timedelta(minutes=5), "next", True),
        (START + timedelta(minutes=20), None, False),
        (START + timedelta(minutes=30), "first", True),
    ]
    with engine.begin() as connection:
        connection.execute(ACTIVATION_CONTEXTS.delete())
        connection.execute(ACTIVATIONS.delete())
        connection.execute(
            ACTIVATIONS.insert(),
            [
                {"activated_at": stamp(at), "plan_id": identity, "enabled": int(enabled)}
                for at, identity, enabled in timeline
            ],
        )
        retain(
            connection,
            (
                PriceSeries(
                    "synthetic-chronology",
                    "NL",
                    "EUR",
                    START - timedelta(hours=1),
                    None,
                    None,
                    tuple(
                        PricePoint(
                            START + timedelta(minutes=15 * index),
                            START + timedelta(minutes=15 * (index + 1)),
                            0.1,
                            PriceKind.PUBLISHED,
                            15,
                        )
                        for index in range(3)
                    ),
                ),
            ),
        )
    store.put_frames(
        (frame(start=START + timedelta(minutes=30)),),
        {"bindings": bindings(), "seeds": [], "location": None},
    )
    physical_contexts, advice = [], []
    original_context, original_advice = daily_module.physical_context, daily_module.active_advice

    def observe_context(plan):
        """Observe the real physics inputs chosen at each frame start."""
        physical_contexts.append(plan["plan_id"])
        return original_context(plan)

    def observe_advice(*args):
        """Execute the real overlapping-plan integration and retain its public result."""
        result = original_advice(*args)
        advice.append(result)
        return result

    monkeypatch.setattr(daily_module, "physical_context", observe_context)
    monkeypatch.setattr(daily_module, "active_advice", observe_advice)
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=1), START, None
    )
    assert physical_contexts == ["first", "next", "first"]
    assert [item["coverage_seconds"] for item in advice] == [600, 300, 900]
    assert [item["import_kwh"] for item in advice] == pytest.approx([0.8, 0.4, 0.6])
    assert report["scenarios"]["actual"]["totals"]["purchase"] == pytest.approx(0.9)
    assert report["savings"] == pytest.approx(-0.2)


def test_day_input_read_keeps_one_snapshot_and_next_read_sees_corrections(settlement, monkeypatch):
    """Concurrent new activation and .80 price cannot mix with an earlier snapshot's .10 receipt identities."""
    _runtime, store, _docs, engine, _plans = settlement
    before = store.daily_inputs(START, END + timedelta(minutes=15), "NL", "EUR")
    original = store.context_plan

    def correct_after_context(*args, **kwargs):
        """Commit real changes after the first SELECT established the snapshot."""
        identity = original(*args, **kwargs)
        with engine.begin() as connection:
            connection.execute(
                ACTIVATIONS.insert().values(
                    activated_at=stamp(START + timedelta(minutes=5)), plan_id=None, enabled=0
                )
            )
            retain(
                connection,
                (
                    PriceSeries(
                        "synthetic-concurrent-correction",
                        "NL",
                        "EUR",
                        END + timedelta(hours=1),
                        None,
                        None,
                        (PricePoint(START, END, 0.8, PriceKind.PUBLISHED, 15),),
                    ),
                ),
            )
        return identity

    monkeypatch.setattr(store, "context_plan", correct_after_context)
    during = store.daily_inputs(START, END + timedelta(minutes=15), "NL", "EUR")
    assert during == before
    monkeypatch.setattr(store, "context_plan", original)
    after = store.daily_inputs(START, END + timedelta(minutes=15), "NL", "EUR")
    assert len(after[1]) == len(before[1]) + 1
    assert any(
        row["price"] == 0.8 and row["received_at"] == stamp(END + timedelta(hours=1))
        for row in after[2]
    )
    assert after[2] != before[2]
