"""Serialize complete archive mutations sharing the single app-owned engine."""

from contextlib import contextmanager
from threading import Lock, RLock
from weakref import WeakKeyDictionary

_registry_lock = Lock()
_engine_locks = WeakKeyDictionary()


@contextmanager
def lifecycle_transaction(engine):
    """Hold one reentrant engine guard until commit or rollback, including lazy context writes.

    Plan publication, document-backed maintenance and context backfill must not
    interleave existence checks with deletion or insertion. Independent engines
    remain independent. Multiple writable app processes are not supported.
    """
    with _registry_lock:
        guard = _engine_locks.get(engine)
        if guard is None:
            guard = RLock()
            _engine_locks[engine] = guard
    with guard, engine.begin() as connection:
        yield connection
