"""Keep historical source identities paired during interleaved settlement reads."""

from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace

from open_energy_planner.savings.runtime import SavingsRuntime
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
from tests.app.savings.support.scenarios import START


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
