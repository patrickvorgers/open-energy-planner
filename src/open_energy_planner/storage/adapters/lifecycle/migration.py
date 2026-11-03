"""Offline, explicitly destructive target rebuild using portable SQLAlchemy operations."""

from uuid import uuid4

from sqlalchemy import func, inspect, select, tuple_
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import (
    META,
    PLANS,
    SCHEMA,
    SCHEMA_VERSION,
    STATE,
    STATE_KEY,
    SqlPlanRepository,
)

MIGRATION_BATCH_ROWS = 250
PROBE_ID_LIMIT = 2**31 - 1


def _distinct_databases(source, target):
    """Refuse aliases to the source before deleting target data, including different hosts/users for one database."""
    if source.url.set(password=None) == target.url.set(password=None):
        raise ValueError("Source and target archive are the same database")
    if not inspect(target).has_table(META.name):
        return
    probe = -(uuid4().int % PROBE_ID_LIMIT + 1)
    with target.begin() as connection:
        connection.execute(META.insert().values(id=probe, version=SCHEMA_VERSION))
    try:
        with source.connect() as connection:
            same = connection.scalar(select(META.c.id).where(META.c.id == probe)) is not None
    finally:
        with target.begin() as connection:
            connection.execute(META.delete().where(META.c.id == probe))
    if same:
        raise ValueError("Source and target archive resolve to the same database")


def _batches(connection, table):
    """Read stable primary-key ordered batches without holding an entire archive in memory."""
    result = connection.execute(
        select(table)
        .order_by(*table.primary_key.columns)
        .execution_options(yield_per=MIGRATION_BATCH_ROWS)
    ).mappings()
    try:
        while rows := result.fetchmany(MIGRATION_BATCH_ROWS):
            yield [dict(row) for row in rows]
    finally:
        result.close()


def _copy_and_verify(original, destination, tables, progress=None):
    """Copy all selected tables and verify exact rows by primary key, independent of database sort order."""
    counts = {}
    total = sum(original.scalar(select(func.count()).select_from(table)) for table in tables)
    copied = 0
    checked = 0
    if progress:
        progress("copying", 0, total)
    for table in tables:
        count = 0
        for batch in _batches(original, table):
            destination.execute(table.insert(), batch)
            count += len(batch)
            copied += len(batch)
            if progress:
                progress("copying", copied, total)
        counts[table.name] = count
    if progress:
        progress("verifying", 0, total)
    for table in tables:
        keys = tuple(table.primary_key.columns)
        verified = 0
        batches = _batches(original, table)
        try:
            for batch in batches:
                expected = {tuple(row[key.name] for key in keys): row for row in batch}
                rows = destination.execute(
                    select(table).where(tuple_(*keys).in_(expected))
                ).mappings()
                actual = {tuple(row[key.name] for key in keys): dict(row) for row in rows}
                if expected != actual:
                    raise ValueError("Migrated archive content verification failed")
                verified += len(batch)
                checked += len(batch)
                if progress:
                    progress("verifying", checked, total)
        finally:
            batches.close()
        if (
            verified != counts[table.name]
            or destination.scalar(select(func.count()).select_from(table)) != verified
        ):
            raise ValueError("Migrated archive row count verification failed")
    return counts


def migrate_archive(source, target, *, overwrite_target=False, progress=None):
    """Rebuild only OEP archive tables, copy/verify every row, and retain the source unchanged.

    The app must be stopped: no source writers, planner or UI can run during this
    maintenance operation. Target DDL commits before the copy on every backend.
    Failure can leave the target empty, but it is never activated by this function.
    Unrelated target tables are not deleted. Explicit permission is mandatory.

    Returns:
        Copied row counts keyed by table name, after exact verification and commit.

    Raises:
        ValueError: Permission, source schema/content, alias or verification is invalid.
        OSError: A database operation fails. The source remains available for recovery.
    """
    if overwrite_target is not True:
        raise ValueError("Migration requires explicit target-overwrite permission")
    try:
        if not all(inspect(source).has_table(table.name) for table in SCHEMA.sorted_tables):
            raise ValueError("Source archive schema is incomplete")
        with source.connect() as connection:
            if (
                connection.scalar(select(META.c.version).where(META.c.id == STATE_KEY))
                != SCHEMA_VERSION
            ):
                raise ValueError("Unsupported source archive schema")
            if connection.scalar(select(STATE.c.id).where(STATE.c.id == STATE_KEY)) is None:
                raise ValueError("Source archive has no application state")
        _distinct_databases(source, target)
        # Check original content before destructive target DDL. Never optimize or repair it.
        reader = SqlPlanRepository(".", engine=source)
        with source.connect() as connection:
            for batch in _batches(connection, PLANS):
                for row in batch:
                    reader.get_plan(row["plan_id"])
        SCHEMA.drop_all(target)
        SCHEMA.create_all(target)
        with source.connect() as original, target.begin() as destination:
            counts = _copy_and_verify(original, destination, SCHEMA.sorted_tables, progress)
        return counts
    except SQLAlchemyError:
        raise OSError(
            "Archive migration failed. Source retained and target not activated"
        ) from None
