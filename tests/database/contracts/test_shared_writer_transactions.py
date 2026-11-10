"""Concurrent domain writers preserve complete commits, rollback and responsive contract saves."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event, current_thread

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.contracts.storage import ContractStore
from open_energy_planner.prices.selection import ForecastPair, ForecastSelection
from open_energy_planner.prices.storage.adapters.selection_store import SqlAlchemySelectionStore
from open_energy_planner.savings.accounting import evaluate
from open_energy_planner.storage.adapters.activities import ActivityStore
from open_energy_planner.storage.adapters.documents import SqlDocuments
from open_energy_planner.storage.adapters.lifecycle import transactions
from open_energy_planner.storage.adapters.lifecycle.schema import PAIRS
from open_energy_planner.storage.writes import complete_write
from tests.app.savings.support.scenarios import END, START, bindings, contract, frame, problem
from tests.database.support.writer_guard import observe_guard


class WriterRollback(Exception):
    """Force rollback after the real competing store has executed its write."""


@pytest.mark.parametrize(
    "writer", ["activity", "prune", "restart", "frames", "settlement", "selection"]
)
@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
def test_domain_writer_and_contract_save_share_one_complete_guard(
    settlement, monkeypatch, writer, rollback
):
    """A held real domain write cannot fail a contract save or block reads and the event loop."""
    _runtime, store, _docs, engine, _plans = settlement
    documents = SqlDocuments(engine, "contracts")
    contracts = ContractStore(documents)
    original = contract(bonus=True)
    contracts.save(original)
    candidate = replace(
        original, bonuses=(replace(original.bonuses[0], prevent_battery_export=True),)
    )
    activity = ActivityStore(engine)
    row = {
        "id": "synthetic-writer",
        "started_at": START.isoformat(),
        "owner": "synthetic",
        "kind": "synthetic",
        "state": "running",
    }
    if writer in ("prune", "restart"):
        activity.write(row, START)
    selection = SqlAlchemySelectionStore(engine)
    selection.load("NL")
    selector = ForecastSelection(selection, "NL", "primary", "alternative")
    result, continuation = evaluate(
        problem(),
        frame(),
        bindings(),
        None,
        contract(),
        (),
        store.published(START, END, "NL", "EUR"),
        None,
    )
    result.update(market_area="NL", currency="EUR")
    new_frame = frame(start=END + timedelta(minutes=15))
    started, release = Event(), Event()
    probe = observe_guard(engine, monkeypatch, "contract-writer")
    statement_prefix = {
        "activity": "INSERT INTO oep_activities",
        "prune": "DELETE FROM oep_activities",
        "restart": "UPDATE oep_activities",
        "frames": "INSERT INTO oep_settlement_frames",
        "settlement": "INSERT INTO oep_settlement_state",
        "selection": "INSERT INTO " + selection.decisions.name,
    }[writer]

    def hold_real_write(_connection, _cursor, statement, _parameters, _context, _many):
        """Hold after real mutation, before commit or the deliberately failed rollback."""
        if current_thread().name.startswith("domain-writer") and statement.startswith(
            statement_prefix
        ):
            started.set()
            assert release.wait(10), "The competing domain writer was not released"
            if rollback:
                raise WriterRollback

    def write_domain():
        """Call the responsible public adapter rather than manufacturing a raw SQL lock."""
        try:
            if writer == "activity":
                activity.write(row, START)
            elif writer == "prune":
                activity.prune(START + timedelta(days=8))
            elif writer == "restart":
                activity.restart(END)
            elif writer == "frames":
                store.put_frames((new_frame,), {"synthetic": True})
            elif writer == "settlement":
                store.commit("synthetic-writer", result, continuation)
            else:
                selection.update(
                    "NL",
                    (ForecastPair(1, START, END, 0.02, -0.01, "a" * 64, "b" * 64),),
                    (),
                    START,
                    START - timedelta(days=30),
                    selector.decide,
                )
        except WriterRollback:
            assert rollback

    async def scenario():
        """Observe actual lock contention and independent serving before releasing the first writer."""
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="domain-writer") as writers,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="contract-writer") as savers,
        ):
            competing = writers.submit(write_domain)
            try:
                assert await asyncio.to_thread(started.wait, 5)
                loop = asyncio.get_running_loop()
                saved = loop.run_in_executor(savers, contracts.save, candidate)
                assert await asyncio.to_thread(probe.blocked.wait, 2)
                # A callback and a separate read must complete while both writes are still pending.
                heartbeat = asyncio.Event()
                loop.call_soon(heartbeat.set)
                await asyncio.wait_for(heartbeat.wait(), 1)
                assert (
                    await asyncio.wait_for(asyncio.to_thread(documents.read, "current.json"), 2)
                    == original.version
                )
                assert contracts.current == original
                assert not release.is_set() and not saved.done()
            finally:
                release.set()
                await asyncio.to_thread(competing.result, 5)
            await asyncio.wait_for(saved, 5)

    event.listen(engine, "after_cursor_execute", hold_real_write)
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", hold_real_write)
    assert contracts.current == candidate
    assert ContractStore(SqlDocuments(engine, "contracts")).current == candidate
    assert contracts.load(original.version) == original
    rows = activity.read(START - timedelta(days=1), START + timedelta(days=1))
    if writer == "activity":
        assert rows == ([] if rollback else [row])
    elif writer == "prune":
        assert rows == ([row] if rollback else [])
    elif writer == "restart":
        assert rows[0]["state"] == ("running" if rollback else "interrupted")
    elif writer == "frames":
        assert bool(store.frames(new_frame.start)) is not rollback
    elif writer == "settlement":
        assert store.state("synthetic-writer") == (None if rollback else continuation)
        assert len(store.results("synthetic-writer", START, END)) == (0 if rollback else 1)
    else:
        assert bool(selection.load("NL")) is not rollback
        with engine.connect() as connection:
            assert len(connection.execute(select(PAIRS).where(PAIRS.c.area == "NL")).all()) == (
                0 if rollback else 1
            )


def test_lazy_selection_initialization_shares_guard_but_initialized_reads_do_not(
    settlement, monkeypatch
):
    """Schema/index initialization waits before its first SQL, while ordinary initialized reads remain free."""
    _runtime, _store, _docs, engine, _plans = settlement
    initialized, lazy = SqlAlchemySelectionStore(engine), SqlAlchemySelectionStore(engine)
    assert initialized.load("NL") == {}
    probe = observe_guard(engine, monkeypatch, "selection-initialize")
    issued = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        """Record whether the lazy worker reached SQL before its transaction was admitted."""
        if current_thread().name.startswith("selection-initialize"):
            issued.append(statement)

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="selection-initialize"
        ) as workers:
            with transactions.lifecycle_transaction(engine):
                pending = workers.submit(lazy.load, "NL")
                assert probe.blocked.wait(2)
                assert issued == []
                assert initialized.load("NL") == {}
            assert pending.result(timeout=5) == {}
        assert issued and lazy.initialized
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)


def test_failed_contract_pointer_write_rolls_back_revision_and_releases_guard(settlement):
    """Failure after pointer mutation keeps the original contract and permits a fresh complete save."""
    _runtime, _store, _docs, engine, _plans = settlement
    documents = SqlDocuments(engine, "contracts")
    contracts = ContractStore(documents)
    original, candidate = contract(buy="0.3"), contract(buy="0.4")
    contracts.save(original)

    def fail_pointer(_connection, _cursor, statement, _parameters, _context, _many):
        """Fail after the real pointer UPDATE so its preceding new revision must roll back too."""
        if statement.startswith("UPDATE oep_documents"):
            raise SQLAlchemyError("synthetic-write-failure")

    event.listen(engine, "after_cursor_execute", fail_pointer)
    try:
        with pytest.raises(OSError, match="Document archive transaction failed"):
            asyncio.run(complete_write(contracts.save, candidate))
    finally:
        event.remove(engine, "after_cursor_execute", fail_pointer)
    assert contracts.current == original
    assert ContractStore(SqlDocuments(engine, "contracts")).current == original
    assert not documents.exists(candidate.version + ".json")
    assert asyncio.run(complete_write(contracts.save, candidate)) is None
    assert ContractStore(SqlDocuments(engine, "contracts")).current == candidate


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
def test_borrowed_frame_connection_keeps_its_callers_atomic_boundary(settlement, rollback):
    """A borrowed frame write and document either commit together or both disappear on failure."""
    _runtime, store, documents, _engine, _plans = settlement
    later = frame(start=END + timedelta(minutes=15))

    def write_together():
        """Borrow the existing guarded connection without creating or committing another transaction."""
        with documents.transaction():
            store.put_frames((later,), {"synthetic": True}, documents.pending.connection)
            documents.write("synthetic-borrowed.json", {"accepted": True})
            if rollback:
                raise WriterRollback

    if rollback:
        with pytest.raises(WriterRollback):
            write_together()
    else:
        write_together()
    assert bool(store.frames(later.start)) is not rollback
    assert documents.exists("synthetic-borrowed.json") is not rollback


def test_failed_selection_initialization_remains_retryable_and_releases_guard(settlement):
    """Failed schema inspection cannot claim initialization or prevent a later guarded attempt."""
    _runtime, _store, _docs, engine, _plans = settlement
    adapter = SqlAlchemySelectionStore(engine)

    def fail_inspection(_connection, _cursor, _statement, _parameters, _context, _many):
        """Simulate an unavailable backend during the first initialization statement."""
        raise SQLAlchemyError("synthetic-inspection-failure")

    event.listen(engine, "before_cursor_execute", fail_inspection)
    try:
        with pytest.raises(OSError, match="forecast_selection_storage_unavailable"):
            adapter.load("NL")
    finally:
        event.remove(engine, "before_cursor_execute", fail_inspection)
    assert not adapter.initialized
    with ThreadPoolExecutor(max_workers=1) as workers:
        assert workers.submit(adapter.load, "NL").result(timeout=5) == {}
    assert adapter.initialized
