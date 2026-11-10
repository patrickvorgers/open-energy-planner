"""Observe actual contention on the shared engine guard without timing-based lock assumptions."""

from threading import Event, current_thread

from open_energy_planner.storage.adapters.lifecycle import transactions


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
