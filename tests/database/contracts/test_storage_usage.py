"""Capacity evidence, failure isolation and authentication on the active database."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import SingletonThreadPool

from open_energy_planner.app import create_app
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import archive_engine
from open_energy_planner.storage.adapters.usage import StorageUsage, storage_usage
from tests.database.support.backends import BACKENDS


@pytest.mark.parametrize("backend,url", BACKENDS, ids=[entry[0] for entry in BACKENDS])
def test_active_database_size_and_honest_free_space(backend, url, tmp_path):
    """Read actual size on every supported backend without using local disk for remote capacity."""
    engine = archive_engine(tmp_path, url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE oep_capacity_fixture (payload TEXT)"))
            connection.execute(
                text("INSERT INTO oep_capacity_fixture VALUES (:payload)"),
                {"payload": "evidence" * 1000},
            )
        usage = storage_usage(engine)
        assert usage.backend == backend
        assert usage.database_bytes > 0
        assert usage.database_estimated == (backend in ("mysql", "mariadb"))
        assert not usage.measurement_failed
        if backend == "sqlite":
            assert usage.disk_total_bytes > 0
            assert usage.disk_free_bytes >= 0
        else:
            assert usage.disk_free_bytes is None
            assert usage.disk_total_bytes is None
            assert usage.status == "unknown"
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT payload FROM oep_capacity_fixture")).scalar_one()
                == "evidence" * 1000
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS oep_capacity_fixture"))
        engine.dispose()


@pytest.mark.parametrize(
    "free_bytes,total_bytes,failed,expected",
    [
        (0, 100 * 1024**3, False, "low"),
        (1024**3 - 1, 10 * 1024**3, False, "low"),
        (2 * 1024**3, 100 * 1024**3, False, "low"),
        (1024**3, 20 * 1024**3, False, "ok"),
        (10 * 1024**3, 100 * 1024**3, False, "ok"),
        (None, None, False, "unknown"),
        (None, None, True, "unavailable"),
        (0, 100 * 1024**3, True, "low"),
        (10 * 1024**3, 100 * 1024**3, True, "unavailable"),
    ],
)
def test_capacity_thresholds_and_missing_evidence(free_bytes, total_bytes, failed, expected):
    """Independent byte and percentage limits warn strictly below either boundary, preserving failures."""
    usage = StorageUsage(
        "sqlite",
        disk_free_bytes=free_bytes,
        disk_total_bytes=total_bytes,
        measurement_failed=failed,
    )
    assert usage.status == expected


def test_sqlite_includes_sidecars_and_preserves_database_if_disk_read_fails(tmp_path, monkeypatch):
    """Include WAL/shared memory bytes without checkpointing and keep size after a disk probe failure."""
    engine = create_engine("sqlite:///" + str(tmp_path / "archive.sqlite3"))
    (tmp_path / "archive.sqlite3").write_bytes(b"a" * 100)
    (tmp_path / "archive.sqlite3-wal").write_bytes(b"b" * 20)
    (tmp_path / "archive.sqlite3-shm").write_bytes(b"c" * 10)

    def unavailable(_path):
        """Simulate an inaccessible filesystem without leaking its path."""
        raise OSError("private/path")

    monkeypatch.setattr("open_energy_planner.storage.adapters.usage.shutil.disk_usage", unavailable)
    usage = storage_usage(engine)
    assert usage.database_bytes == 130
    assert usage.disk_free_bytes is None
    assert usage.status == "unavailable"
    assert (tmp_path / "archive.sqlite3-wal").read_bytes() == b"b" * 20
    engine.dispose()


@pytest.mark.parametrize("free_bytes,expected", [(0, "low"), (10 * 1024**3, "unavailable")])
def test_missing_sqlite_file_preserves_volume_without_creating_database(
    tmp_path, monkeypatch, free_bytes, expected
):
    """Missing files remain unknown while an independently measured full volume still warns."""
    path = tmp_path / "missing.sqlite3"
    engine = create_engine("sqlite:///" + str(path))

    def volume(probe_path):
        """A missing file can still leave its containing volume accessible."""
        if probe_path == path:
            raise FileNotFoundError
        assert probe_path == tmp_path
        return SimpleNamespace(total=100 * 1024**3, free=free_bytes)

    monkeypatch.setattr("open_energy_planner.storage.adapters.usage.shutil.disk_usage", volume)
    usage = storage_usage(engine)
    assert usage.database_bytes is None
    assert usage.measurement_failed
    assert usage.disk_free_bytes == free_bytes
    assert usage.disk_total_bytes == 100 * 1024**3
    assert usage.status == expected
    assert not path.exists()
    engine.dispose()


def test_sqlite_symlink_measures_target_sidecars_and_volume(tmp_path, monkeypatch):
    """Follow SQLite's canonical filename so another volume's WAL and low capacity are not missed."""
    target_directory = tmp_path / "target-volume"
    target_directory.mkdir()
    target = target_directory / "active.sqlite3"
    target.write_bytes(b"a" * 100)
    target.with_name("active.sqlite3-wal").write_bytes(b"b" * 20)
    link = tmp_path / "archive.sqlite3"
    link.symlink_to(target)
    engine = create_engine("sqlite:///" + str(link))

    def target_volume(path):
        """Make the symlink directory look healthy while the actual database volume is full."""
        return SimpleNamespace(total=100 * 1024**3, free=0 if path == target else 10 * 1024**3)

    monkeypatch.setattr(
        "open_energy_planner.storage.adapters.usage.shutil.disk_usage", target_volume
    )
    usage = storage_usage(engine)
    assert usage.database_bytes == 120
    assert usage.disk_free_bytes == 0
    assert usage.status == "low"
    engine.dispose()


def test_sqlite_file_mount_does_not_use_parent_volume(tmp_path, monkeypatch):
    """A separately mounted database file can be full while its parent volume has ample space."""
    path = tmp_path / "archive.sqlite3"
    path.write_bytes(b"a" * 100)
    engine = create_engine("sqlite:///" + str(path))

    def volume(probe_path):
        """Represent distinct file-mount and parent-volume capacity through the filesystem API."""
        return SimpleNamespace(total=100 * 1024**3, free=0 if probe_path == path else 10 * 1024**3)

    monkeypatch.setattr("open_energy_planner.storage.adapters.usage.shutil.disk_usage", volume)
    usage = storage_usage(engine)
    assert usage.disk_free_bytes == 0
    assert usage.status == "low"
    engine.dispose()


@pytest.mark.parametrize(
    "url",
    ["sqlite://", "sqlite:///:memory:", "sqlite:///file:memdb?mode=memory&cache=shared&uri=true"],
)
def test_memory_database_never_reports_app_host_capacity(url, monkeypatch):
    """In-memory and URI databases never guess filesystem ownership."""
    engine = create_engine(url, poolclass=SingletonThreadPool)

    def unexpected(_path):
        """Fail if monitoring tries to assign the application host's disk to memory storage."""
        pytest.fail("Memory storage has no known volume")

    monkeypatch.setattr("open_energy_planner.storage.adapters.usage.shutil.disk_usage", unexpected)
    assert storage_usage(engine) == StorageUsage("sqlite")
    engine.dispose()


