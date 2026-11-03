"""Independent signature-gate cases. Fake SHAs never create unsigned real commits."""

import json
from copy import deepcopy
from urllib.error import URLError

import pytest

from script.ci.check_commit_signatures import (
    GitHub,
    NoRedirects,
    VerificationError,
    check,
    introduced_commits,
    main,
)

BASE, FIRST, MERGE, HEAD = (character * 40 for character in "1234")
REPO = "patrickvorgers/open-energy-planner"
EVENT = {
    "number": 5,
    "repository": {"full_name": REPO},
    "pull_request": {"head": {"sha": HEAD}, "base": {"sha": BASE}, "commits": 3},
}


class GitHubFixture:
    """Serve independent commit metadata while allowing explicit verification and PR-identity faults."""

    def __init__(self):
        """Initialize a complete signed history including a merged commit and mutable PR identity controls."""
        self.requests = []
        self.change = False
        self.fail = None
        self.records = {
            value: {"sha": value, "verification": {"verified": True, "reason": "valid"}}
            for value in (FIRST, MERGE, HEAD)
        }

    def get(self, path):
        """Return fixture GitHub metadata or the explicitly injected transport/identity failure."""
        self.requests.append(path)
        if self.fail:
            raise self.fail
        if "/pulls/" in path:
            result = deepcopy(EVENT["pull_request"])
            if self.change and len(self.requests) > 1:
                result["head"]["sha"] = "5" * 40
            return result
        return self.records[path.rsplit("/", 1)[-1]]


def all_commits(base, head):
    """Return the complete independent base/head history fixture, including the merged commit."""
    assert (base, head) == (BASE, HEAD)
    return [FIRST, MERGE, HEAD]


def test_checks_every_introduced_commit_including_merge_and_bot_commits():
    """Verify a mixed-author history. Require metadata checks for every introduced commit, including merges."""
    client = GitHubFixture()
    assert check(EVENT, REPO, client, all_commits) == 3
    assert client.requests == [
        f"repos/{REPO}/pulls/5",
        *(f"repos/{REPO}/git/commits/{value}" for value in (FIRST, MERGE, HEAD)),
        f"repos/{REPO}/pulls/5",
    ]


@pytest.mark.parametrize("commit", [FIRST, MERGE, HEAD])
@pytest.mark.parametrize("reason", ["unsigned", "unknown_key", "invalid", "gpgverify_unavailable"])
def test_any_unverified_introduced_commit_fails_the_gate(commit, reason):
    """Invalidate an earlier, merged or head commit. Require the all-commits gate to fail in every case."""
    client = GitHubFixture()
    client.records[commit]["verification"] = {"verified": False, "reason": reason}
    with pytest.raises(VerificationError, match=commit):
        check(EVENT, REPO, client, all_commits)


@pytest.mark.parametrize(
    "verification",
    [{}, {"verified": "true", "reason": "valid"}, {"verified": True, "reason": "unsigned"}],
)
def test_missing_or_inconsistent_verification_is_not_success(verification):
    """Remove or contradict signature evidence. Reject it rather than treating partial metadata as verification."""
    client = GitHubFixture()
    client.records[FIRST]["verification"] = verification
    with pytest.raises(VerificationError):
        check(EVENT, REPO, client, all_commits)


def test_api_failure_is_not_treated_as_success():
    """Make GitHub verification unavailable. Require failure instead of a green signature result."""
    client = GitHubFixture()
    client.fail = VerificationError("unavailable")
    with pytest.raises(VerificationError):
        check(EVENT, REPO, client, all_commits)


def test_changed_head_invalidates_finished_verification():
    """Change PR identity during verification. Reject a result that no longer describes the current head."""
    client = GitHubFixture()
    client.change = True
    with pytest.raises(VerificationError, match="changed"):
        check(EVENT, REPO, client, all_commits)


def test_stale_event_is_rejected_before_checking_commits():
    """Present an event for an outdated PR head. Reject it before trusting commit evidence."""
    event = deepcopy(EVENT)
    event["pull_request"]["base"]["sha"] = "6" * 40
    client = GitHubFixture()
    with pytest.raises(VerificationError, match="changed"):
        check(event, REPO, client, all_commits)
    assert len(client.requests) == 1


@pytest.mark.parametrize("commits", [[], [FIRST], [HEAD], [FIRST, HEAD, HEAD]])
def test_empty_or_incomplete_history_fails(commits):
    """Omit required commits from enumeration. Reject empty/incomplete history instead of approving only its head."""
    with pytest.raises(VerificationError):
        check(EVENT, REPO, GitHubFixture(), lambda *_: commits)


def test_git_range_uses_event_head_not_synthetic_workflow_merge(monkeypatch):
    """Enumerate event base/head. Require exclusion of GitHub's synthetic test-merge commit."""

    def enumerate(command, **kwargs):
        if command == ["git", "rev-parse", "--is-shallow-repository"]:
            return "false\n"
        assert command == ["git", "rev-list", "--topo-order", "--reverse", f"{BASE}..{HEAD}"]
        return f"{FIRST}\n{MERGE}\n{HEAD}\n"

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", enumerate)
    assert introduced_commits(BASE, HEAD) == [FIRST, MERGE, HEAD]


