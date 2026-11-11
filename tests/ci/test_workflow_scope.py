"""Classify immutable workflow changes by their actual impact without narrowing mixed PRs."""

import re
from pathlib import Path, PurePosixPath

import pytest

from script.ci.ci_scope import performance_queries_path, select_profile, selected_suites

WORKFLOW = """name: Checks
on: workflow_dispatch
permissions:
  contents: read
jobs:
  quality:
    runs-on: ubuntu-24.04
    steps:
      - name: Prepare Python
        uses: actions/setup-python@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa # v6
        with:
          python-version: '3.14'
      - run: python script/ci/ci.py
"""
UPDATED_WORKFLOW = WORKFLOW.replace("a" * 40 + " # v6", "b" * 40 + " # v7.1.0")


@pytest.fixture
def workflow_trees(tmp_path, monkeypatch):
    """Supply independent immutable tree/blob records without adding Git to native test images."""
    snapshots, blobs, staged = {}, {}, {}

    def tree(workflow, extra=None):
        """Stage an independently specified workflow and optional second owner, then record its tree."""
        path = tmp_path / ".github/workflows/checks.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        files = {}
        if workflow is None:
            staged.pop(".github/workflows/checks.yaml", None)
        else:
            path.write_text(workflow, encoding="utf-8")
            files[".github/workflows/checks.yaml"] = workflow.encode()
        if extra:
            files[extra] = b"value = 1\n"
        for name, contents in files.items():
            blob = f"{len(blobs) + 100:040x}"
            blobs[blob] = contents
            staged[name] = blob
        revision = f"{len(snapshots) + 1:040x}"
        snapshots[revision] = staged.copy()
        return revision

    def changes(base, tested, root):
        """Encode the complete independent diff, including additions and removals."""
        assert root == tmp_path
        before, after = snapshots[base], snapshots[tested]
        records = []
        for name in sorted(before.keys() | after.keys()):
            if before.get(name) == after.get(name):
                continue
            status = "M" if name in before and name in after else "A" if name in after else "D"
            old_mode = "100644" if name in before else "000000"
            new_mode = "100644" if name in after else "000000"
            records.append(
                f":{old_mode} {new_mode} {before.get(name, '0' * 40)} {after.get(name, '0' * 40)} {status}\0{name}\0".encode()
            )
        return b"".join(records)

    monkeypatch.setattr("script.ci.ci_scope.raw_changes", changes)
    monkeypatch.setattr("script.ci.ci_scope.immutable_blob", lambda blob, root: blobs[blob])

    return tmp_path, tree


def test_setup_python_pin_only_uses_ci_and_immutable_blobs(workflow_trees):
    """An action pin update selects CI even when the working copy contains unrelated executable changes."""
    root, tree = workflow_trees
    base = tree(WORKFLOW)
    tested = tree(UPDATED_WORKFLOW)
    (root / ".github/workflows/checks.yaml").write_text("untrusted working copy", encoding="utf-8")
    reasons = []
    assert select_profile(base, tested, root, reasons=reasons) == "ci"
    assert reasons == [("ci", ".github/workflows/checks.yaml")]


@pytest.mark.parametrize(
    "workflow",
    [
        "jobs:\n  quality:\n    steps:\n      - run: |\n          uses: actions/setup-python@"
        + "a" * 40
        + "\n",
        "jobs:\n  quality:\n    steps:\n      - run: >-\n          uses: actions/setup-python@"
        + "a" * 40
        + "\n",
        "jobs:\n  quality:\n    steps:\n      - name: '\n        uses: actions/setup-python@"
        + "a" * 40
        + "\n        '\n",
        "jobs:\n  quality:\n    env:\n      uses: actions/setup-python@" + "a" * 40 + "\n",
        "description: |\n  jobs:\n    quality:\n      steps:\n        - name: Example\n          uses: actions/setup-python@"
        + "a" * 40
        + "\n",
    ],
)
def test_scalar_and_non_step_references_keep_full(workflow_trees, workflow):
    """Matching action text in executable scalars or unrelated mappings cannot narrow functional CI."""
    root, tree = workflow_trees
    base = tree(workflow)
    tested = tree(workflow.replace("a" * 40, "b" * 40))
    assert select_profile(base, tested, root) == "full"


