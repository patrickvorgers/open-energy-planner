"""Serialize complete archive mutations sharing the single app-owned engine."""

from contextlib import contextmanager
from threading import Lock, RLock
from weakref import WeakKeyDictionary

_registry_lock = Lock()
_engine_locks = WeakKeyDictionary()


@contextmanager
def lifecycle_transaction(engine):
    """Hold one reentrant engine guard until commit or rollback, including lazy context writes.

    Plan publication, document stores, activity retention, settlement and price
    selection must not interleave existence checks with another writer. The guard
    starts before the first SQL and covers commit or rollback. Borrowed connections
    remain owned by their caller and ordinary reads do not take this guard.
    Independent engines remain independent. This guard cannot serialize another
    process or external database writer. Multiple writable app processes are not supported.
    """
    with _registry_lock:
        guard = _engine_locks.get(engine)
        if guard is None:
            guard = RLock()
            _engine_locks[engine] = guard
    with guard, engine.begin() as connection:
        yield connection
