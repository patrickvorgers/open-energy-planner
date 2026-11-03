"""Keep presentation independent of authoritative immutable-base selection."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from script.ci import scope_report

SELECTOR_TIMEOUT_SECONDS = 30


def test_renderer_preserves_captured_decisions_and_cannot_change_actions_outputs(
    monkeypatch, tmp_path
):
    """Render an unknown profile's captured rows verbatim without selecting suites or changing proof."""
    path = tmp_path / "results/ci/selection.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "profile": "captured-profile",
                    "policy_base": "1" * 40,
                    "tested_commit": "2" * 40,
                    "suites": [
                        {
                            "suite": "ci",
                            "required": True,
                            "included_in": None,
                            "reasons": ["metadata|name`\n.py"],
                        },
                        {
                            "suite": "performance-queries",
                            "required": False,
                            "included_in": None,
                            "reasons": [],
                        },
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    original = path.read_bytes()
    output = tmp_path / "output"
    output.write_text("profile=authoritative\n")
    summary = tmp_path / "summary.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    scope_report.main()
    rendered = summary.read_text()
    assert "Profile: `captured-profile`" in rendered
    assert "| ci | Selected | `metadata&#124;name&#96; .py` |" in rendered
    assert "| performance-queries | Not selected |" in rendered
    assert output.read_text() == "profile=authoritative\n"
    assert path.read_bytes() == original


@pytest.mark.parametrize("legacy_report", [None, {"profile": "full", "selected": ["native"]}])
def test_legacy_base_policy_keeps_its_existing_summary(monkeypatch, tmp_path, legacy_report):
    """An old base that already printed its table or took the fallback needs no new renderer payload."""
    path = tmp_path / "results/ci/selection.json"
    if legacy_report is not None:
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(legacy_report))
    summary = tmp_path / "summary.md"
    summary.write_text("Existing trusted-base summary.\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    scope_report.main()
    assert summary.read_text() == "Existing trusted-base summary.\n"


def test_standalone_selector_never_imports_candidate_reporter(monkeypatch, tmp_path):
    """An extracted policy runs alone under isolated Python and captures its own decisions."""
    root = Path(__file__).resolve().parents[2]
    trusted = tmp_path / "trusted"
    for name in (
        "script/ci/ci_scope.py",
        "script/ci/ci_groups.py",
        "script/ci/proof_git.py",
    ):
        target = trusted / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((root / name).read_text(encoding="utf-8"), encoding="utf-8")
    policy = trusted / "script/ci/ci_scope.py"
    candidate = tmp_path / "script/ci/ci_groups.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("raise RuntimeError('candidate group definitions executed')\n")
    (tmp_path / "scope_report.py").write_text("raise RuntimeError('candidate code executed')\n")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(policy)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=SELECTOR_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "results/ci/selection.json").read_text())
    assert "profile=full\n" in output.read_text()
    assert report["profile"] == report["summary"]["profile"] == "full"
    rows = {row["suite"]: row for row in report["summary"]["suites"]}
    assert rows["native"]["required"] is True
    assert rows["unit-app"]["included_in"] == "native"
    assert rows["performance-queries"]["required"] is False


@pytest.mark.parametrize("module_state", ["present", "missing", "broken"])
def test_actual_workflow_requires_a_working_reporter(monkeypatch, tmp_path, module_state):
    """The real workflow command renders captured evidence and fails if its required module is missing or broken."""
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/checks.yaml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Report authoritative CI selection\n", 1)[1].split(
        "      - name:", 1
    )[0]
    command = step.split("        run: ", 1)[1].strip()
    if module_state != "missing":
        reporter = tmp_path / "script/ci/scope_report.py"
        reporter.parent.mkdir(parents=True)
        reporter.write_text(
            (root / "script/ci/scope_report.py").read_text(encoding="utf-8")
            if module_state == "present"
            else "raise RuntimeError('broken reporter')\n",
            encoding="utf-8",
        )
    selection = tmp_path / "results/ci/selection.json"
    selection.parent.mkdir(parents=True)
    selection.write_text(
        json.dumps(
            {
                "summary": {
                    "profile": "ci",
                    "policy_base": "1" * 40,
                    "tested_commit": "2" * 40,
                    "suites": [
                        {
                            "suite": "ci",
                            "required": True,
                            "included_in": None,
                            "reasons": ["report-only change"],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=SELECTOR_TIMEOUT_SECONDS,
    )
    if module_state == "present":
        assert result.returncode == 0, result.stderr
        assert "| ci | Selected | `report-only change` |" in result.stdout
    else:
        assert result.returncode != 0
