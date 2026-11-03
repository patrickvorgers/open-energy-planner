"""Bound fixed Git capabilities and preserve their existing caller error contracts."""

import subprocess

import pytest

from script.ci import proof_git


def test_history_fetch_timeout_becomes_credential_free_verification_failure(monkeypatch):
    """A stalled remote fetch gets a longer bounded deadline and fails signature verification cleanly."""

    def timeout(command, **options):
        """Assert the independent network deadline before modelling expiry."""
        assert options["timeout"] == 120
        raise subprocess.TimeoutExpired(command, 120)

    monkeypatch.setattr(proof_git.subprocess, "run", timeout)
    with pytest.raises(proof_git.VerificationError, match="Cannot fetch PR history") as error:
        proof_git.fetch_pr_history("owner/repo", 1, "private-fixture-token")
    assert "private-fixture-token" not in str(error.value)


@pytest.mark.parametrize("stage", ["rev-parse", "rev-list"])
def test_commit_enumeration_timeout_remains_a_verification_failure(monkeypatch, stage):
    """Either bounded history read timing out must retain the signature caller's existing failure type."""

    def read(command, **options):
        """Only a successful shallow check may precede the selected failing history read."""
        assert options["timeout"] == 60
        if command[1] == stage:
            raise subprocess.TimeoutExpired(command, 60)
        return "false\n"

    monkeypatch.setattr(proof_git.subprocess, "check_output", read)
    with pytest.raises(proof_git.VerificationError, match="Cannot enumerate PR history"):
        proof_git.introduced_commits("1" * 40, "2" * 40)


@pytest.mark.parametrize("operation", ["checkout", "merge", "provenance"])
def test_standalone_metadata_operations_keep_process_failure_contract(
    monkeypatch, tmp_path, operation
):
    """Standalone completion, label and provenance callers retain bounded process errors."""

    def timeout(command, **options):
        """The local deadline expires before any result can authorize completion or labels."""
        assert options["timeout"] == 60
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(proof_git.subprocess, "check_output", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        if operation == "checkout":
            proof_git.checkout_tree()
        elif operation == "merge":
            proof_git.merged_tree("1" * 40, "2" * 40, tmp_path)
        else:
            proof_git.tree_records(tmp_path, "1" * 40)


def test_clean_checkout_check_bounds_every_local_process(monkeypatch):
    """A successful checkout proof bounds both reads and the clean-tree diff without changing identity."""
    calls = []

    def read(command, **options):
        """Return independent checkout identities while recording each metadata deadline."""
        assert options["timeout"] == 60
        calls.append(command)
        return "1" * 40 if command[-1] == "HEAD" else "2" * 40

    def clean(command, **options):
        """Require the dirty-check process to have the same bounded local deadline."""
        assert options["timeout"] == 60
        assert options["check"] is True
        calls.append(command)

    monkeypatch.setattr(proof_git.subprocess, "check_output", read)
    monkeypatch.setattr(proof_git.subprocess, "run", clean)
    assert proof_git.checkout_tree("1" * 40) == "2" * 40
    assert calls == [
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "--exit-code", "HEAD", "--"],
        ["git", "rev-parse", "HEAD^{tree}"],
    ]


@pytest.mark.parametrize("revision", ["--help", "HEAD", "a" * 39, "A" * 40, None])
def test_immutable_source_reader_rejects_non_sha_before_starting_git(
    monkeypatch, tmp_path, revision
):
    """Every metadata caller must supply an immutable identity rather than a ref or Git option."""

    def unexpected(*arguments, **options):
        """Fail if an invalid revision reaches any process boundary."""
        pytest.fail("Invalid revision reached Git")

    monkeypatch.setattr(proof_git.subprocess, "run", unexpected)
    with pytest.raises(proof_git.VerificationError, match="immutable 40-character commit SHA"):
        proof_git.immutable_test_sources(revision, tmp_path)
