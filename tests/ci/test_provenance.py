"""Protect the complete public baseline independently of later working file edits."""

import json

import pytest

from script.ci.check_provenance import verify


@pytest.fixture
def historical_repository(tmp_path, monkeypatch):
    """Provide independently declared public baseline tree and blob identities."""
    snapshot = "1" * 40
    receipt = {
        "snapshot_revision": snapshot,
        "records": [
            {
                "path": "README.md",
                "git_blob": "3" * 40,
            }
        ],
    }
    (tmp_path / "design").mkdir()
    path = tmp_path / "design/PUBLIC-SNAPSHOT.json"
    path.write_text(json.dumps(receipt))

    def read_tree(command, **kwargs):
        assert kwargs["cwd"] == tmp_path
        assert command[-1] == snapshot
        return f"100644 blob {'3' * 40}\tREADME.md\n"

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", read_tree)
    return tmp_path, path, receipt


def test_later_working_document_edits_do_not_rewrite_historical_provenance(historical_repository):
    """Edit a current document. Retain provenance verification because it refers to fixed historical blobs."""
    root, _, _ = historical_repository
    (root / "README.md").write_text("A later, intentionally different document.")
    assert verify(root) == 1


def test_rejects_a_record_not_matching_its_fixed_revision(historical_repository):
    """Reject a forged blob identity against the independent snapshot tree."""
    root, path, receipt = historical_repository
    receipt["records"][0]["git_blob"] = "5" * 40
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="provenance"):
        verify(root)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra"])
def test_rejects_incomplete_or_ambiguous_snapshot_records(historical_repository, mutation):
    """Require exact baseline coverage rather than accepting a valid subset."""
    root, path, receipt = historical_repository
    if mutation == "missing":
        receipt["records"] = []
    elif mutation == "duplicate":
        receipt["records"].append(dict(receipt["records"][0]))
    else:
        receipt["records"].append({"path": "extra.md", "git_blob": "6" * 40})
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="provenance"):
        verify(root)
