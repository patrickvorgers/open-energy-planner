"""Private cross-backend maintenance journal and database preflight, outside the moving archive."""

import json
from enum import StrEnum
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from .files import atomic_json
from .migration import _distinct_databases
from .sqlalchemy import (
    META,
    PRIVATE_DATABASE_MODE,
    SCHEMA,
    SCHEMA_VERSION,
    STATE,
    STATE_KEY,
    SqlPlanRepository,
    archive_engine,
)

JOURNAL_FILE = "database-migration.json"
SELECTION_FILE = "database-selection.json"


class MigrationPhase(StrEnum):
    """Durable maintenance states shared by recovery and runtime orchestration."""

    PREPARING = "preparing"
    COPYING = "copying"
    VERIFYING = "verifying"
    CHECKING = "checking"
    RESTARTING = "restarting"
    COMPLETED = "completed"
    RESTORED = "restored"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class DestinationCheckError(ValueError):
    """An application-owned connection explanation safe to show without driver details."""


def read_private(root, filename):
    """Read a bootstrap document strictly. Corrupt state must never select an empty archive."""
    path = Path(root) / filename
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_private(root, filename, document):
    """Atomically persist credentials and maintenance intent with owner-only permissions."""
    atomic_json(Path(root) / filename, document, mode=PRIVATE_DATABASE_MODE)


def selected_url(root, configured_url):
    """Prefer explicit UI selection, including local SQLite, over stale container options."""
    selection = read_private(root, SELECTION_FILE)
    return selection["url"] if selection is not None else configured_url


def describe(url):
    """Expose only database kind. Never echo user-controlled hosts, paths, options or credentials."""
    if not url:
        return "SQLite"
    from sqlalchemy.engine import make_url

    return {"postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB"}[
        make_url(url).get_backend_name()
    ]


def connection_description(url):
    """Identify the server and database for authenticated confirmation without credentials or URL options."""
    if not url:
        return "SQLite: Local database"
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    server = parsed.host or "Local socket"
    if parsed.port:
        server += f":{parsed.port}"
    return f"{describe(url)}: {server} / {parsed.database}"


def activate(root, url):
    """Reconcile private selection and repository identity from durable release intent."""
    engine = archive_engine(root, url)
    try:
        location = {
            "driver": engine.url.drivername,
            "host": engine.url.host,
            "port": engine.url.port,
            "database": Path(engine.url.database).name
            if engine.url.drivername == "sqlite"
            else engine.url.database,
        }
        write_private(root, SELECTION_FILE, {"url": url})
        write_private(root, "archive-backend.json", {"location": location, "url": url})
    finally:
        engine.dispose()


def recover_source(root):
    """Restore only unreleased source selection. Post-release rollback needs reverse migration."""
    journal = read_private(root, JOURNAL_FILE)
    if journal is None or journal["released"]:
        raise ValueError("No unreleased migration is available for source recovery")
    activate(root, journal["source_url"])
    write_private(root, JOURNAL_FILE, {**journal, "phase": MigrationPhase.RESTORED, "error": None})


def requested_recovery(root):
    """Honor the optional Supervisor rescue flag once per unreleased operation, never after release."""
    journal = read_private(root, JOURNAL_FILE)
    if journal is None or journal["released"] or journal["phase"] == MigrationPhase.RESTORED:
        return False
    recover_source(root)
    return True


def check_destination(root, source_url, target_url):
    """Check connectivity and aliases without rebuilding tables or returning driver errors."""
    source = target = None
    try:
        source = archive_engine(root, source_url)
        target = archive_engine(root, target_url)
        with target.connect() as connection:
            connection.execute(select(1))
        try:
            _distinct_databases(source, target)
        except ValueError:
            raise DestinationCheckError(
                "Source and destination are the same database. Choose a different destination."
            ) from None
        existing = set(inspect(target).get_table_names())
        return {
            "backend": describe(target_url),
            "destination": connection_description(target_url),
            "contains_oep_data": bool(existing & set(SCHEMA.tables)),
        }
    except DestinationCheckError:
        raise
    except (OSError, ValueError, TypeError, SQLAlchemyError):
        raise DestinationCheckError(
            "Connection check failed. Check the destination, credentials and database permissions. Source and destination must differ."
        ) from None
    finally:
        if source is not None:
            source.dispose()
        if target is not None:
            target.dispose()


def check_candidate(root, url):
    """Validate transferred state and a rolled-back write before application construction or release."""
    engine = archive_engine(root, url)
    try:
        if not all(inspect(engine).has_table(table.name) for table in SCHEMA.sorted_tables):
            raise ValueError("Candidate schema is incomplete")
        with engine.connect() as connection:
            if (
                connection.scalar(select(META.c.version).where(META.c.id == STATE_KEY))
                != SCHEMA_VERSION
                or connection.scalar(select(STATE.c.id).where(STATE.c.id == STATE_KEY)) is None
            ):
                raise ValueError("Candidate metadata or application state is invalid")
        repository = SqlPlanRepository(root, engine=engine)
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(META.insert().values(id=-1, version=SCHEMA_VERSION))
            finally:
                transaction.rollback()
        if not repository.state or not repository.state.get("instance_id"):
            raise ValueError("Candidate state is incomplete")
        return engine
    except Exception:
        engine.dispose()
        raise


def open_runtime_engine(root, url, *, migrate_database=False):
    """Transfer ownership of a normally validated repository engine to the maintenance host."""
    repository = SqlPlanRepository(root, database_url=url, migrate_database=migrate_database)
    return repository.engine
