"""Execute trusted workflow fallbacks against old and grouped immutable base layouts."""

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.parametrize(
    "workflow,script",
    [
        ("verified-commits.yaml", "check_commit_signatures.py"),
        ("ci-scope-labels.yaml", "label_ci_scope.py"),
    ],
)
@pytest.mark.parametrize("layout", ["legacy", "grouped", "both", "missing"])
def test_trusted_base_layout_selects_available_isolated_entrypoint(
    tmp_path, workflow, script, layout
):
    """New workflows accept an older base, prefer grouped code and fail when neither entrypoint exists."""
    root = Path(__file__).resolve().parents[2]
    source = (root / ".github/workflows" / workflow).read_text(encoding="utf-8")
    command = dedent(source.rsplit("        run: |\n", 1)[1])
    assert "python3 -I" in command
    for area, enabled in (
        ("script", layout in {"legacy", "both"}),
        ("script/ci", layout in {"grouped", "both"}),
    ):
        if enabled:
            path = tmp_path / area / script
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("print(" + repr(area) + ")\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if layout == "missing":
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ("script" if layout == "legacy" else "script/ci")


@pytest.mark.parametrize("script", ["check_commit_signatures", "label_ci_scope"])
@pytest.mark.parametrize("exit_status", [0, 1, 2])
def test_legacy_main_workflow_delegates_to_grouped_trusted_base(tmp_path, script, exit_status):
    """Main's old entrypoint executes only the checked-out base module under isolated Python."""
    import sys

    root = Path(__file__).resolve().parents[2]
    target = tmp_path / "script" / "ci"
    target.mkdir(parents=True)
    (target.parent / "__init__.py").write_text("")
    (target / "__init__.py").write_text("")
    (target / f"{script}.py").write_text(
        'def main():\n    print("trusted base")\n    return ' + str(exit_status) + "\n"
    )
    wrapper = target.parent / f"{script}.py"
    wrapper.write_text((root / "script" / f"{script}.py").read_text())
    result = subprocess.run(
        [sys.executable, "-I", str(wrapper)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_status, result.stderr
    assert result.stdout.strip() == "trusted base"


@pytest.mark.parametrize("script", ["check_commit_signatures", "label_ci_scope"])
def test_real_grouped_entrypoints_load_their_metadata_adapter_under_isolation(monkeypatch, script):
    """Actual privileged entrypoints import trusted helpers under isolated Python and fail closed without an event."""
    import sys

    monkeypatch.setenv("GITHUB_EVENT_NAME", "invalid-fixture-event")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-I", str(root / "script/ci" / (script + ".py"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "verification failed" in result.stdout or "Unable to publish CI scope" in result.stdout
    assert not result.stderr