def test_shallow_history_is_rejected_even_if_base_and_head_exist(monkeypatch):
    """Provide a shallow checkout with apparent endpoints. Reject potentially incomplete introduced history."""
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output",
        lambda *args, **kwargs: "true\n",
    )
    with pytest.raises(VerificationError, match="non-shallow"):
        introduced_commits(BASE, HEAD)


def test_wrong_commit_identity_fails():
    """Return metadata for another SHA. Reject mismatched evidence even when its signature is valid."""
    client = GitHubFixture()
    client.records[FIRST]["sha"] = HEAD
    with pytest.raises(VerificationError):
        check(EVENT, REPO, client, all_commits)


def test_http_redirects_are_not_followed():
    """Offer an HTTP redirect. Refuse it so authentication cannot be forwarded to another endpoint."""
    assert NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


def test_api_transport_failure_hides_provider_exception_and_token():
    """Inject a credential-bearing transport error. Emit a safe failure without exposing the secret."""
    client = GitHub("fixture-secret")

    class BrokenTransport:
        def open(self, *args, **kwargs):
            raise URLError("fixture-secret must not appear in diagnostics")

    client._opener = BrokenTransport()
    with pytest.raises(VerificationError) as error:
        client.get(f"repos/{REPO}/git/commits/{HEAD}")
    assert "fixture-secret" not in str(error.value)


def test_missing_workflow_credentials_fails_closed(monkeypatch):
    """Omit trusted workflow credentials/event input. Require a failing exit code."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert main() == 1


def test_untrusted_pull_request_execution_is_rejected(monkeypatch):
    """Run under a PR-controlled event. Refuse execution rather than trusting potentially modified verifier code."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    assert main() == 1


def test_history_fetch_never_checks_out_pr_code_or_persists_token(monkeypatch):
    """Fetch PR objects. Require no checkout/execution and credentials confined to the child environment."""
    calls = []

    def fetch(command, **kwargs):
        calls.append(command)
        assert command == [
            "git",
            "fetch",
            "--no-tags",
            f"https://github.com/{REPO}.git",
            "+refs/pull/5/head:refs/remotes/origin/signature-pr-head",
        ]
        assert "fixture-secret" not in repr(command)
        assert kwargs["check"] is True
        assert kwargs["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        assert kwargs["env"]["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")

    monkeypatch.setattr("script.ci.proof_git.subprocess.run", fetch)
    GitHub("fixture-secret").fetch_pr_history(REPO, 5)
    assert len(calls) == 1


@pytest.mark.parametrize("fails", [False, True])
def test_trusted_main_returns_the_actual_outcome_without_status_write_access(
    tmp_path, monkeypatch, fails
):
    """Run trusted orchestration with success/failure evidence. Return the matching exit code without duplicate status publication."""
    path = tmp_path / "event.json"
    path.write_text(json.dumps(EVENT))
    for key, value in {
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_TOKEN": "fixture-only",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    class Client:
        def __init__(self, token):
            assert token == "fixture-only"

        def fetch_pr_history(self, repo, number):
            calls.append("fetch")

    def verify(*args):
        calls.append("verify")
        if fails:
            raise VerificationError("unverified")
        return 3

    monkeypatch.setattr("script.ci.check_commit_signatures.GitHub", Client)
    monkeypatch.setattr("script.ci.check_commit_signatures.check", verify)
    assert main() == (1 if fails else 0)
    assert calls == ["fetch", "verify"]


@pytest.mark.parametrize("metadata", [None, [], {}, "true"])
def test_malformed_verification_metadata_fails(metadata):
    """Replace signature metadata with malformed values. Reject it without accepting truthy lookalikes."""
    client = GitHubFixture()
    client.records[FIRST]["verification"] = metadata
    with pytest.raises(VerificationError):
        check(EVENT, REPO, client, all_commits)


@pytest.mark.parametrize(
    "status,data",
    [
        (401, b"{}"),
        (500, b"{}"),
        (200, b""),
        (200, b"broken"),
        (200, b"[]"),
        (200, b"x" * 2_000_001),
    ],
)
def test_api_status_json_and_size_failures(status, data):
    """Return HTTP errors, malformed JSON or oversized data. Require bounded fail-closed verification."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == 2_000_001
            return data

    response = Response()
    response.status = status

    class Transport:
        def open(self, request, timeout):
            assert request.full_url.startswith("https://api.github.com/repos/")
            assert timeout == 20
            return response

    client = GitHub("fixture-only")
    client._opener = Transport()
    with pytest.raises(VerificationError):
        client.get(f"repos/{REPO}/git/commits/{HEAD}")


@pytest.mark.parametrize(
    "repository,number", [("../elsewhere", 5), ("owner/repo", True), ("owner/repo", 0)]
)
def test_history_process_owner_rejects_unvalidated_identity(monkeypatch, repository, number):
    """Even a future metadata caller cannot pass a path or invalid PR number to Git fetch."""

    def unexpected(*args, **kwargs):
        """A rejected identity must fail before starting any process."""
        pytest.fail("Git must not run for invalid identity")

    monkeypatch.setattr("script.ci.proof_git.subprocess.run", unexpected)
    with pytest.raises(VerificationError):
        GitHub("fixture-secret").fetch_pr_history(repository, number)
