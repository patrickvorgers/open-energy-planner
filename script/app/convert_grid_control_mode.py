"""One-off offline conversion of stored grid commands to the schema-3 plan contract."""

import argparse
import json
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.storage.adapters.lifecycle.schema import (
    META,
    PLAN_CONTEXTS,
    PLANS,
    STATE,
)
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    SCHEMA_VERSION,
    STATE_KEY,
    SqlPlanRepository,
    archive_engine,
    canonical,
    digest,
)
from open_energy_planner.storage.adapters.lifecycle.transactions import lifecycle_transaction
from open_energy_planner.storage.adapters.payloads import decode_payload, encode_payload

BATCH_SIZE = 32


def _rows(connection, table, *columns):
    """Read bounded pages without an open result cursor during writes to the same table."""
    previous = None
    while True:
        query = select(table.c.plan_id, *columns).order_by(table.c.plan_id).limit(BATCH_SIZE)
        if previous is not None:
            query = query.where(table.c.plan_id > previous)
        rows = connection.execute(query).mappings().all()
        if not rows:
            return
        yield from rows
        previous = rows[-1]["plan_id"]


def _convert(document):
    """Rewrite the old boolean in place, preserving all other step and document fields."""
    changed = False
    schema = document.get("schema")
    for step in document.get("steps", []):
        if "grid_active" in step:
            active = step.pop("grid_active")
            if type(active) is not bool or "grid_control_mode" in step:
                raise ValueError("Invalid or conflicting archived grid modes")
            step["grid_control_mode"] = "zero_grid" if active else "inactive"
            changed = True
        elif "grid_control_mode" in step or schema in (2, 3):
            if step.get("grid_control_mode") not in ("zero_grid", "inactive"):
                raise ValueError("Invalid or missing archived grid mode")
    if schema == 2:
        document["schema"] = 3
        changed = True
    return changed


def convert_archive(engine, *, apply=False):
    """Verify and count affected documents, optionally replacing them atomically by identity.

    Run with all app writers stopped. Reuse the archive codec and keep original
    publication IDs, state and activations. Rehash changed records and detached
    context payloads, then verify through the same storage reader before commit.
    """
    with lifecycle_transaction(engine) as connection:
        if (
            connection.scalar(select(META.c.version).where(META.c.id == STATE_KEY))
            != SCHEMA_VERSION
            or connection.scalar(select(STATE.c.id).where(STATE.c.id == STATE_KEY)) is None
        ):
            raise ValueError("Unsupported archive metadata or missing active state")
        plans, contexts = 0, 0
        for row in _rows(connection, PLANS, PLANS.c.record_ref):
            plan = SqlPlanRepository._read_record(connection, row["record_ref"], {})
            if plan.get("plan_id") != row["plan_id"]:
                raise ValueError("Archived plan identity mismatch")
            if not _convert(plan):
                continue
            plans += 1
            if apply:
                reference = SqlPlanRepository._store_plan_record(connection, plan)
                connection.execute(
                    PLANS.update()
                    .where(PLANS.c.plan_id == row["plan_id"])
                    .values(record_ref=reference)
                )
                if SqlPlanRepository._read_record(connection, reference, {}) != plan:
                    raise ValueError("Converted plan verification failed")
        for row in _rows(
            connection, PLAN_CONTEXTS, PLAN_CONTEXTS.c.payload, PLAN_CONTEXTS.c.payload_hash
        ):
            if row["payload"] is None:
                continue
            context = json.loads(decode_payload(row["payload"]))
            if digest(context) != row["payload_hash"] or context.get("plan_id") != row["plan_id"]:
                raise ValueError("Archived plan context integrity check failed")
            if not _convert(context):
                continue
            contexts += 1
            if apply:
                connection.execute(
                    PLAN_CONTEXTS.update()
                    .where(PLAN_CONTEXTS.c.plan_id == row["plan_id"])
                    .values(
                        payload=encode_payload(canonical(context)), payload_hash=digest(context)
                    )
                )
                updated = connection.execute(
                    select(PLAN_CONTEXTS.c.payload, PLAN_CONTEXTS.c.payload_hash).where(
                        PLAN_CONTEXTS.c.plan_id == row["plan_id"]
                    )
                ).one()
                if json.loads(
                    decode_payload(updated.payload)
                ) != context or updated.payload_hash != digest(context):
                    raise ValueError("Converted plan context verification failed")
        return plans, contexts


def main(argv=None):
    """Read the archive's existing backend binding without printing credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Existing OEP data directory")
    parser.add_argument("--apply", action="store_true", help="Write the verified conversion")
    args = parser.parse_args(argv)
    root = args.directory
    try:
        binding = json.loads((root / "archive-backend.json").read_text(encoding="utf-8"))
        url = binding["url"]
        if url is None and not (root / "archive.sqlite3").is_file():
            parser.exit(1, "The bound SQLite archive is missing. Restore the archive first.\n")
        engine = archive_engine(root, url)
        try:
            if not inspect(engine).has_table(META.name):
                parser.exit(1, "The active OEP archive was not found.\n")
            plans, contexts = convert_archive(engine, apply=args.apply)
        finally:
            engine.dispose()
    except (OSError, SQLAlchemyError, KeyError, TypeError, ValueError):
        parser.exit(
            1, "Archive conversion failed. Check the backend binding and archive integrity.\n"
        )
    print(f"{'Converted' if args.apply else 'Would convert'} plans: {plans}, contexts: {contexts}")


if __name__ == "__main__":
    main()
