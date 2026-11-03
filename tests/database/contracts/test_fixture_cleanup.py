"""Keep failed shared-fixture preparation isolated from later database groups."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect

from tests.database.support import settlement as fixture_module
from tests.database.support.backends import BACKENDS


@pytest.mark.parametrize("backend", BACKENDS, ids=[item[0] for item in BACKENDS])
@pytest.mark.parametrize("failure", ["seed", "cleanup", "seed_cleanup"])
def test_settlement_failure_removes_tables_and_disposes_engine(
    monkeypatch, tmp_path, backend, failure
):
    """Setup failure drops partial tables, while failed cleanup still disposes every backend engine."""
    _name, url = backend
    engine = create_engine(url or f"sqlite:///{tmp_path / 'fixture.sqlite'}")
    disposed = []
    event.listen(engine, "engine_disposed", lambda engine: disposed.append(True))
    monkeypatch.setattr(fixture_module, "create_engine", lambda url: engine)
    monkeypatch.setattr(fixture_module, "archive_engine", lambda directory: engine)
    original_drop = fixture_module.SCHEMA.drop_all
    drops = []

    def drop_schema(engine):
        """Fail the final drop only after real preparation has created the shared schema."""
        drops.append(True)
        if failure != "seed" and len(drops) == 2:
            raise RuntimeError("Injected cleanup failure")
        original_drop(engine)

    def fail_seed(*args, **kwargs):
        """Interrupt setup after schema creation and retained plan/price writes."""
        raise RuntimeError("Injected seed failure")

    monkeypatch.setattr(fixture_module.SCHEMA, "drop_all", drop_schema)
    if failure != "cleanup":
        monkeypatch.setattr(fixture_module.SettlementStore, "put_frames", fail_seed)
    generator = fixture_module.settlement.__wrapped__(SimpleNamespace(param=backend), tmp_path)
    try:
        primary = "seed" if failure == "seed" else "cleanup"
        with pytest.raises(RuntimeError, match=f"Injected {primary} failure") as captured:
            next(generator)
            generator.close()
        if failure == "seed_cleanup":
            assert isinstance(captured.value.__context__, RuntimeError)
            assert str(captured.value.__context__) == "Injected seed failure"
            assert captured.value.__context__.__traceback__ is not None
        assert len(drops) == 2
        assert disposed == [True]
        if failure == "seed":
            assert not set(inspect(engine).get_table_names()) & {
                table.name for table in fixture_module.SCHEMA.tables.values()
            }
    finally:
        original_drop(engine)
        engine.dispose()
