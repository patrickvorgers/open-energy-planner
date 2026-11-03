"""Prove Draft transitions and resolved-discussion admission with independent API evidence."""

from copy import deepcopy
from pathlib import Path

import pytest

from script.ci.check_commit_signatures import GitHub, VerificationError
from script.ci.pr_readiness import (
    require_resolved_threads,
    reset_changed_pr,
)
from tests.ci.test_ci_candidate import Evidence, check


class ReadinessEvidence:
    """Serve one ready PR and record the actual GraphQL operation, without GitHub writes."""

    def __init__(self):
        """Use fixed independent event identities and an initially ready PR."""
        self.event = {
            "number": 12,
            "action": "synchronize",
            "repository": {"full_name": "owner/planner"},
            "pull_request": {"head": {"sha": "2" * 40}, "base": {"sha": "1" * 40}},
        }
        self.current = {
            "state": "open",
            "draft": False,
            "node_id": "PR_fixture",
            "head": {"sha": "2" * 40},
            "base": {"sha": "1" * 40},
        }
        self.writes = []
        self.thread_pages = [{"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}]
        self.invalid_mutation = False

    def get(self, path):
        """Permit only the expected PR metadata read."""
        if "/actions/workflows/checks.yaml/runs?" in path:
            return {"workflow_runs": [], "total_count": 0}
        assert path == "repos/owner/planner/pulls/12"
        return deepcopy(self.current)

    def post(self, path, payload):
        """Apply the documented Draft operation or return a requested review-thread page."""
        assert path == "graphql"
        if "convertPullRequestToDraft" in payload["query"]:
            self.writes.append(payload)
            assert payload["variables"] == {"id": "PR_fixture"}
            if self.invalid_mutation:
                return {"errors": [{"message": "unavailable"}]}
            self.current["draft"] = True
            return {
                "data": {
                    "convertPullRequestToDraft": {
                        "pullRequest": {
                            "id": "PR_fixture",
                            "isDraft": True,
                            "headRefOid": "2" * 40,
                        }
                    }
                }
            }
        return {
            "data": {"repository": {"pullRequest": {"reviewThreads": self.thread_pages.pop(0)}}}
        }


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "edited"])
def test_new_or_changed_ready_pr_returns_to_draft(action):
    """A new revision or retargeting requires deliberate Ready after its review corrections."""
    client = ReadinessEvidence()
    client.event["action"] = action
    if action == "edited":
        client.event["changes"] = {"base": {"ref": {"from": "previous-base"}}}
    assert reset_changed_pr(client.event, "owner/planner", client) is True
    assert client.current["draft"] is True
    assert len(client.writes) == 1
    assert reset_changed_pr(client.event, "owner/planner", client) is False
    assert len(client.writes) == 1


@pytest.mark.parametrize(
    "fault", ["stale-head", "stale-base", "closed", "draft", "title", "ready", "label"]
)
def test_stale_and_unrelated_events_never_change_pr_readiness(fault):
    """An old event or metadata edit cannot draft a newer candidate or defeat explicit Ready."""
    client = ReadinessEvidence()
    if fault.startswith("stale-"):
        client.current[fault.removeprefix("stale-")]["sha"] = "3" * 40
    elif fault == "closed":
        client.current["state"] = "closed"
    elif fault == "draft":
        client.current["draft"] = True
    else:
        client.event["action"] = {
            "title": "edited",
            "ready": "ready_for_review",
            "label": "labeled",
        }[fault]
    assert reset_changed_pr(client.event, "owner/planner", client) is False
    assert client.writes == []


def test_unconfirmed_draft_update_is_not_reported_as_success():
    """A GraphQL failure cannot silently claim that the PR is protected by Draft state."""
    client = ReadinessEvidence()
    client.invalid_mutation = True
    with pytest.raises(VerificationError):
        reset_changed_pr(client.event, "owner/planner", client)


@pytest.mark.parametrize(
    "metadata", [{}, {"state": "open"}, {"state": "unexpected", "head": {}, "base": {}}]
)
def test_missing_readiness_metadata_is_an_error_instead_of_a_successful_noop(metadata):
    """An incomplete API response cannot silently leave a changed PR ready for candidate execution."""
    client = ReadinessEvidence()
    client.current = metadata
    with pytest.raises(VerificationError):
        reset_changed_pr(client.event, "owner/planner", client)
    assert client.writes == []


@pytest.mark.parametrize("resolved", [True, False, None, "true"])
def test_review_discussions_must_be_explicitly_resolved(resolved):
    """Open, outdated or malformed discussions block candidate admission, while resolved history is allowed."""
    client = ReadinessEvidence()
    client.thread_pages[0]["nodes"] = [{"id": "thread-one", "isResolved": resolved}]
    if resolved is True:
        require_resolved_threads("owner/planner", 12, client)
    else:
        with pytest.raises(VerificationError):
            require_resolved_threads("owner/planner", 12, client)


def test_open_thread_on_later_page_still_blocks_admission():
    """Resolved first-page findings cannot hide an unresolved discussion on the next page."""
    client = ReadinessEvidence()
    client.thread_pages = [
        {
            "nodes": [{"id": "resolved", "isResolved": True}],
            "pageInfo": {"hasNextPage": True, "endCursor": "next"},
        },
        {
            "nodes": [{"id": "open", "isResolved": False}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    ]
    with pytest.raises(VerificationError):
        require_resolved_threads("owner/planner", 12, client)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "cursor", "null-page", "wrong-next"])
