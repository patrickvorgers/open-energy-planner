"""Verify the offline grid command conversion on every supported archive backend."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from custom_components.open_energy_planner.protocol import Plan
from open_energy_planner.storage.adapters.lifecycle.schema import PLAN_CONTEXTS, SCHEMA
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SqlPlanRepository,
    archive_engine,
)
from script.app.convert_grid_control_mode import convert_archive, main
from tests.database.support.backends import BACKENDS
from tests.ha_contract.support.commands import command_plan


@pytest.fixture(params=BACKENDS, ids=[name for name, _ in BACKENDS])
def archives(request, tmp_path):
    """Seed a disposable legacy original and a separate detached historical context."""
    _name, url = request.param
    engine = archive_engine(tmp_path, url)
    try:
        SCHEMA.drop_all(engine)
        repository = SqlPlanRepository(tmp_path, engine=engine)
        original = command_plan(datetime(2026, 11, 24, tzinfo=UTC), powers=(0, None))
        original.update(
            schema=2,
            independent_costs={"economic_cost": 1.23},
            expected_flows=[{"grid_import": 0.001, "grid_export": 0}],
            input_snapshot={"problem": {"intervals": [{"duration_h": 0.25}]}},
        )
        for step, active in zip(original["steps"], (False, True), strict=True):
            step.pop("grid_control_mode")
            step["grid_active"] = active
        repository.state["plan"] = original
        repository.save()
        with engine.begin() as connection:
            repository._write_context(connection, {**original, "plan_id": "detached"})
        yield engine, repository, original
    finally:
        try:
            SCHEMA.drop_all(engine)
        finally:
            engine.dispose()


def test_conversion_preserves_active_plan_original_and_detached_context(archives, tmp_path):
    """Migrate both boolean modes, preserving every other value and sharing the active plan ID."""
    engine, repository, original = archives
    old_context = repository.get_context("detached")
    assert convert_archive(engine) == (1, 2)
    assert repository.get_plan(original["plan_id"]) == original
    assert convert_archive(engine, apply=True) == (1, 2)
    assert convert_archive(engine, apply=True) == (0, 0)
    expected = deepcopy(original)
    expected["schema"] = 3
    for step, mode in zip(expected["steps"], ("inactive", "zero_grid"), strict=True):
        del step["grid_active"]
        step["grid_control_mode"] = mode
    reopened = SqlPlanRepository(tmp_path, engine=engine)
    assert reopened.state["plan"] == expected
    assert reopened.get_plan(original["plan_id"]) == expected
    assert [step.command.grid_control_mode for step in Plan.parse(expected, "demo-1").steps] == [
        "inactive",
        "zero_grid",
    ]
    assert reopened.get_context("detached") == {**old_context, "steps": expected["steps"]}


def test_invalid_context_rolls_back_plan_record_replacement(archives):
    """A failed integrity check leaves all original plan references readable and unchanged."""
    engine, repository, original = archives
    with engine.begin() as connection:
        connection.execute(
            PLAN_CONTEXTS.update()
            .where(PLAN_CONTEXTS.c.plan_id == "detached")
            .values(payload_hash="0" * 64)
        )
    with pytest.raises(ValueError, match="context integrity"):
        convert_archive(engine, apply=True)
    assert repository.get_plan(original["plan_id"]) == original


def test_missing_cli_binding_does_not_create_an_archive(tmp_path):
    """A wrong directory fails without silently converting or creating an empty SQLite archive."""
    with pytest.raises(SystemExit) as failure:
        main([str(tmp_path), "--apply"])
    assert failure.value.code == 1
    assert not (tmp_path / "archive.sqlite3").exists()
