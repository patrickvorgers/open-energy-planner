"""Prove HA partitions preserve every scenario, shared-app serialization and process failures."""

import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from script.ci import check_ha, ha_shard


def test_two_ha_partitions_cover_each_case_once_and_keep_remote_app_serial():
    """Independent cases cover all four history variants. Remote plan/pause cases never overlap across processes."""
    identifiers = [
        *(
            f"tests/custom_component/integration/test_ha_chain.py::test_native_test_data_reuses_energy_ids_units_and_preferences[{variant}]"
            for variant in ("False-False", "False-True", "True-False", "True-True")
        ),
        "tests/custom_component/integration/test_ha_chain.py::test_remote_plan",
        "tests/custom_component/integration/test_ha_chain.py::test_remote_pause",
        "tests/custom_component/integration/thermal/test_thermal.py::test_evidence",
        "tests/custom_component/integration/test_ha_chain.py::test_other_case",
    ]
    partitions = []
    for worker in (0, 1):
        items = [SimpleNamespace(nodeid=identifier) for identifier in reversed(identifiers)]
        deselected = []
        config = SimpleNamespace(
            getoption=lambda name, worker=worker: worker,
            hook=SimpleNamespace(
                pytest_deselected=lambda items, deselected=deselected: deselected.extend(items)
            ),
        )
        ha_shard.pytest_collection_modifyitems(config, items)
        partitions.append({item.nodeid for item in items})
        assert len(items) + len(deselected) == len(identifiers)
        assert sum("test_native_test_data" in item.nodeid for item in items) == 2
    assert partitions[0] | partitions[1] == set(identifiers)
    assert not partitions[0] & partitions[1]
    assert all("test_remote_" not in identifier for identifier in partitions[1])


@pytest.mark.parametrize("failed_worker", [None, 0, 1])
def test_ha_processes_overlap_join_and_propagate_failure(monkeypatch, failed_worker):
    """Barriers require concurrent pytest processes. Every original file and independent report reaches both workers."""
    started = [Event(), Event()]
    finished = [Event(), Event()]

    def run(command, **kwargs):
        """Emulate two cooperating pytest children without running HA in the native image."""
        worker = int(command[command.index("--ha-shard") + 1])
        assert kwargs == {"check": False}
        assert "tests/custom_component/integration" in command
        assert f"--junitxml=results/ci/ha-{worker}.xml" in command
        started[worker].set()
        assert started[1 - worker].wait(5)
        finished[worker].set()
        return SimpleNamespace(returncode=1 if worker == failed_worker else 0)

    monkeypatch.setattr(check_ha.subprocess, "run", run)
    monkeypatch.setattr(check_ha.sys, "argv", ["check_ha.py"])
    assert check_ha.main() == (0 if failed_worker is None else 1)
    assert all(event.is_set() for event in finished)


def test_shared_remote_app_users_keep_serial_partition_contract():
    """Guard the concurrency boundary when adding scenarios that mutate the shared solver app."""
    root = Path(__file__).resolve().parents[1] / "custom_component/integration"
    for source in root.rglob("test_*.py"):
        module = ast.parse(source.read_text(encoding="utf-8"))
        for scenario in module.body:
            if not isinstance(scenario, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            shared_calls = [
                call
                for call in ast.walk(scenario)
                if isinstance(call, ast.Call)
                and (
                    isinstance(call.func, ast.Name)
                    and call.func.id in {"remote_reset", "remote_replan"}
                    or any(keyword.arg == "remote_url" for keyword in call.keywords)
                )
            ]
            if shared_calls:
                assert scenario.name.startswith("test_remote_"), scenario.name


@pytest.mark.parametrize(
    "results,expected",
    [
        ((0, 5), 0),
        ((5, 0), 0),
        ((5, 5), 1),
        ((1, 5), 1),
        ((5, 1), 1),
        ((2, 0), 1),
        ((0, 3), 1),
        ((4, 5), 1),
        ((-9, 0), 1),
    ],
)
def test_filtered_ha_run_allows_only_empty_peer_of_a_successful_worker(
    monkeypatch, results, expected
):
    """One passing filtered shard can have an empty peer. No tests, collection errors, interruption and killed processes still fail."""
    seen = []

    def run(command, **kwargs):
        """Return independently specified pytest process statuses for each actually launched worker."""
        worker = int(command[command.index("--ha-shard") + 1])
        seen.append(worker)
        return SimpleNamespace(returncode=results[worker])

    monkeypatch.setattr(check_ha.subprocess, "run", run)
    monkeypatch.setattr(check_ha.sys, "argv", ["check_ha.py", "-k", "test_remote_plan"])
    assert check_ha.main() == expected
    assert sorted(seen) == [0, 1]


def test_energy_http_startup_cannot_bypass_the_isolated_port_owner():
    """Every direct native Energy setup uses the central helper instead of the shared default port."""
    root = Path(__file__).resolve().parents[1] / "custom_component"
    owners = []
    for path in root.rglob("*.py"):
        source = ast.parse(path.read_text(encoding="utf-8"))
        for function in source.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(function):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "async_setup_component"
                    and len(call.args) >= 2
                    and isinstance(call.args[1], ast.Constant)
                    and call.args[1].value == "energy"
                ):
                    owners.append((path.relative_to(root).as_posix(), function.name))
    assert owners == [("support/chain.py", "setup_energy")]