def test_external_query_failure_does_not_probe_local_disk(monkeypatch):
    """Failed remote queries return unavailable evidence and never reveal credentials or paths."""

    def unavailable():
        """Simulate a denied remote query containing private connection details."""
        raise OperationalError("secret/password@host", {}, Exception("private"))

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=unavailable)
    usage = storage_usage(engine)
    assert usage == StorageUsage("postgresql", measurement_failed=True)
    assert usage.status == "unavailable"


def test_api_requires_authentication_and_runs_probe_off_event_loop(tmp_path, monkeypatch):
    """Public shells reveal no storage evidence and authorized requests run probes in a worker."""
    main_thread = threading.get_ident()

    def probe(_engine):
        """Assert the blocking adapter is not executed on the HTTP event loop thread."""
        assert threading.get_ident() != main_thread
        return StorageUsage("postgresql", database_bytes=8192)

    monkeypatch.setattr("open_energy_planner.storage.adapters.usage.storage_usage", probe)

    async def scenario():
        """Use real app routing and middleware to verify authentication and the byte contract."""
        token = "storage-test-token-0123456789"
        async with TestClient(
            TestServer(create_app(tmp_path, token, source="home_assistant"))
        ) as client:
            assert (await client.get("/v1/storage")).status == 401
            assert (await client.get("/data-storage")).status == 200
            response = await client.get("/v1/storage", headers={"Authorization": f"Bearer {token}"})
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert await response.json() == {
                "backend": "postgresql",
                "database_bytes": 8192,
                "database_estimated": False,
                "disk_total_bytes": None,
                "disk_free_bytes": None,
                "measurement_failed": False,
                "status": "unknown",
            }

    asyncio.run(scenario())
