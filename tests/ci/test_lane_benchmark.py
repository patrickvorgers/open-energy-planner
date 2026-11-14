"""Keep experimental resource allocation and original execution evidence bounded."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.ci import resource_probe
from script.ci.lane_benchmark import docker_arguments, resource_overlay, validate_probe


def test_overlay_preserves_private_supervisor_options_and_uncapped_databases():
    """Resource changes retain private engine isolation and never reduce an unlimited DB budget."""
    original = [
        "compose",
        "-p",
        "oep-supervisor-check-fixture",
        "-f",
        "compose.dev.yaml",
        "-f",
        "/temporary/private-ports.yaml",
        "up",
        "--detach",
        "--build",
        "dev",
    ]
    altered = docker_arguments(original, "/benchmark/resources.json", "supervisor")
    assert altered == [*original[:7], "-f", "/benchmark/resources.json", *original[7:]]
    assert docker_arguments(["info", "--format", "{{.ID}}"], "unused", "supervisor") == [
        "info",
        "--format",
        "{{.ID}}",
    ]
    assert resource_overlay("databases", True, "probe") == {"services": {}}
    assert resource_overlay("native", True, "probe")["services"]["ha-tests"] == {
        "cpus": 4,
        "mem_limit": "4g",
    }
    assert resource_overlay("supervisor", True, "probe")["services"]["dev"]["mem_limit"] == "8g"


def test_probe_keeps_failed_phases_and_ignores_nested_test_roots(tmp_path, monkeypatch):
    """A real phase failure remains visible in telemetry, while nested regressions are untouched."""
    original_path = Path

    def fixture_path(value):
        """Map Linux workload and cgroup roots into this test's isolated filesystem."""
        name = str(value)
        if name.startswith("/workspace"):
            return tmp_path / name.removeprefix("/workspace").lstrip("/")
        if name.startswith("/sys/fs/cgroup"):
            return tmp_path / "cgroup" / name.removeprefix("/sys/fs/cgroup").lstrip("/")
        return original_path(value)

    monkeypatch.setattr(resource_probe, "Path", fixture_path)
    monkeypatch.setattr(
        resource_probe.os, "uname", lambda: SimpleNamespace(nodename="fixture"), raising=False
    )
    item = SimpleNamespace(nodeid="tests/test_sample.py::test_expected", user_properties=[])
    session = SimpleNamespace(
        items=[item],
        config=SimpleNamespace(
            rootpath=tmp_path, option=SimpleNamespace(xmlpath="/workspace/results/ci/sample.xml")
        ),
    )
    item.session = session
    resource_probe.pytest_collection_finish(session)
    for name, outcome in (("setup", "passed"), ("call", "failed"), ("teardown", "passed")):
        phase = SimpleNamespace(when=name, outcome=outcome, duration=0.25)
        wrapper = resource_probe.pytest_runtest_makereport(item, None)
        next(wrapper)
        with pytest.raises(StopIteration):
            wrapper.send(SimpleNamespace(get_result=lambda phase=phase: phase))
    resource_probe.pytest_sessionfinish(session, 1)
    record = json.loads((tmp_path / "results/ci/sample.probe.json").read_text())
    assert record["selected"] == ["tests/test_sample.py::test_expected"]
    assert record["exit_code"] == 1
    assert record["outcomes"][item.nodeid]["call"] == {"outcome": "failed", "seconds": 0.25}
    nested = SimpleNamespace(
        config=SimpleNamespace(rootpath=tmp_path / "nested", option=session.config.option)
    )
    resource_probe.pytest_collection_finish(nested)
    assert not hasattr(nested, "resource_probe")


def test_lane_validation_rejects_missing_or_failed_execution_phases(tmp_path):
    """Selected identity equality and every actual phase are required, not only passing JUnit."""
    identity = "tests/test_sample.py::test_expected"
    (tmp_path / "native.xml").write_text(
        '<testsuites><testsuite><testcase name="test_expected"><properties>'
        f'<property name="oep_benchmark_nodeid" value="{identity}"/>'
        "</properties></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    path = tmp_path / "native.probe.json"
    record = {
        "selected": [identity],
        "exit_code": 0,
        "outcomes": {
            identity: {
                phase: {"outcome": "passed", "seconds": 1}
                for phase in ("setup", "call", "teardown")
            }
        },
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    assert validate_probe(tmp_path) == {"native.xml": [identity]}
    record["outcomes"][identity]["teardown"]["outcome"] = "failed"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="execution phases"):
        validate_probe(tmp_path)
