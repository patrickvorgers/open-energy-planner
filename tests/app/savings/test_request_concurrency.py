"""Keep historical source identities paired during interleaved settlement reads."""

from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.core.calendar import day_window
from open_energy_planner.installation.location import SiteLocation
from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.savings.support.scenarios import START


@pytest.mark.parametrize("timezone", ["UTC", "Europe/Amsterdam"])
def test_recovery_waits_for_recorder_delay_after_local_midnight(tmp_path, timezone):
    """The newly closed day's final quarter is requested only after the existing Recorder delay."""
    first, end = day_window(START.date().isoformat(), timezone)
    gap = end - timedelta(minutes=15)
    report = {
        "start": first.isoformat(),
        "end": end.isoformat(),
        "scenarios": {
            "actual": {
                "missing": [
                    {
                        "start": gap.isoformat(),
                        "end": end.isoformat(),
                        "reason": "Required measurements are missing. Missing energy is not treated as zero.",
                    }
                ]
            }
        },
    }

    def next_day(*, after, before, finalized):
        """Return the supplied completed local day only when the requested bounds include it."""
        return report if after <= first < before and finalized is False else None

    with closing(SqlPlanRepository(tmp_path)) as repository:
        store = SimpleNamespace(
            engine=repository.engine,
            first_activation=lambda: START.isoformat(),
            day=next_day,
            frames=lambda *_args, **_kwargs: [],
        )
        runtime = SavingsRuntime(
            store, SimpleNamespace(exists=lambda _name: False), SimpleNamespace()
        )
        location = SiteLocation(52, 5, timezone)
        assert runtime._missing_capture(end + timedelta(minutes=5), location) is None
        assert runtime._missing_capture(end + timedelta(minutes=20), location) == (gap, end)


def test_recovery_pages_past_gap_free_days_without_advancing_an_unacknowledged_attempt(tmp_path):
    """Bound each read while reaching a later missing quarter and keeping its selection stable for receipt."""
    reports = [
        {
            "start": (START.replace(hour=0) + timedelta(days=index)).isoformat(),
            "end": (START.replace(hour=0) + timedelta(days=index + 1)).isoformat(),
            "scenarios": {"actual": {"missing": []}},
        }
        for index in range(6)
    ]
    gap = START + timedelta(days=5)
    reports[-1]["scenarios"]["actual"]["missing"] = [
        {
            "start": gap.isoformat(),
            "end": (gap + timedelta(minutes=15)).isoformat(),
            "reason": "Required measurements are missing. Missing energy is not treated as zero.",
        }
    ]
    queried = []

    def next_day(*, after, before, finalized):
        """Return an independently supplied chronological provisional day using the adapter's query contract."""
        queried.append(after)
        assert finalized is False
        return next(
            (
                report
                for report in reports
                if after.isoformat() <= report["start"] < before.isoformat()
            ),
            None,
        )

    with closing(SqlPlanRepository(tmp_path)) as repository:
        store = SimpleNamespace(
            engine=repository.engine,
            first_activation=lambda: START.isoformat(),
            day=next_day,
            frames=lambda *_args, **_kwargs: [],
        )
        runtime = SavingsRuntime(
            store, SimpleNamespace(exists=lambda _name: False), SimpleNamespace()
        )
        arguments = (START + timedelta(days=6), SiteLocation(52, 5, "UTC"))
        assert runtime._missing_capture(*arguments) is None
        assert len(queried) == 4
        assert runtime._missing_capture(*arguments) == (gap, gap + timedelta(minutes=15))
        assert runtime._missing_capture(*arguments) == (gap, gap + timedelta(minutes=15))


class InterleavedRuntime(SavingsRuntime):
    """Allow another reader to run exactly when a shared cache value is published."""

    def __setattr__(self, name, value):
        """Model a worker switch after publication without depending on scheduler timing."""
        super().__setattr__(name, value)
        observer = self.__dict__.get("on_cache_write")
        if name.startswith("_") and observer is not None:
            self.on_cache_write = None
            observer()


def test_interleaved_requests_keep_each_plans_original_bindings(tmp_path):
    """A reader of plan A cannot borrow plan B's cached bindings during B's publication."""
    selection = {"plan": "a"}
    originals = {
        "a": {"comparison_bindings": {"source": "original-a"}},
        "b": {"comparison_bindings": {"source": "original-b"}},
    }
    with closing(SqlPlanRepository(tmp_path)) as repository:
        store = SimpleNamespace(
            engine=repository.engine,
            first_activation=lambda: START.isoformat(),
            context_plan=lambda _start: selection["plan"],
            next_configuration=lambda *_arguments: None,
        )
        runtime = InterleavedRuntime(
            store,
            SimpleNamespace(exists=lambda _name: False),
            SimpleNamespace(get_context=lambda plan: originals[plan]),
        )
        arguments = (
            START + timedelta(hours=2),
            SimpleNamespace(batteries=()),
            SimpleNamespace(sources=()),
            SimpleNamespace(terms=()),
            (),
            None,
        )
        assert runtime.request(*arguments)["bindings"] == {"source": "original-a"}
        interleaved = []

        def read_original():
            """Revisit the previous plan while a competing reader publishes a different cache entry."""
            selection["plan"] = "a"
            interleaved.append(runtime.request(*arguments))

        selection["plan"] = "b"
        runtime.on_cache_write = read_original
        result = runtime.request(*arguments)
        assert result["bindings"] == {"source": "original-b"}
        assert interleaved[0]["bindings"] == {"source": "original-a"}
