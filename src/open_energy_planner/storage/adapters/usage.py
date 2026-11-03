"""Read capacity evidence from the active database without changing retained data."""

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.core.units import BYTES_PER_KIB

LOW_FREE_BYTES = BYTES_PER_KIB**3
LOW_FREE_FRACTION = 0.05


class CapacityStatus(StrEnum):
    """Describe measured capacity separately from unavailable or unknown evidence."""

    OK = "ok"
    LOW = "low"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass
class StorageUsage:
    """Hold bytes and measurement quality without exposing connection details or paths."""

    backend: str
    database_bytes: int | None = None
    database_estimated: bool = False
    disk_total_bytes: int | None = None
    disk_free_bytes: int | None = None
    measurement_failed: bool = False

    @property
    def status(self) -> CapacityStatus:
        """Warn on either capacity limit, even when another measurement failed."""
        if (
            self.disk_free_bytes is not None
            and self.disk_total_bytes
            and (
                self.disk_free_bytes < LOW_FREE_BYTES
                or self.disk_free_bytes / self.disk_total_bytes < LOW_FREE_FRACTION
            )
        ):
            return CapacityStatus.LOW
        if self.measurement_failed:
            return CapacityStatus.UNAVAILABLE
        if self.disk_free_bytes is None:
            return CapacityStatus.UNKNOWN
        return CapacityStatus.OK


def storage_usage(engine) -> StorageUsage:
    """Synchronously measure the active database using read-only filesystem or SQL operations.

    Async callers must run this function in a worker to avoid blocking the event loop.

    SQLite includes the main file, WAL and shared-memory sidecar logical sizes.
    External sizes cover the whole connected database, including non-OEP tables.
    Server filesystem space and quotas are deliberately unknown. Failures retain
    independent evidence and never expose exception text or fall back to local disk.
    """
    backend = engine.dialect.name
    usage = StorageUsage(backend, database_estimated=backend in ("mysql", "mariadb"))
    if backend == "sqlite":
        database = engine.url.database
        if not database or database == ":memory:" or engine.url.query.get("uri"):
            return usage
        path = Path(database)
        try:
            path = path.resolve()
        except (OSError, RuntimeError):
            usage.measurement_failed = True
            return usage
        try:
            usage.database_bytes = path.stat().st_size
            for suffix in ("-wal", "-shm"):
                try:
                    usage.database_bytes += Path(str(path) + suffix).stat().st_size
                except FileNotFoundError:
                    pass
        except OSError:
            usage.database_bytes = None
            usage.measurement_failed = True
        try:
            try:
                disk = shutil.disk_usage(path)
            except FileNotFoundError:
                disk = shutil.disk_usage(path.parent)
            usage.disk_total_bytes, usage.disk_free_bytes = disk.total, disk.free
        except OSError:
            usage.measurement_failed = True
        return usage
    query = {
        "postgresql": "SELECT pg_database_size(current_database())",
        "mysql": "SELECT COALESCE(SUM(data_length + index_length), 0) "
        "FROM information_schema.tables WHERE table_schema = DATABASE()",
        "mariadb": "SELECT COALESCE(SUM(data_length + index_length), 0) "
        "FROM information_schema.tables WHERE table_schema = DATABASE()",
    }.get(backend)
    if query is None:
        return usage
    try:
        with engine.connect() as connection:
            usage.database_bytes = int(connection.execute(text(query)).scalar_one())
    except (SQLAlchemyError, TypeError, ValueError):
        usage.measurement_failed = True
    return usage
