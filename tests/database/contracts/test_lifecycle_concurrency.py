"""Publication, collection and lazy historical reads share complete lifecycle transactions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Event, current_thread

import pytest
from sqlalchemy import event, select

from open_energy_planner.storage.adapters.lifecycle import transactions
from open_energy_planner.storage.adapters.lifecycle.retention import thin
from open_energy_planner.storage.adapters.lifecycle.schema import (
    ACTIVATION_CONTEXTS,
    PLAN_CONTEXTS,
    STATE,
    VALUES,
)
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    canonical,
    digest,
)
from tests.app.savings.support.scenarios import START


class GuardProbe:
    """Observe real guard contention without changing the production synchronization contract."""

    def __init__(self, guard, contender, *, reject=False):
        """Wrap the existing engine guard and identify only the competing worker."""
        self.guard, self.contender, self.reject = guard, contender, reject
        self.attempted, self.blocked = Event(), Event()

    def __enter__(self):
        """Signal actual acquisition failure before performing the real blocking acquire."""
        contender = current_thread().name.startswith(self.contender)
        if contender:
            self.attempted.set()
        if not self.guard.acquire(blocking=False):
            if contender:
                self.blocked.set()
                if self.reject:
                    raise AssertionError(
                        "An existing context reader attempted the held lifecycle guard"
                    )
            self.guard.acquire()
        return self

    def __exit__(self, *_exception):
        """Release the same real guard after the complete transaction exits."""
        self.guard.release()


def observe_guard(engine, monkeypatch, contender, *, reject=False):
    """Install a test-only proxy around the existing engine's actual RLock."""
    with transactions._registry_lock:
        probe = GuardProbe(transactions._engine_locks[engine], contender, reject=reject)
        monkeypatch.setitem(transactions._engine_locks, engine, probe)
    return probe


def test_present_context_reads_do_not_wait_for_unrelated_lifecycle_writes(settlement, monkeypatch):
    """A verified existing context remains readable while another full lifecycle transaction holds the engine guard."""
    _runtime, _store, _documents, engine, plans = settlement
    expected = plans.get_context("original-plan")
    probe = observe_guard(engine, monkeypatch, "context-reader", reject=True)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="context-reader") as readers,
        transactions.lifecycle_transaction(engine),
    ):
        result = readers.submit(plans.get_context, "original-plan").result(timeout=10)
        assert result == expected
        assert not probe.attempted.is_set()


def test_publication_cannot_lose_a_known_leaf_to_concurrent_collection(settlement, monkeypatch):
    """A collector competing after the publisher reads a known orphan cannot delete its new reference."""
    _runtime, _store, documents, engine, plans = settlement
    original = plans.get_plan("original-plan")
    database_errors = []

    def remember_constraint(error_context):
        """Retain only public error classes/codes, never query parameters or measurements."""
        error = error_context.original_exception
        database_errors.append((type(error).__name__, getattr(error, "sqlstate", None)))

    leaf = "synthetic energy leaf awaiting publication"
    document = {
        **deepcopy(original),
        "plan_id": "concurrent-publication",
        "generated_at": (START + timedelta(minutes=15)).isoformat(),
        "extra": leaf,
    }
    with engine.begin() as connection:
        connection.execute(VALUES.insert().values(digest=digest(leaf), payload=canonical(leaf)))
    documents.write("retention.json", {"garbage_pending": True})
    probe = observe_guard(engine, monkeypatch, "collector")
    read_known, release_publication, collector_started, collector_finished = (
        Event(),
        Event(),
        Event(),
        Event(),
    )

    def pause_known_read(_connection, _cursor, statement, _parameters, _context, _many):
        """Hold the actual known-leaf SELECT result before new PARTS references are inserted."""
        if (
            current_thread().name.startswith("publication")
            and statement.startswith("SELECT")
            and "oep_archive_values.digest" in statement
        ):
            read_known.set()
            assert release_publication.wait(10), "Publication barrier was not released"

    def publish():
        """Exercise the real public save and active-pointer transaction."""
        plans.state["plan"] = document
        plans.save()

    def collect():
        """Signal contention before entering the actual maintenance transaction."""
        collector_started.set()
        try:
            return thin(plans, documents, START + timedelta(days=2), "UTC")
        finally:
            collector_finished.set()

    event.listen(engine, "after_cursor_execute", pause_known_read)
    event.listen(engine, "handle_error", remember_constraint)
    try:
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="publication") as publishers,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="collector") as collectors,
        ):
            publication = publishers.submit(publish)
            try:
                assert read_known.wait(10)
                collection = collectors.submit(collect)
                assert collector_started.wait(10)
                assert probe.attempted.wait(10)
                assert probe.blocked.wait(10)
                assert not collector_finished.is_set()
                assert not release_publication.is_set()
            finally:
                release_publication.set()
            try:
                publication.result(timeout=10)
            except OSError as error:
                error.add_note(f"Observed database error classes/codes: {database_errors}")
                raise
            collection.result(timeout=10)
    finally:
        event.remove(engine, "after_cursor_execute", pause_known_read)
        event.remove(engine, "handle_error", remember_constraint)
    assert database_errors == []
    assert plans.get_plan("concurrent-publication") == document
    assert plans.get_plan("original-plan") == original
    with engine.connect() as connection:
        assert connection.scalar(select(STATE.c.plan_id)) == "concurrent-publication"
        assert connection.scalar(
            select(VALUES.c.payload).where(VALUES.c.digest == digest(leaf))
        ) == canonical(leaf)