def test_real_parallel_workflow_setup_updates_keep_conservative_route(workflow_trees):
    """Changing multiple machine setup steps exceeds the existing one-reference-only exemption."""
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/checks.yaml").read_text()
    updated = re.sub(r"(uses: actions/setup-python@)[0-9a-f]{40}", r"\g<1>" + "b" * 40, workflow)
    root, tree = workflow_trees
    base = tree(workflow)
    tested = tree(updated)
    assert select_profile(base, tested, root) == "full"


@pytest.mark.parametrize(
    "before,after",
    [
        ("python-version: '3.14'", "python-version: '3.15'"),
        ("contents: read", "contents: write"),
        ("ubuntu-24.04", "self-hosted"),
        ("run: python script/ci/ci.py", "run: python different.py"),
        ("actions/setup-python@" + "b" * 40, "actions/setup-python@v7"),
        ("actions/setup-python@", "different/setup-python@"),
        ("      - run:", "      - uses: actions/setup-python@" + "c" * 40 + "\n      - run:"),
    ],
)
def test_setup_pin_cannot_hide_execution_or_privilege_changes(workflow_trees, before, after):
    """Runtime, workload, privilege, unpinned and ambiguous action edits retain full functional CI."""
    root, tree = workflow_trees
    base = tree(WORKFLOW)
    tested = tree(UPDATED_WORKFLOW.replace(before, after))
    assert select_profile(base, tested, root) == "full"


@pytest.mark.parametrize("added", [False, True])
def test_new_or_removed_workflow_stays_full(workflow_trees, added):
    """A newly added or removed workflow cannot use the reference-only modification exception."""
    root, tree = workflow_trees
    base = tree(None if added else WORKFLOW)
    tested = tree(UPDATED_WORKFLOW if added else None)
    assert select_profile(base, tested, root) == "full"


@pytest.mark.parametrize(
    "extra",
    [
        "script/ci/future.py",
        "requirements.lock",
        "src/open_energy_planner/storage/adapters/queries/read.py",
    ],
)
def test_reference_update_preserves_every_other_owner(workflow_trees, extra):
    """A cheap workflow edit cannot hide shared execution, runtime dependencies or database query impact."""
    root, tree = workflow_trees
    base = tree(WORKFLOW)
    tested = tree(UPDATED_WORKFLOW, extra)
    suites = selected_suites(select_profile(base, tested, root))
    if extra == "script/ci/future.py":
        assert suites == {"native", "browser", "ha", "databases", "supervisor"}
    else:
        assert "performance-queries" in suites
        assert "databases" in suites or "database-contracts" in suites


@pytest.mark.parametrize("failure", [OSError, UnicodeError])
def test_unreadable_workflow_evidence_retains_full(workflow_trees, monkeypatch, failure):
    """Unavailable immutable blob evidence cannot authorize a cheaper workflow selection."""
    root, tree = workflow_trees
    base = tree(WORKFLOW)
    tested = tree(UPDATED_WORKFLOW)

    def unavailable(*args):
        """Model a bounded Git read or decoding failure before any metadata exemption is granted."""
        raise failure("Unavailable blob")

    monkeypatch.setattr("script.ci.ci_scope.immutable_blob", unavailable)
    assert select_profile(base, tested, root) == "full"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/checks.yaml",
        "script/ci/ci.py",
        "script/ci/check_images.py",
        "script/ci/pytest_suites.py",
        "script/ci/future.py",
    ],
)
def test_ci_control_paths_do_not_imply_query_measurements(path):
    """CI controller and workflow ownership alone never requests database volume measurements."""
    assert not performance_queries_path(PurePosixPath(path))
