"""Age thinning preserves active plans, exact shared blocks and independently retained historical context."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from open_energy_planner.savings.daily import DailySavings
from open_energy_planner.storage.adapters.lifecycle.retention import recompress, thin
from open_energy_planner.storage.adapters.lifecycle.schema import (
    DOCUMENTS,
    PLAN_CONTEXTS,
    PLANS,
    SETTLEMENT_FRAMES,
)
from open_energy_planner.storage.adapters.payloads import decode_payload
from tests.app.savings.support.scenarios import END, START, contract


def test_quarter_thinning_keeps_first_plan_and_active_plan_without_changing_shared_inputs(
    settlement,
):
    """Twenty minute plans become four quarter anchors plus the active plan, with exact retained JSON."""
    _runtime, _store, docs, _engine, plans = settlement
    original = plans.get_plan("original-plan")
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    expected = {}
    for index in range(20):
        document = {
            **deepcopy(original),
            "plan_id": f"thinning-{index:02}",
            "generated_at": (now - timedelta(days=2) + timedelta(minutes=index * 3)).isoformat(),
            "synthetic_shared_array": [{"hour": hour, "kwh": hour / 7} for hour in range(200)],
        }
        expected[document["plan_id"]] = deepcopy(document)
        plans.state["plan"] = document
        plans.save()
    assert thin(plans, docs, now, "Europe/Amsterdam") == 15
    kept = {f"thinning-{index:02}" for index in (0, 5, 10, 15, 19)}
    for identity, document in expected.items():
        assert plans.get_plan(identity) == (document if identity in kept else None)
        assert plans.get_context(identity)["input_snapshot"] == document["input_snapshot"]
    assert plans.state["plan"]["plan_id"] == "thinning-19"
    # Pagination still advances if its original cursor was thinned between polls.
    assert plans.list_plans(before="thinning-18")[0]["plan_id"] == "thinning-15"


def test_last_day_retains_every_plan_and_old_days_keep_local_calendar_anchors(settlement):
    """Recent ad-hoc publications survive while old plans retain separate HA calendar-day anchors."""
    _runtime, _store, docs, _engine, plans = settlement
    original = plans.get_plan("original-plan")
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    instants = [
        now - timedelta(hours=1),
        now - timedelta(hours=1, minutes=2),
        datetime(2026, 10, 30, 22, 45, tzinfo=UTC),
        datetime(2026, 10, 30, 23, 15, tzinfo=UTC),
        datetime(2026, 10, 30, 23, 30, tzinfo=UTC),
    ]
    for index, instant in enumerate(instants):
        plans.state["plan"] = {
            **deepcopy(original),
            "plan_id": f"calendar-{index}",
            "generated_at": instant.isoformat(),
        }
        plans.save()
    # Make a recent plan active, so the extra old plan can actually expire.
    plans.state["plan"] = {
        **deepcopy(original),
        "plan_id": "active-new",
        "generated_at": now.isoformat(),
    }
    plans.save()
    thin(plans, docs, now, "Europe/Amsterdam")
    for index in range(4):
        assert plans.get_plan(f"calendar-{index}") is not None
    assert plans.get_plan("calendar-4") is None


def test_corrupt_context_aborts_thinning_transaction(settlement):
    """An unreadable copied context prevents deleting its exact original and leaves the maintenance cursor unchanged."""
    _runtime, _store, docs, engine, plans = settlement
    original = plans.get_plan("original-plan")
    for index in range(3):
        plans.state["plan"] = {
            **deepcopy(original),
            "plan_id": f"corrupt-{index}",
            "generated_at": (START + timedelta(minutes=index)).isoformat(),
        }
        plans.save()
    with engine.begin() as connection:
        connection.execute(
            PLAN_CONTEXTS.update()
            .where(PLAN_CONTEXTS.c.plan_id == "corrupt-1")
            .values(payload="{}")
        )
    with pytest.raises(ValueError, match="Historical calculation context hash mismatch"):
        thin(plans, docs, START + timedelta(days=2), "UTC")
    assert plans.get_plan("corrupt-1") is not None
    assert not docs.exists("retention.json")


def test_raw_evidence_never_expires_without_an_immutable_day(settlement):
    """Plan thinning alone cannot delete unsettled measurements or historical installation context."""
    _runtime, store, docs, engine, plans = settlement
    thin(plans, docs, START + timedelta(days=20), "UTC")
    assert len(store.frames()) == 2
    with engine.connect() as connection:
        assert connection.scalar(select(SETTLEMENT_FRAMES.c.start)) is not None
        assert connection.scalar(select(PLANS.c.plan_id)) is not None


def test_final_daily_totals_survive_expiring_temporary_quarters(settlement):
    """A sealed partial day's .60 measured cost remains exact after both raw frames have expired."""
    runtime, store, docs, _engine, plans = settlement
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=4), START, None
    )
    report["finalized_at"] = (END + timedelta(days=4)).isoformat()
    store.put_day(report, docs, "2026-10-23")
    thin(plans, docs, END + timedelta(days=5), "UTC")
    assert store.frames() == []
    assert store.day(finalized=True) == report
    assert store.daily_summary(START.replace(hour=0), START.replace(hour=0) + timedelta(days=1))[
        "actual_purchase"
    ] == pytest.approx(0.6)