def test_incomplete_thread_metadata_fails_closed(fault):
    """Missing records, duplicate identities and malformed cursors never prove a completed review."""
    client = ReadinessEvidence()
    if fault == "missing":
        client.thread_pages = [{}]
    elif fault == "duplicate":
        client.thread_pages[0]["nodes"] = [{"id": "same", "isResolved": True}] * 2
    elif fault == "null-page":
        client.thread_pages[0]["pageInfo"] = None
    else:
        client.thread_pages[0]["pageInfo"] = {
            "hasNextPage": True if fault == "cursor" else "false",
            "endCursor": None,
        }
    with pytest.raises(VerificationError):
        require_resolved_threads("owner/planner", 12, client)


@pytest.mark.parametrize("action", ["ready_for_review", "labeled"])
def test_ready_and_manual_retry_use_the_same_current_head_gate(action):
    """A ready transition and explicit retry both require complete current-head checks and no open threads."""
    event = {
        "number": 12,
        "action": action,
        "label": {"name": "ci: run"},
        "repository": {"full_name": "owner/planner"},
        "pull_request": {"head": {"sha": "2" * 40}, "base": {"sha": "1" * 40}},
    }
    current = {
        "state": "open",
        "draft": False,
        "head": {"sha": "2" * 40},
        "base": {"sha": "1" * 40},
        "changed_files": 3,
    }
    checks = [
        {
            "id": identifier,
            "name": name,
            "head_sha": "2" * 40,
            "app": {"id": app},
            "status": "completed",
            "conclusion": "success",
        }
        for identifier, name, app in [
            (1, "Quick checks", 15368),
            (2, "GitHub Verified PR commits", 15368),
            (3, "CodeRabbit", 347564),
            (4, "CodeQL", 57789),
        ]
    ]
    checks[2]["output"] = {
        "title": "Finished",
        "summary": (
            "Result: Success - Review completed\n\n"
            "| Prepare review scope | Completed | 1s |\n"
            "| Prepare repository workspace | Completed | 1s |\n"
            "| Run analysis checks | Completed | 1s |\n"
            "| Generate review feedback | Completed | 1s |\n"
            "| Publish review results | Completed | 1s |\n"
        ),
    }
    checks.append(
        {**checks[2], "id": 5, "conclusion": "skipped", "output": {"title": "Review skipped"}}
    )
    client = Evidence(current, checks)
    assert check(event, "owner/planner", client) == "2" * 40
    checks[2]["status"] = "in_progress"
    with pytest.raises(VerificationError):
        check(event, "owner/planner", client)
    checks[2]["status"] = "completed"
    blocked = ReadinessEvidence()
    blocked.thread_pages[0]["nodes"] = [{"id": "unresolved", "isResolved": False}]
    client.post = blocked.post
    with pytest.raises(VerificationError):
        check(event, "owner/planner", client)


def test_privileged_readiness_workflow_uses_only_immutable_base_code():
    """The writer job cannot execute a PR checkout, access secrets or enter personal hardware."""
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/pr-readiness.yaml").read_text()
    assert "pull_request_target:" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "run: python3 -m script.ci.pr_readiness" in workflow
    assert "head.sha" not in workflow
    assert "secrets." not in workflow
    assert "actions: write" not in workflow


def test_native_concurrency_cancels_candidates_without_cancelling_quick():
    """Trusted source updates and manual Draft transitions invalidate candidates, while cheap checks keep running."""
    root = Path(__file__).resolve().parents[2]
    readiness = (root / ".github/workflows/pr-readiness.yaml").read_text()
    candidate = (root / ".github/workflows/checks.yaml").read_text()
    quick = (root / ".github/workflows/quick-checks.yaml").read_text()
    assert "format('pr-ci-{0}', github.event.pull_request.number)" in readiness
    assert "format('pr-ci-{0}', github.event.pull_request.number || github.ref)" in candidate
    assert "format('pr-quick-{0}', github.event.pull_request.number)" in quick
    for workflow in (readiness, candidate, quick):
        assert "cancel-in-progress: true" in workflow
    assert "readiness-metadata-{0}" in readiness
    assert "github.event.changes.base.ref.from == null" in readiness
    assert "types: [ready_for_review, converted_to_draft, labeled]" in candidate
    assert "types: [opened, synchronize, reopened, edited]" in quick


def test_post_transport_keeps_credentials_in_headers_and_accepts_graphql_json():
    """GraphQL support uses the existing bounded no-redirect client and never encodes credentials in the URL."""

    class Response:
        """Return a small independently specified GraphQL body."""

        status = 200

        def __enter__(self):
            """Enter the fake HTTP response context."""
            return self

        def __exit__(self, *args):
            """Close the fake response without suppressing failures."""

        def read(self, limit):
            """Respect the bounded response contract."""
            assert limit == 2_000_001
            return b'{"data": {"value": true}}'

    class Transport:
        """Inspect the outgoing request without performing network I/O."""

        def open(self, request, timeout):
            """Verify that only JSON payload and standard authentication are sent."""
            assert request.full_url == "https://api.github.com/graphql"
            assert request.get_method() == "POST"
            assert request.data == b'{"query": "query { viewer { login } }"}'
            assert timeout == 20
            return Response()

    client = GitHub("fixture-only")
    client._opener = Transport()
    assert client.post("graphql", {"query": "query { viewer { login } }"}) == {
        "data": {"value": True}
    }
