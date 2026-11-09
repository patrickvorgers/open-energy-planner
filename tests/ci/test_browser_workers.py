"""Prove browser partition coverage, actual pytest outcomes and joined container failures."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from threading import Event
from xml.etree import ElementTree

import pytest

from script.ci import browser_suites, check_browser
from script.ci.pytest_suites import pytest_arguments

ROOT = Path(__file__).resolve().parents[2]
IDENTIFIERS = [f"tests/frontend/browser/test_cases.py::test_case[{index}]" for index in range(4)]
EXPECTED_CAUSES = {
    "missing_worker": (FileNotFoundError, "browser-collected.json"),
    "inventory": (ValueError, "Browser worker inventories differ"),
    "partition": (ValueError, "Invalid browser worker 1 collection"),
    # Both execution corruptions exercise the same combined metadata guard.
    "execution": (ValueError, "Browser worker 1 execution differs from collection"),
    "missing_phase": (ValueError, "Browser worker 1 execution differs from collection"),
    "duplicate": (ValueError, "Duplicated browser execution identity"),
    "skip": (ValueError, "Unsuccessful browser execution"),
}


def pytest_child(project, worker, *, collect_only, arguments=()):
    """Execute the real plugin in its own project without inheriting another pytest session's state."""
    return subprocess.run(
        [
            sys.executable,
            *pytest_arguments(
                "browser", worker=worker, collect_only=collect_only, arguments=arguments
            ),
        ],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def actual_browser_reports(tmp_path_factory):
    """Collect and execute real pytest children with independent files and the production partition plugin."""
    directory = tmp_path_factory.mktemp("browser-workers")
    output = directory / "reports"
    for worker in (0, 1):
        project = directory / f"worker-{worker}"
        source = project / "tests/frontend/browser/test_cases.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "import pytest\n@pytest.mark.parametrize('value',[0,1,2,3])\n"
            "def test_case(value):\n    assert value in (0,1,2,3)\n",
            encoding="utf-8",
        )
        for collect_only in (True, False):
            result = pytest_child(project, worker, collect_only=collect_only)
            assert result.returncode == 0, result.stdout + result.stderr
        shutil.copytree(project / "results/ci", output / f"browser-{worker}")
    return output


def test_real_browser_workers_execute_exactly_their_collected_cases(actual_browser_reports):
    """The actual pytest plugin preserves all parametrized cases exactly once and produces readable original JUnit."""
    records = browser_suites.collections(actual_browser_reports)
    assert records[0]["all"] == IDENTIFIERS
    assert records[0]["selected"] == [IDENTIFIERS[0], IDENTIFIERS[2]]
    assert records[1]["selected"] == [IDENTIFIERS[1], IDENTIFIERS[3]]
    browser_suites.combine_reports(actual_browser_reports)
    browser_suites.execution_reports(actual_browser_reports, combined=True)
    assert len(ElementTree.parse(actual_browser_reports / "ui.xml").findall(".//testcase")) == 4


def test_filtered_browser_run_allows_only_a_genuinely_empty_peer(tmp_path, actual_browser_reports):
    """Actual pytest filtering keeps the same complete selected identity and proves an empty worker with exit five."""
    output = tmp_path / "reports"
    for worker in (0, 1):
        project = tmp_path / f"worker-{worker}"
        shutil.copytree(
            actual_browser_reports.parent / f"worker-{worker}" / "tests", project / "tests"
        )
        for collect_only in (True, False):
            result = pytest_child(project, worker, collect_only=collect_only, arguments=("-k", "0"))
            assert result.returncode == (0 if worker == 0 else 5), result.stdout + result.stderr
        shutil.copytree(project / "results/ci", output / f"browser-{worker}")
    browser_suites.combine_reports(output)
    assert browser_suites.case_identities(ElementTree.parse(output / "ui.xml").getroot()) == [
        IDENTIFIERS[0]
    ]


