"""Validate real query-suite partitioning only where application dependencies are installed."""

import os
import subprocess
import sys
from pathlib import Path

from script.ci.pytest_suites import pytest_arguments

ROOT = Path(__file__).resolve().parents[3]


def test_real_collection_partitions_volume_cases_without_losing_index_repair():
    """All twelve four-backend volume variants collect only in performance while small index repairs stay functional."""
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "OEP_REQUIRE_EXTERNAL_DATABASE_TESTS": "1",
        "OEP_TEST_POSTGRES_URL": "postgresql://unused/unused",
        "OEP_TEST_MARIADB_URL": "mariadb+pymysql://unused/unused",
        "OEP_TEST_MYSQL_URL": "mysql+pymysql://unused/unused",
    }
    collected = {}
    for suite in ("databases", "performance-queries"):
        result = subprocess.run(
            [sys.executable, *pytest_arguments(suite, collect_only=True)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        collected[suite] = {
            line
            for line in result.stdout.splitlines()
            if line.startswith("tests/") and "::" in line
        }
    expected = {
        f"tests/database/performance/test_price_queries.py::test_five_year_history_uses_bounded_reads_and_recent_scoring[{backend}-{identified}]"
        for backend in ("sqlite", "postgresql", "mariadb", "mysql")
        for identified in (False, True)
    } | {
        f"tests/database/performance/test_savings_queries.py::test_three_year_period_query_aggregates_without_loading_all_detail[{backend}]"
        for backend in ("sqlite", "postgresql", "mariadb", "mysql")
    }
    assert expected <= collected["performance-queries"]
    assert all("/performance/" in case for case in collected["performance-queries"])
    assert not any("/performance/" in case for case in collected["databases"])
    assert (
        len(
            [
                case
                for case in collected["databases"]
                if "::test_existing_scoring_index_is_repaired_without_changing_evidence[" in case
            ]
        )
        == 8
    )
    assert (
        len(
            [
                case
                for case in collected["databases"]
                if "::test_compact_price_history_keeps_latest_buckets_and_excludes_old_errors["
                in case
            ]
        )
        == 8
    )
    assert (
        len(
            [
                case
                for case in collected["databases"]
                if "::test_sparse_multi_year_query_preserves_year_bounds_and_bounded_details["
                in case
            ]
        )
        == 4
    )
    assert collected["databases"].isdisjoint(collected["performance-queries"])