def test_lazy_context_and_backfill_commit_one_verified_context(settlement, monkeypatch):
    """A lazy overview read and background backfill cannot both insert the same old plan context."""
    _runtime, _store, _documents, engine, plans = settlement
    reader = SqlPlanRepository(".", engine=engine)
    expected = plans.get_context("original-plan")
    database_errors = []

    def remember_constraint(error_context):
        """Retain only the underlying duplicate error identity for causal RED evidence."""
        error = error_context.original_exception
        database_errors.append((type(error).__name__, getattr(error, "sqlstate", None)))

    with engine.begin() as connection:
        connection.execute(ACTIVATION_CONTEXTS.delete())
        connection.execute(PLAN_CONTEXTS.delete())
    probe = observe_guard(engine, monkeypatch, "backfill")
    missing_context, release_reader, backfill_started, backfill_finished = (
        Event(),
        Event(),
        Event(),
        Event(),
    )

    def paused_context(_connection, _cursor, statement, _parameters, _context, _many):
        """Pause the real _write_context existence SELECT before its INSERT."""
        if current_thread().name.startswith("lazy-reader") and statement.startswith(
            "SELECT oep_plan_contexts.plan_id "
        ):
            missing_context.set()
            assert release_reader.wait(10), "Lazy-read barrier was not released"

    def backfill():
        """Compete through a separate repository sharing the app-owned engine."""
        backfill_started.set()
        try:
            return plans.preserve_history()
        finally:
            backfill_finished.set()

    event.listen(engine, "after_cursor_execute", paused_context)
    event.listen(engine, "handle_error", remember_constraint)
    try:
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="lazy-reader") as readers,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="backfill") as writers,
        ):
            lazy = readers.submit(reader.get_context, "original-plan")
            try:
                assert missing_context.wait(10)
                preserved = writers.submit(backfill)
                assert backfill_started.wait(10)
                assert probe.attempted.wait(10)
                assert probe.blocked.wait(10)
                assert not backfill_finished.is_set()
                assert not release_reader.is_set()
            finally:
                release_reader.set()
            try:
                assert lazy.result(timeout=10) == expected
            except OSError as error:
                error.add_note(f"Observed database error classes/codes: {database_errors}")
                raise
            preserved.result(timeout=10)
    finally:
        event.remove(engine, "after_cursor_execute", paused_context)
        event.remove(engine, "handle_error", remember_constraint)
    assert database_errors == []
    assert plans.get_context("original-plan") == expected
    with engine.connect() as connection:
        assert connection.scalar(select(PLAN_CONTEXTS.c.payload_hash)) == digest(expected)


def test_context_integrity_errors_are_not_suppressed_or_published(settlement, monkeypatch):
    """An unrelated real NOT NULL violation rolls back lazy creation rather than being mistaken for a winning duplicate."""
    _runtime, _store, _documents, engine, plans = settlement
    expected = plans.get_context("original-plan")
    with engine.begin() as connection:
        connection.execute(ACTIVATION_CONTEXTS.delete())
        connection.execute(PLAN_CONTEXTS.delete())
    write_context = plans._write_context

    def invalid_context(connection, _plan):
        """Trigger a genuine integrity failure unrelated to a duplicate context key."""
        connection.execute(PLAN_CONTEXTS.insert().values(plan_id="invalid-context"))

    monkeypatch.setattr(plans, "_write_context", invalid_context)
    with pytest.raises(OSError, match="Historical calculation archive unavailable"):
        plans.get_context("original-plan")
    with engine.connect() as connection:
        assert list(connection.scalars(select(PLAN_CONTEXTS.c.plan_id))) == []
    monkeypatch.setattr(plans, "_write_context", write_context)
    assert plans.get_context("original-plan") == expected