@pytest.mark.parametrize("failed_phase", ["setup", "call", "teardown"])
def test_actual_browser_phase_failure_prevents_completion(tmp_path, failed_phase):
    """Real fixture and assertion failures retain diagnostics and cannot publish a successful browser union."""
    project = tmp_path / "project"
    source = project / "tests/frontend/browser/test_cases.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def lifecycle():\n"
        f"    assert {failed_phase != 'setup'!r}, 'Setup failed'\n"
        "    yield\n"
        f"    assert {failed_phase != 'teardown'!r}, 'Teardown failed'\n"
        "def test_case(lifecycle):\n"
        f"    assert {failed_phase != 'call'!r}, 'Assertion failed'\n",
        encoding="utf-8",
    )
    output = tmp_path / "reports"
    for worker in (0, 1):
        collected = pytest_child(project, worker, collect_only=True)
        assert collected.returncode == (0 if worker == 0 else 5), (
            collected.stdout + collected.stderr
        )
        executed = pytest_child(project, worker, collect_only=False)
        assert executed.returncode == (1 if worker == 0 else 5), executed.stdout + executed.stderr
        shutil.copytree(project / "results/ci", output / f"browser-{worker}")
    failed = json.loads((output / "browser-0/browser-executed.json").read_text())
    identity = "tests/frontend/browser/test_cases.py::test_case"
    assert failed["outcomes"][identity][failed_phase] == "failed"
    with pytest.raises(RuntimeError, match="browser execution"):
        browser_suites.combine_reports(output)
    assert not (output / "ui.xml").exists()


@pytest.mark.parametrize(
    "corruption",
    ["missing_worker", "inventory", "partition", "execution", "missing_phase", "duplicate", "skip"],
)
def test_browser_completion_rejects_partial_or_inconsistent_execution(
    tmp_path, actual_browser_reports, corruption
):
    """Each corruption reaches its responsible validator before it can count as coverage."""
    output = tmp_path / "reports"
    shutil.copytree(actual_browser_reports, output)
    worker = output / "browser-1"
    if corruption == "missing_worker":
        (worker / "browser-collected.json").unlink()
    elif corruption in {"inventory", "partition"}:
        path = worker / "browser-collected.json"
        record = json.loads(path.read_text())
        if corruption == "inventory":
            record["all"] = IDENTIFIERS[:2]
            record["selected"] = [IDENTIFIERS[1]]
        else:
            record["selected"] = [IDENTIFIERS[0]]
        path.write_text(json.dumps(record))
    elif corruption in {"execution", "missing_phase"}:
        path = worker / "browser-executed.json"
        record = json.loads(path.read_text())
        if corruption == "execution":
            record["selected"] = [IDENTIFIERS[0]]
        else:
            del record["outcomes"][IDENTIFIERS[1]]["teardown"]
        path.write_text(json.dumps(record))
    else:
        path = worker / "ui.xml"
        document = ElementTree.parse(path)
        case = document.find(".//testcase")
        if corruption == "duplicate":
            case.find("./properties/property").set("value", IDENTIFIERS[3])
        else:
            ElementTree.SubElement(case, "skipped")
        document.write(path)
    with pytest.raises(RuntimeError, match="browser") as failure:
        browser_suites.combine_reports(output)
    cause = failure.value.__cause__
    expected_type, expected_message = EXPECTED_CAUSES[corruption]
    assert isinstance(cause, expected_type)
    if corruption == "missing_worker":
        assert Path(cause.filename) == worker / expected_message
    else:
        assert str(cause) == expected_message


@pytest.mark.parametrize("failed_worker", [None, 0, 1])
def test_browser_workers_overlap_and_join_before_failure_or_combination(
    tmp_path, actual_browser_reports, monkeypatch, failed_worker
):
    """Both bounded containers launch and exit before failure propagates or complete reports are combined."""
    monkeypatch.chdir(tmp_path)
    shutil.copytree(actual_browser_reports, tmp_path / "results/ci")
    started = [Event(), Event()]
    finished = [Event(), Event()]

    def run(command, **options):
        """Observe the actual private mount and shared worker selection while returning retained execution evidence."""
        worker = int(command[command.index("--browser-shard") + 1])
        assert options == {"check": False}
        assert command[command.index("--entrypoint") + 2] == "ui-worker"
        assert "--no-deps" in command
        assert command[command.index("--volume") + 1] == (
            f"{tmp_path / 'results/ci' / f'browser-{worker}'}:/workspace/results/ci"
        )
        started[worker].set()
        assert started[1 - worker].wait(5)
        for name in ("browser-executed.json", "ui.xml"):
            shutil.copy2(
                actual_browser_reports / f"browser-{worker}" / name,
                tmp_path / "results/ci" / f"browser-{worker}" / name,
            )
        finished[worker].set()
        return subprocess.CompletedProcess(command, 1 if worker == failed_worker else 0)

    monkeypatch.setattr(check_browser.subprocess, "run", run)
    if failed_worker is None:
        check_browser.run_workers(["docker", "compose"])
        assert (tmp_path / "results/ci/ui.xml").exists()
    else:
        with pytest.raises(subprocess.CalledProcessError):
            check_browser.run_workers(["docker", "compose"])
        assert not (tmp_path / "results/ci/ui.xml").exists()
    assert all(event.is_set() for event in finished)