def test_existing_plain_json_is_recompressed_without_changing_its_value(settlement):
    """The bounded maintenance pass handles old plain documents losslessly and resumes across table cursors."""
    _runtime, _store, docs, engine, _plans = settlement
    payload = '{"history":[' + ",".join('"synthetic-unchanged"' for _index in range(1500)) + "]}"
    with engine.begin() as connection:
        connection.execute(
            DOCUMENTS.insert().values(namespace="savings", name="old-large.json", payload=payload)
        )
    for _round in range(16):
        recompress(docs)
    with engine.connect() as connection:
        stored = connection.scalar(
            select(DOCUMENTS.c.payload).where(DOCUMENTS.c.name == "old-large.json")
        )
    assert stored.startswith("oep:gzip:1:")
    assert decode_payload(stored) == payload
    assert docs.read("old-large.json")["history"] == ["synthetic-unchanged"] * 1500


def test_compression_cannot_overwrite_a_newer_case_only_document_change(settlement, monkeypatch):
    """A competing OLD to old update survives compression on case-insensitive database defaults too."""
    from open_energy_planner.storage.adapters.lifecycle import retention

    _runtime, _store, docs, engine, _plans = settlement
    original = '{"name":"OLD","padding":"' + "synthetic-padding-" * 800 + '"}'
    newer = original.replace('"OLD"', '"old"')
    with engine.begin() as connection:
        connection.execute(
            DOCUMENTS.insert().values(namespace="savings", name="changing.json", payload=original)
        )
    encode, changed = retention.encode_payload, []

    def competing_write(payload):
        """Change the real stored text after maintenance read it, before its compare-and-swap update."""
        if payload == original:
            docs.pending.connection.execute(
                DOCUMENTS.update()
                .where(DOCUMENTS.c.namespace == "savings", DOCUMENTS.c.name == "changing.json")
                .values(payload=newer)
            )
            changed.append(True)
        return encode(payload)

    monkeypatch.setattr(retention, "encode_payload", competing_write)
    for _round in range(16):
        recompress(docs)
    assert changed == [True]
    assert docs.read("changing.json")["name"] == "old"


def test_old_archive_startup_adds_context_tables_and_incoming_indexes_without_loss(settlement):
    """Restart expands an existing version-1 archive and resumes context backfill without changing its original plan."""
    from sqlalchemy import inspect

    from open_energy_planner.storage.adapters.lifecycle.schema import (
        ACTIVATION_CONTEXTS,
        ARCHIVE_INCOMING_INDEXES,
        SAVINGS_DAYS,
    )
    from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

    _runtime, _store, _docs, engine, plans = settlement
    original = plans.get_plan("original-plan")
    with engine.begin() as connection:
        ACTIVATION_CONTEXTS.drop(connection)
        PLAN_CONTEXTS.drop(connection)
        SAVINGS_DAYS.drop(connection)
        for index in ARCHIVE_INCOMING_INDEXES:
            column = next(iter(index.columns))
            # Existing InnoDB archives already have an incoming FK-support index.
            connection.exec_driver_sql(
                f"CREATE INDEX fixture_legacy_{index.name} ON {index.table.name} ({column.name})"
            )
            index.drop(connection)
    reopened = SqlPlanRepository(".", engine=engine)
    assert reopened.get_plan("original-plan") == original
    for index in ARCHIVE_INCOMING_INDEXES:
        assert index.name in {
            item["name"] for item in inspect(engine).get_indexes(index.table.name)
        }
    assert reopened.preserve_history(limit=1)["contexts"] == 1
    reopened.preserve_history(limit=1)
    assert reopened.get_context("original-plan")["input_snapshot"] == original["input_snapshot"]


def test_cached_final_day_end_cannot_authorize_expiring_later_unfinalized_frames(settlement):
    """A damaged lookup end must not delete next-day metering beyond the verified finalized report."""
    from open_energy_planner.storage.adapters.lifecycle.schema import SAVINGS_DAYS
    from tests.app.savings.support.scenarios import bindings, frame

    runtime, store, docs, engine, plans = settlement
    report = DailySavings(runtime).calculate(
        "2026-10-22", "UTC", contract(), None, END + timedelta(days=4), START, None
    )
    report["finalized_at"] = (END + timedelta(days=4)).isoformat()
    store.put_day(report, docs, "2026-10-23")
    later = frame(start=START + timedelta(days=1))
    store.put_frames((later,), {"bindings": bindings(), "seeds": [], "location": None})
    with engine.begin() as connection:
        connection.execute(SAVINGS_DAYS.update().values(end="2026-10-25T00:00:00.000000+00:00"))
    thin(plans, docs, END + timedelta(days=5), "UTC")
    assert [row["frame"] for row in store.frames()] == [later.record()]
    with pytest.raises(OSError, match="Daily savings archive failed integrity checks"):
        store.day(finalized=True)
