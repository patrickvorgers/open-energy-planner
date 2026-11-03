"""Execute trusted snapshot loading against legacy layouts, candidate shadows and unsafe archives."""

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from textwrap import dedent

import pytest

from script.ci.proof_git import base_test_only_profile

TIMEOUT_SECONDS = 30
BASE = "1" * 40
HEAD = "2" * 40


@pytest.mark.parametrize("layout", ["legacy", "modular"])
def test_reuse_executes_every_policy_dependency_from_the_same_base(
    monkeypatch, tmp_path, policy_archive, layout
):
    """A changed candidate initializer/helper cannot supply the immutable parent's suite definitions."""
    monkeypatch.setattr("script.ci.proof_git.tempfile.tempdir", str(tmp_path))
    policy = (
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
        "from script.ci.ci_groups import RESULT\n"
        "def candidate_reuse_profile(base, tested):\n"
        f"    assert (base, tested) == ('{BASE}', '{HEAD}')\n"
        "    assert sys.flags.isolated and sys.flags.no_site\n"
        "    return RESULT\n"
    )
    if layout == "legacy":
        policy = "def candidate_reuse_profile(base, tested):\n    return 'ci'\n"
    name = "script/ci_scope.py" if layout == "legacy" else "script/ci/ci_scope.py"
    archive = policy_archive({name: policy, "script/ci/ci_groups.py": "RESULT = 'ci'\n"})
    candidate = tmp_path / "script/ci"
    candidate.mkdir(parents=True)
    (candidate.parent / "__init__.py").write_text("raise RuntimeError('candidate initializer')\n")
    (candidate / "ci_groups.py").write_text("raise RuntimeError('candidate definitions')\n")
    original = subprocess.check_output

    def transport(command, **options):
        """Replace only immutable Git bytes and retain the real isolated interpreter."""
        if command[0] == "git":
            assert command == ["git", "archive", "--format=tar", BASE, "script"]
            return archive
        return original(command, **options)

    monkeypatch.setattr(subprocess, "check_output", transport)
    monkeypatch.chdir(tmp_path)
    assert base_test_only_profile(BASE, HEAD, selector="candidate_reuse_profile") == "ci"


def test_reuse_rejects_an_archive_escaping_the_trusted_directory(monkeypatch, tmp_path):
    """An unsafe member cannot write outside the snapshot or authorize focused reuse."""
    monkeypatch.setattr("script.ci.proof_git.tempfile.tempdir", str(tmp_path))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("../escape.py")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: output.getvalue())
    assert base_test_only_profile(BASE, HEAD) is None
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize(
    "case",
    [
        "modular",
        "legacy",
        "partial_failure",
        "missing",
        "missing_helper",
        "unsafe",
        "stale_helper",
        "stale_output",
    ],
)
def test_actual_workflow_loads_trusted_policy_and_discards_partial_failure(
    monkeypatch, tmp_path, policy_archive, case
):
    """The real selection step uses parent helpers and publishes full fallback instead of partial cheap outputs."""
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/checks.yaml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Select checks using the PR base policy\n", 1)[1].split(
        "      - name:", 1
    )[0]
    command = dedent(step.split("        run: |\n", 1)[1])
    policy = (
        "import os, sys\nfrom pathlib import Path\n"
        "assert sys.flags.isolated and sys.flags.no_site\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
        "from script.ci.ci_groups import RESULT\n"
        "Path(os.environ['GITHUB_OUTPUT']).write_text('profile=' + RESULT + '\\n')\n"
    )
    if case == "legacy":
        policy = "import os\nfrom pathlib import Path\nPath(os.environ['GITHUB_OUTPUT']).write_text('profile=ci\\n')\n"
    if case == "partial_failure":
        policy += "raise RuntimeError('failed after publishing partial selection')\n"
    if case == "stale_output":
        policy = "assert True\n"
    name = "script/ci_scope.py" if case == "legacy" else "script/ci/ci_scope.py"
    contents = policy_archive(
        {name: policy}
        if case in {"missing_helper", "stale_helper"}
        else {name: policy, "script/ci/ci_groups.py": "RESULT = 'ci'\n"}
    )
    if case == "unsafe":
        contents = policy_archive({"../../escape.py": "raise RuntimeError('unsafe archive')"})
    archive = tmp_path / "policy.tar"
    archive.write_bytes(contents)
    executable = tmp_path / "bin/git"
    executable.parent.mkdir()
    executable.write_text(
        "#!" + sys.executable + "\nimport os,sys\nfrom pathlib import Path\n"
        f"assert sys.argv[1:] == ['archive', '{BASE}', 'script']\n"
        "sys.stdout.buffer.write(Path(os.environ['FIXTURE_ARCHIVE']).read_bytes())\n"
    )
    executable.chmod(0o755)
    runner = tmp_path / "runner"
    runner.mkdir()
    stale_helper = runner / "oep-ci-policy/script/ci/ci_groups.py"
    stale_helper.parent.mkdir(parents=True)
    stale_helper.write_text("RESULT = 'ci'\n")
    stale_output = runner / "oep-ci-policy-output"
    stale_output.write_text("profile=ci\n")
    output = tmp_path / "output"
    candidate = tmp_path / "script/ci"
    candidate.mkdir(parents=True)
    (candidate.parent / "__init__.py").write_text("raise RuntimeError('candidate initializer')\n")
    (candidate / "ci_groups.py").write_text("raise RuntimeError('candidate helper')\n")
    for key, value in {
        "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"],
        "PR_BASE_SHA": BASE,
        "RUNNER_TEMP": str(runner),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_EVENT_NAME": "workflow_dispatch" if case == "missing" else "pull_request",
        "FIXTURE_ARCHIVE": str(archive),
    }.items():
        monkeypatch.setenv(key, value)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr
    lines = output.read_text().splitlines()
    if case in {"modular", "legacy"}:
        assert lines == ["profile=ci"]
    else:
        assert "profile=full" in lines and "profile=ci" not in lines
        assert "targets=app,native,ha-tests,ui-tests" in lines
    assert not (tmp_path / "escape.py").exists()
    assert not list(runner.glob("oep-ci-policy.*"))
    assert stale_helper.read_text() == "RESULT = 'ci'\n"
    assert stale_output.read_text() == "profile=ci\n"


def test_missing_base_helper_cannot_resolve_from_candidate(monkeypatch, tmp_path, policy_archive):
    """An incomplete trusted snapshot fails closed even when the candidate provides the missing helper."""
    monkeypatch.setattr("script.ci.proof_git.tempfile.tempdir", str(tmp_path))
    policy = (
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
        "from script.ci.ci_groups import RESULT\n"
        "def candidate_reuse_profile(base, tested):\n    return RESULT\n"
    )
    archive = policy_archive({"script/ci/ci_scope.py": policy})
    candidate = tmp_path / "script/ci"
    candidate.mkdir(parents=True)
    (candidate / "ci_groups.py").write_text("RESULT = 'ci'\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    original = subprocess.check_output

    def transport(command, **options):
        """Supply an incomplete immutable snapshot and execute the real child interpreter."""
        return archive if command[0] == "git" else original(command, **options)

    monkeypatch.setattr(subprocess, "check_output", transport)
    assert base_test_only_profile(BASE, HEAD, selector="candidate_reuse_profile") is None
