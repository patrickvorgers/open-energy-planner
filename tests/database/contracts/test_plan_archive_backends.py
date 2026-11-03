"""Identical immutable plan, reference and transaction contracts on real supported databases."""

import json

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError

from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    META,
    PARTS,
    PLANS,
    SCHEMA,
    VALUES,
    SqlPlanRepository,
    archive_engine,
)
from tests.database.support.backends import BACKENDS


@pytest.fixture(params=BACKENDS, ids=[item[0] for item in BACKENDS])
def archive(request, tmp_path):
    """Use disposable CI databases. Reset all OEP database tables, including forecast selection."""
    name, url = request.param
    engine = archive_engine(tmp_path) if name == "sqlite" else create_engine(url)
    SCHEMA.drop_all(engine)
    repository = SqlPlanRepository(tmp_path, engine=engine)
    try:
        yield repository, engine, tmp_path
    finally:
        SCHEMA.drop_all(engine)
        engine.dispose()


def plan(identity="plan-a", *, minute="00", changed_measurement=False):
    """Independent JSON fixture keeps precision, nulls, modes and forecasts. Only the measured value may differ."""
    return {
        "plan_id": identity,
        "generated_at": f"2026-10-22T10:{minute}:00.123456+02:00",
        "valid_until": "2026-10-22T09:00:00+00:00",
        "strategy": "minimize cost",
        "solution_status": "optimal",
        "steps": [{"battery_power_w": None, "mode": "follow_grid_target"}],
        "input_snapshot": {
            "measurement_evidence": [{"soc": 0.30000000000000004 if changed_measurement else 0.2}],
            "forecast_version": "forecast-unchanged",
            "price_version": "price-unchanged",
            "problem": {
                "intervals": [{"price": -0.12345678912345678, "demand": 0.4}],
                "limit": 5.0,
            },
            "contract": {"label": "Tést", "bonus": None},
        },
    }


def test_backend_exact_reconstruction_reuse_and_restart(archive):
    """Two plans reuse unchanged input blocks and recover original floats, timestamps and modes after reopen."""
    repository, engine, root = archive
    first = plan()
    repository.state["plan"] = first
    repository.save()
    with engine.connect() as connection:
        before_values = set(connection.scalars(select(VALUES.c.digest)))
    second = plan("plan-b", minute="15", changed_measurement=True)
    repository.state["plan"] = second
    repository.state["enabled"] = False
    repository.save()
    restored = SqlPlanRepository(root, engine=engine)
    assert restored.state["enabled"] is False
    assert restored.state["plan"] == second
    assert restored.get_plan("plan-a") == first
    read = restored.get_plan("plan-a")
    read["input_snapshot"]["problem"]["limit"] = 0
    assert restored.get_plan("plan-a") == first
    with engine.connect() as connection:
        after_values = set(connection.scalars(select(VALUES.c.digest)))
        assert before_values <= after_values
        assert len(after_values - before_values) == 3  # ID, generated time, measured evidence only.
        assert connection.scalar(select(func.count()).select_from(PLANS)) == 2


def test_backend_control_projection_skips_full_plans_and_reuses_shared_blocks(archive, monkeypatch):
    """One indexed query reads original controls across plans without reconstructing solver inputs."""
    repository, engine, _root = archive
    identities = []
    for plan_index, controls in enumerate([None, [], ["sensor.heat"], ["sensor.heat"]]):
        document = plan(f"controls-{plan_index}")
        if controls is not None:
            document["forecast_quality_controlled"] = controls
        repository.state["plan"] = document
        repository.save()
        identities.append(document["plan_id"])

    def reject_full_read(*args):
        """Protect the query path from reintroducing full historical plan reconstruction."""
        pytest.fail("Control projection must not reconstruct full plans")

    monkeypatch.setattr(repository, "_read_record", reject_full_read)
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        """Count actual SQL reads independently of elapsed machine time."""
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        assert repository.controlled_entities([*identities, identities[-1]]) == ["sensor.heat"]
        assert len(statements) == 1
        assert repository.controlled_entities([]) == []
        assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    with pytest.raises(ValueError, match="publication evidence unavailable"):
        repository.controlled_entities([*identities, "missing-plan"])


@pytest.mark.parametrize("controls", [None, "sensor.heat", [42]])
def test_backend_control_projection_rejects_invalid_original_types(archive, controls):
    """A present malformed control list fails instead of granting an accuracy score."""
    repository, _engine, _root = archive
    document = plan()
    document["forecast_quality_controlled"] = controls
    repository.state["plan"] = document
    repository.save()
    with pytest.raises(ValueError, match="control list invalid"):
        repository.controlled_entities([document["plan_id"]])


