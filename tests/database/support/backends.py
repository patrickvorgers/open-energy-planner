"""One required backend matrix shared by every database contract and migration test."""

import os

EXTERNAL_BACKENDS = (
    ("postgresql", "OEP_TEST_POSTGRES_URL"),
    ("mariadb", "OEP_TEST_MARIADB_URL"),
    ("mysql", "OEP_TEST_MYSQL_URL"),
)
BACKENDS = [("sqlite", None)] + [
    (name, os.environ[key]) for name, key in EXTERNAL_BACKENDS if os.environ.get(key)
]
if (
    os.environ.get("OEP_REQUIRE_EXTERNAL_DATABASE_TESTS") == "1"
    and len(BACKENDS) != len(EXTERNAL_BACKENDS) + 1
):
    raise RuntimeError("Database CI requires SQLite, PostgreSQL, MariaDB and MySQL settings")
