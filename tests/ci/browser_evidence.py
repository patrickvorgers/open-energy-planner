"""Provide independent browser proof documents and an explicit controller workload boundary."""

import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from script.ci import check_browser


def write_browser_evidence(directory, *, collected_only=False):
    """Describe one passing independent case and its genuinely empty peer without changing other suite oracles."""
    directory = Path(directory)
    identity = "tests/frontend/browser/test_example.py::test_example"
    combined = ElementTree.Element("testsuites")
    for worker, selected in ((0, [identity]), (1, [])):
        location = directory / f"browser-{worker}"
        location.mkdir(parents=True, exist_ok=True)
        record = {"worker": worker, "all": [identity], "selected": selected}
        (location / "browser-collected.json").write_text(json.dumps(record), encoding="utf-8")
        if collected_only:
            continue
        executed = {
            **record,
            "outcomes": {
                name: {"setup": "passed", "call": "passed", "teardown": "passed"}
                for name in selected
            },
            "exit_code": 0 if selected else 5,
        }
        (location / "browser-executed.json").write_text(json.dumps(executed), encoding="utf-8")
        suite = ElementTree.Element("testsuite")
        for name in selected:
            case = ElementTree.SubElement(
                suite, "testcase", classname="independent", name="example"
            )
            properties = ElementTree.SubElement(case, "properties")
            ElementTree.SubElement(properties, "property", name="oep_nodeid", value=name)
        ElementTree.ElementTree(suite).write(location / "ui.xml", encoding="utf-8")
        combined.append(suite)
    if not collected_only:
        ElementTree.ElementTree(combined).write(directory / "ui.xml", encoding="utf-8")


def observe_browser_boundary(monkeypatch):
    """Keep controller mocks at the container-launch boundary while worker proof has actual subprocess tests."""

    def run_workers(compose):
        """Expose both private commands to the controller test's existing subprocess observer."""
        for worker in (0, 1):
            arguments = check_browser.worker_arguments(Path("results/ci"), worker)
            subprocess.run([*compose, *arguments], check=True)

    monkeypatch.setattr(check_browser, "run_workers", run_workers)
