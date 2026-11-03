"""Bind focused product reuse to immutable whole-change selection and ordered publication."""

import subprocess
from pathlib import Path

import pytest

from script.ci.check_main_ci_reuse import base_test_only_profile
from script.ci.ci_scope import candidate_reuse_profile


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/open_energy_planner/web/public/future.js", "selected:browser,ci,unit-frontend"),
        ("src/open_energy_planner/new_shared.py", None),
        ("script/ci/ci_scope.py", "ci"),
        ("tests/database/performance/test_future.py", "ci+performance-queries"),
    ],
)
def test_product_reuse_only_accepts_a_known_focused_complete_change(monkeypatch, path, expected):
    """Unknown composition or policy impact cannot receive a focused-product reuse marker."""
    record = f":100644 100644 {'3' * 40} {'4' * 40} M\0{path}\0".encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: record
    )
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {})
    assert candidate_reuse_profile("1" * 40, "2" * 40) == expected


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("profile", ["selected:browser,ci", "ci+performance-queries"])
def test_product_policy_uses_first_parent_code_in_a_real_isolated_process(
    tmp_path, monkeypatch, available, profile, policy_archive
):
    """Candidate-controlled policy is not executed and old bases cannot authorize the new capability."""
    monkeypatch.setattr("script.ci.proof_git.tempfile.tempdir", str(tmp_path))
    original_output = subprocess.check_output
    policy = (
        (
            "import sys\n"
            "def candidate_reuse_profile(base, tested):\n"
            "    assert sys.flags.isolated == 1 and sys.flags.no_site == 1\n"
            f"    assert (base, tested) == ('{'1' * 40}', '{'2' * 40}')\n"
            f"    return {profile!r}\n"
        )
        if available
        else "# Previous policy has no focused-product capability.\n"
    )
    (tmp_path / "ci_scope.py").write_text(
        "raise RuntimeError('untrusted candidate policy')\n", encoding="utf-8"
    )

    def immutable_policy(command, **options):
        """Replace only Git transport and keep the actual isolated policy interpreter."""
        if command[0] == "git":
            assert command == ["git", "archive", "--format=tar", "1" * 40, "script"]
            return policy_archive({"script/ci/ci_scope.py": policy})
        return original_output(command, **options)

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", immutable_policy)
    monkeypatch.chdir(tmp_path)
    assert base_test_only_profile("1" * 40, "2" * 40, selector="candidate_reuse_profile") == (
        profile if available else None
    )


def test_focused_product_marker_follows_completion_and_original_bundle():
    """The workflow cannot publish focused product identity before required completion and packaging."""
    root = Path(__file__).resolve().parents[2]
    source = (root / ".github/workflows/checks.yaml").read_text(encoding="utf-8")
    marker = source.split("- name: 'Selected CI source tree:", 1)[1].split("- name:", 1)[0]
    assert "steps.scope.outputs.test_only != 'true'" in marker
    assert "steps.scope.outputs.documentation_only != 'true'" in marker
    assert "startsWith(steps.scope.outputs.profile, 'selected:')" in marker
    assert "git diff --exit-code HEAD --" in marker
    assert source.index("script.ci.check_suite_completion") < source.index(
        "Selected CI source tree:"
    )
    assert source.index("Preserve matched installation artifacts") < source.index(
        "Selected CI source tree:"
    )


def test_policy_owner_rejects_unreviewed_entrypoint_before_process_launch(monkeypatch):
    """The isolated policy adapter cannot be turned into an arbitrary Python entrypoint."""

    def unexpected(*args, **kwargs):
        """No Git read or Python execution is permitted for an unreviewed selector."""
        pytest.fail("Unreviewed policy entrypoint executed")

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", unexpected)
    assert base_test_only_profile("1" * 40, "2" * 40, selector="main") is None