def test_backend_control_projection_rejects_corrupt_leaf(archive):
    """Modified control bytes cannot silently remove exclusions from daily accuracy."""
    repository, engine, _root = archive
    document = plan()
    document["forecast_quality_controlled"] = ["sensor.heat"]
    repository.state["plan"] = document
    repository.save()
    with engine.begin() as connection:
        reference = connection.scalar(
            select(PARTS.c.value_ref)
            .join(PLANS, PLANS.c.record_ref == PARTS.c.owner)
            .where(
                PLANS.c.plan_id == document["plan_id"],
                PARTS.c.field == "forecast_quality_controlled",
            )
        )
        connection.execute(VALUES.update().where(VALUES.c.digest == reference).values(payload="[]"))
    with pytest.raises(ValueError, match="control hash mismatch"):
        repository.controlled_entities([document["plan_id"]])


def test_backend_plan_id_is_immutable_and_failed_save_restores_state(archive):
    """Reusing an ID with different content fails without modifying its original bytes or active pointer."""
    repository, engine, root = archive
    first = plan()
    repository.state["plan"] = first
    repository.save()
    repository.state["plan"] = plan(changed_measurement=True)
    repository.state["enabled"] = False
    with pytest.raises(ValueError, match="immutable"):
        repository.save()
    assert repository.state["plan"] == first and repository.state["enabled"] is True
    assert SqlPlanRepository(root, engine=engine).state == repository.state


def test_backend_transaction_failure_rolls_back_all_content_and_pointer(archive):
    """An error after writing new blocks but before state commit leaves no partial plan or orphaned evidence."""
    repository, engine, root = archive
    repository.state["plan"] = plan()
    repository.save()
    with engine.connect() as connection:
        counts = [
            connection.scalar(select(func.count()).select_from(table))
            for table in (VALUES, PARTS, PLANS)
        ]

    def fail_state(_connection, _cursor, statement, _parameters, _context, _many):
        """Inject failure at the final state update, after archive inserts have already executed."""
        if statement.lstrip().upper().startswith("UPDATE OEP_ARCHIVE_STATE"):
            raise OSError("fixture disk/server outage")

    event.listen(engine, "before_cursor_execute", fail_state)
    repository.state["plan"] = plan("plan-b", minute="15", changed_measurement=True)
    try:
        with pytest.raises(OSError):
            repository.save()
    finally:
        event.remove(engine, "before_cursor_execute", fail_state)
    assert repository.state["plan"]["plan_id"] == "plan-a"
    with engine.connect() as connection:
        assert counts == [
            connection.scalar(select(func.count()).select_from(table))
            for table in (VALUES, PARTS, PLANS)
        ]
    assert SqlPlanRepository(root, engine=engine).get_plan("plan-b") is None


def test_backend_hard_invalidation_never_resumes_commands_after_failed_commit(archive):
    """A failed revocation flush keeps commands unavailable in memory while preserving the historical plan."""
    repository, engine, _root = archive
    repository.state["plan"] = plan()
    repository.save()

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        """Fail the final state update, testing the real adapter's rollback and command revocation."""
        if statement.lstrip().upper().startswith("UPDATE OEP_ARCHIVE_STATE"):
            raise OSError("fixture outage")

    repository.invalidate_plan()
    assert repository.state["plan"] is None and repository.pending_invalidation
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(OSError):
            repository.save()
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert repository.state["plan"] is None and repository.pending_invalidation
    assert repository.get_plan("plan-a") == plan()
    repository.save()
    assert repository.state["plan"] is None and not repository.pending_invalidation


def test_backend_history_pages_equal_timestamps_without_duplicates(archive):
    """Case-sensitive IDs and a stable secondary sort paginate equal times without skipping or repeating plans."""
    repository, _engine, _root = archive
    for identity in ("plan-A", "plan-a", "plan-b"):
        repository.state["plan"] = plan(identity)
        repository.save()
    first = repository.list_plans(limit=2)
    second = repository.list_plans(limit=2, before=first[-1]["plan_id"])
    assert [plan_summary["plan_id"] for plan_summary in first + second] == [
        "plan-b",
        "plan-a",
        "plan-A",
    ]
    assert repository.get_plan("unknown") is None
    with pytest.raises(ValueError):
        repository.list_plans(limit=101)
    with pytest.raises(ValueError):
        repository.list_plans(before="unknown")


