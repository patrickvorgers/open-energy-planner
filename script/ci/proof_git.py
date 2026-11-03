"""Own reviewed Git metadata processes and immutable base-policy execution.

This module retains conservative CI coverage. Proof owners may consume these
fixed operations, but cannot launch processes or load executable policy directly.
"""

import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from base64 import b64encode
from pathlib import Path

POLICY_TIMEOUT_SECONDS = 30
GIT_TIMEOUT_SECONDS = 60
FETCH_TIMEOUT_SECONDS = 120


class VerificationError(ValueError):
    """A failed or unavailable verification must never produce a passing check."""


def sha(value):
    """Require an immutable lowercase 40-character Git commit identity."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise VerificationError("Expected an immutable 40-character commit SHA")
    return value


def fetch_pr_history(repository, number, token):
    """Fetch objects without checking out or executing any PR-controlled files."""
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise VerificationError("Invalid repository identity")
    if type(number) is not int or number <= 0:
        raise VerificationError("Invalid PR number")
    environment = dict(os.environ)
    header = b64encode(("x-access-token:" + token).encode()).decode()
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + header,
        }
    )
    try:
        subprocess.run(
            [
                "git",
                "fetch",
                "--no-tags",
                f"https://github.com/{repository}.git",
                f"+refs/pull/{number}/head:refs/remotes/origin/signature-pr-head",
            ],
            check=True,
            env=environment,
            timeout=FETCH_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise VerificationError("Cannot fetch PR history from GitHub") from None


def introduced_commits(base, head):
    """All reachable new commits, including merged branches. Exclude GitHub's test merge."""
    try:
        shallow = subprocess.check_output(
            ["git", "rev-parse", "--is-shallow-repository"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        ).strip()
        if shallow != "false":
            raise VerificationError("A full, non-shallow checkout is required")
        output = subprocess.check_output(
            ["git", "rev-list", "--topo-order", "--reverse", f"{sha(base)}..{sha(head)}"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise VerificationError(
            "Cannot enumerate PR history. A full checkout is required"
        ) from None
    commits = [sha(line) for line in output.splitlines()]
    if not commits or head not in commits or len(commits) != len(set(commits)):
        raise VerificationError("PR commit history is empty, incomplete or ambiguous")
    return commits


def base_test_only_profile(base_sha, commit_sha, *, selector="test_only_profile"):
    """Run the first parent's self-contained policy against the complete merged diff.

    An older policy without this capability, unreadable Git data or uncertain
    ownership cannot authorize reuse of focused evidence.
    """
    if selector not in {
        "test_only_profile",
        "candidate_reuse_profile",
        "performance_queries_required",
    }:
        return None
    try:
        archive = subprocess.check_output(
            ["git", "archive", "--format=tar", sha(base_sha), "script"],
            stderr=subprocess.DEVNULL,
            timeout=POLICY_TIMEOUT_SECONDS,
        )
        with tempfile.TemporaryDirectory(prefix="oep-ci-policy-") as directory:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as snapshot:
                snapshot.extractall(directory, filter="data")
            path = Path(directory) / "script/ci/ci_scope.py"
            if not path.is_file():
                path = Path(directory) / "script/ci_scope.py"
            profile = subprocess.check_output(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    (
                        "import runpy, sys\n"
                        "policy = runpy.run_path(sys.argv[1])\n"
                        f"print(policy[{selector!r}](sys.argv[2], sys.argv[3]))"
                    ),
                    str(path),
                    base_sha,
                    sha(commit_sha),
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=POLICY_TIMEOUT_SECONDS,
            ).strip()
        if selector == "performance_queries_required":
            return {"True": True, "False": False}.get(profile)
        if re.fullmatch(r"ci(?:\+performance-queries)?|selected:[a-z0-9,-]+", profile):
            return profile
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        pass
    return None


def merged_tree(base, head, root):
    """Produce the intended merged tree from immutable revisions, failing on conflicts."""
    result = subprocess.check_output(
        ["git", "merge-tree", "--write-tree", sha(base), sha(head)],
        cwd=root,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return sha(result.strip())


def checkout_tree(expected_head=None):
    """Read the checkout tree and, when requested, require the expected clean commit."""
    if expected_head is not None:
        actual_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=GIT_TIMEOUT_SECONDS
        ).strip()
        if actual_head != sha(expected_head):
            raise VerificationError("Checkout is not the pushed commit")
        subprocess.run(
            ["git", "diff", "--exit-code", "HEAD", "--"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], text=True, timeout=GIT_TIMEOUT_SECONDS
    ).strip()


def tree_records(root, revision):
    """Read provenance blob records from an immutable tree without executing its contents."""
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={root}", "ls-tree", "-r", sha(revision)],
        cwd=root,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def immutable_test_sources(revision, root):
    """Read immutable blobs without candidate execution or archive attribute filtering."""
    listing = subprocess.run(
        ["git", "ls-tree", "-rz", sha(revision), "--", "tests"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    files = []
    for record in listing.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        if not path.startswith("tests/") or not path.endswith(".py"):
            continue
        mode, kind, blob = metadata.split()
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError("Unsupported test source mode")
        files.append((path, blob))
    if not files:
        raise ValueError("No immutable test sources")
    contents = subprocess.run(
        ["git", "cat-file", "--batch"],
        input=b"".join(blob + b"\n" for _, blob in files),
        cwd=root,
        capture_output=True,
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    stream = io.BytesIO(contents.stdout)
    sources = {}
    for path, requested_blob in files:
        blob, kind, raw_size = stream.readline().split()
        size = int(raw_size)
        if blob != requested_blob or kind != b"blob" or size < 0:
            raise ValueError("Unexpected test source blob")
        source = stream.read(size)
        if len(source) != size or stream.read(1) != b"\n":
            raise ValueError("Truncated test source blob")
        sources[path] = source.decode("utf-8-sig")
    if stream.read():
        raise ValueError("Unexpected trailing test source data")
    return sources


def raw_changes(base, tested, root):
    """Read every changed regular-file record without external diff or text conversion hooks."""
    return subprocess.check_output(
        [
            "git",
            "diff",
            "--raw",
            "--no-abbrev",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "-z",
            sha(base),
            sha(tested),
            "--",
        ],
        cwd=root,
        stderr=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_SECONDS,
    )