def test_backend_large_input_and_corrupt_block_detection(archive):
    """Inputs exceeding MariaDB TEXT's 64KiB limit survive exactly. Later corruption is reported rather than hidden."""
    repository, engine, _root = archive
    expected = plan()
    expected["input_snapshot"]["forecast"] = [0.123456789] * 10000
    repository.state["plan"] = expected
    repository.save()
    assert repository.get_plan("plan-a") == expected
    with engine.begin() as connection:
        ref = connection.scalar(
            select(VALUES.c.digest).where(VALUES.c.payload == '"forecast-unchanged"')
        )
        connection.execute(
            VALUES.update().where(VALUES.c.digest == ref).values(payload='"altered"')
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        repository.get_plan("plan-a")


def test_backend_rejects_unknown_schema_without_clearing_content(archive):
    """A future schema is rejected on reopen without resetting the active state or historical rows."""
    repository, engine, root = archive
    repository.state["plan"] = plan()
    repository.save()
    with engine.begin() as connection:
        connection.execute(META.update().values(version=999))
    with pytest.raises(ValueError, match="schema version"):
        SqlPlanRepository(root, engine=engine)
    assert repository.get_plan("plan-a") == plan()


def test_backend_foreign_keys_protect_referenced_input_blocks(archive):
    """Every backend rejects deletion of a still-referenced value, preserving complete plan reconstruction."""
    repository, engine, _root = archive
    repository.state["plan"] = plan()
    repository.save()
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(VALUES.delete())
    assert repository.get_plan("plan-a") == plan()


def test_sqlite_backend_change_rejected_without_replacing_existing_history(tmp_path):
    """Changing the deployment URL cannot silently create an empty server archive in place of local history."""
    repository = SqlPlanRepository(tmp_path)
    repository.state["plan"] = plan()
    repository.save()
    repository.close()
    with pytest.raises(ValueError, match="backend changed"):
        SqlPlanRepository(
            tmp_path,
            database_url="postgresql+psycopg://user:secret@127.0.0.1:1/other",
        )
    reopened = SqlPlanRepository(tmp_path)
    try:
        assert reopened.get_plan("plan-a") == plan()
        assert "secret" not in (tmp_path / "archive-backend.json").read_text()
    finally:
        reopened.close()


def test_sqlite_abrupt_process_exit_rolls_back_unpublished_candidate(tmp_path):
    """A process exiting after block inserts but before pointer commit reopens with only the previous valid plan."""
    import subprocess
    import sys

    repository = SqlPlanRepository(tmp_path)
    repository.state["plan"] = plan()
    repository.save()
    repository.close()
    script = """
import os, sys
from sqlalchemy import event
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository
repository = SqlPlanRepository(sys.argv[1])
def interrupt(connection, cursor, statement, parameters, context, many):
    if statement.lstrip().upper().startswith("UPDATE OEP_ARCHIVE_STATE"):
        os._exit(23)
event.listen(repository.engine, "before_cursor_execute", interrupt)
repository.state["plan"]["plan_id"] = "uncommitted-plan"
repository.save()
"""
    import os
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )
    assert result.returncode == 23
    restored = SqlPlanRepository(tmp_path)
    try:
        assert restored.state["plan"] == plan()
        assert restored.get_plan("uncommitted-plan") is None
        with restored.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
    finally:
        restored.close()


def test_sqlite_durable_settings_without_unreleased_json_import(tmp_path):
    """A fresh archive uses durable settings and ignores unreleased JSON state without deleting development files."""
    legacy = {"instance_id": "retained-app-identity", "enabled": False, "plan": plan()}
    path = tmp_path / "demo-state.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    repository = SqlPlanRepository(tmp_path)
    try:
        assert repository.state["instance_id"] != "retained-app-identity"
        assert repository.state["enabled"] is True and repository.state["plan"] is None
        assert repository.list_plans() == [] and json.loads(path.read_text()) == legacy
        with repository.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar() == 2
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        repository.close()


def test_external_failure_is_sanitized_and_never_creates_local_archive(tmp_path):
    """An unreachable server fails startup without exposing a URL password or creating an empty SQLite fallback."""
    with pytest.raises(OSError) as failure:
        SqlPlanRepository(
            tmp_path,
            database_url="postgresql+psycopg://oep:never-log-this@127.0.0.1:1/oep",
        )
    assert "never-log-this" not in str(failure.value)
    assert not (tmp_path / "archive.sqlite3").exists()
