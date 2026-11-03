"""Exercise frozen-head review preparation without spending a provider review or mutating GitHub."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from script.ci import review_preflight
from script.ci.check_ci_candidate import changed_files, require_coderabbit_review
from script.ci.check_commit_signatures import VerificationError

REPOSITORY = "owner/planner"
BASE = "1" * 40
HEAD = "2" * 40
REVIEWED = "3" * 40
ROOT = Path(__file__).resolve().parents[2]


def completed_review():
    """Provide independently declared phase evidence from a substantive CodeRabbit review."""
    return {
        "id": 20,
        "name": "CodeRabbit",
        "head_sha": HEAD,
        "app": {"id": 347564},
        "status": "completed",
        "conclusion": "success",
        "output": {
            "title": "Finished",
            "summary": (
                "Result: Success - Review completed\n\n"
                "| Step | Status | Time |\n"
                "| Prepare review scope | Completed | 1s |\n"
                "| Prepare repository workspace | Completed | 1s |\n"
                "| Run analysis checks | Completed | 1s |\n"
                "| Generate review feedback | Completed | 1s |\n"
                "| Publish review results | Completed | 1s |\n"
            ),
        },
    }


class Evidence:
    """Serve metadata only. A write operation is never available to the preflight."""

    def __init__(self, count=3):
        """Start with a same-repository Draft whose final head includes one correction commit."""
        self.current = {
            "state": "open",
            "draft": True,
            "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}},
            "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPOSITORY}},
            "user": {"login": "developer"},
            "changed_files": count,
            "commits": 2,
        }
        self.files = [
            {"filename": f"file-{index}.png", "status": ("added", "removed", "renamed")[index % 3]}
            for index in range(count)
        ]
        self.commits = [{"sha": REVIEWED}, {"sha": HEAD}]
        self.comments = []
        self.checks = [
            {
                "id": number,
                "name": name,
                "head_sha": HEAD,
                "app": {"id": app},
                "status": "completed",
                "conclusion": "success",
            }
            for number, name, app in (
                (1, "Quick checks", 15368),
                (2, "GitHub Verified PR commits", 15368),
                (3, "CodeQL", 57789),
            )
        ]
        self.pr_reads = 0
        self.changed = None
        self.unresolved = False
        self.comparison = {"merge_base_commit": {"sha": BASE}, "status": "ahead"}
        self.calls = []

    def get(self, path):
        """Read current PR identity or complete check pages, with optional concurrent mutation."""
        self.calls.append(("GET", path))
        if path == f"repos/{REPOSITORY}/pulls/12":
            self.pr_reads += 1
            return deepcopy(self.changed if self.pr_reads > 1 and self.changed else self.current)
        if path == f"repos/{REPOSITORY}/compare/{BASE}...{HEAD}?per_page=1":
            return deepcopy(self.comparison)
        assert path.startswith(f"repos/{REPOSITORY}/commits/{HEAD}/check-runs?")
        page = int(path.rsplit("=", 1)[1])
        return {
            "total_count": len(self.checks),
            "check_runs": self.checks[(page - 1) * 100 : page * 100],
        }

    def get_list(self, path):
        """Serve actual complete file, commit and comment pages rather than trusting a count alone."""
        self.calls.append(("GET", path))
        page = int(path.rsplit("=", 1)[1])
        if "/files?" in path:
            rows = self.files
        elif "/commits?" in path:
            rows = self.commits
        else:
            assert f"repos/{REPOSITORY}/issues/12/comments?" in path
            rows = self.comments
        return deepcopy(rows[(page - 1) * 100 : page * 100])

    def post(self, path, payload):
        """The only HTTP POST is an existing read-only GraphQL query, never a mutation or comment."""
        assert path == "graphql" and "query(" in payload["query"]
        assert "mutation" not in payload["query"]
        self.calls.append(("QUERY", path))
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [{"id": "thread", "isResolved": False}]
                            if self.unresolved
                            else [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }


def run(client, reviewed=REVIEWED):
    """Run the real preflight against an independently selected head and earlier reviewed commit."""
    return review_preflight.preflight(REPOSITORY, 12, HEAD, BASE, reviewed, client)


def test_final_command_preserves_an_earlier_independent_review_without_requesting_anything():
    """A later tested correction commit is allowed without demanding a new independent review on every push."""
    client = Evidence()
    summary = run(client)
    assert f"Independent Codex review SHA: `{REVIEWED}`" in summary
    assert f"@coderabbitai review\n<!-- oep-coderabbit-head:{HEAD} -->" in summary
    assert "No review has been requested." in summary
    assert client.pr_reads == 2
    assert all(method in {"GET", "QUERY"} for method, _ in client.calls)


def test_exactly_150_changed_files_include_binary_deleted_and_renamed_files():
    """All changed file types count, and the final 50 files are read from the second page."""
    client = Evidence(150)
    assert "Changed files: 150" in run(client)
    assert any(path.endswith("files?per_page=100&page=2") for _, path in client.calls)


def test_missing_second_file_page_cannot_hide_50_changed_files():
    """A full first page does not prove the declared 150-file inventory is complete."""
    client = Evidence(150)
    client.files = client.files[:100]
    with pytest.raises(VerificationError, match="Incomplete"):
        run(client)
    assert any(path.endswith("files?per_page=100&page=2") for _, path in client.calls)


def test_reviewed_commit_can_be_on_the_second_complete_history_page():
    """Read the complete introduced history instead of stopping after page one."""
    client = Evidence()
    client.commits = [{"sha": f"{number:040x}"} for number in range(1, 101)]
    client.commits.extend([{"sha": REVIEWED}, {"sha": HEAD}])
    client.current["commits"] = 102
    assert "No review has been requested." in run(client)
    assert any(path.endswith("commits?per_page=100&page=2") for _, path in client.calls)


@pytest.mark.parametrize(
    "comparison",
    [
        {"merge_base_commit": {"sha": "4" * 40}, "status": "diverged"},
        {"merge_base_commit": {"sha": HEAD}, "status": "behind"},
        {"merge_base_commit": None, "status": "ahead"},
        {"merge_base_commit": {"sha": BASE}, "status": "behind"},
        {},
    ],
)
def test_current_main_must_be_included_before_final_review(comparison):
    """Green head checks cannot authorize review before the predecessor's main merge is included."""
    client = Evidence()
    client.comparison = comparison
    with pytest.raises(VerificationError, match="current main"):
        run(client)
    assert not any("check-runs?" in path for _, path in client.calls)


@pytest.mark.parametrize("count", [151, 0, -1, None, True, "3"])
def test_invalid_or_excessive_authoritative_count_is_rejected_before_listing(count):
    """No truthy/coerced count or 151-file request can reach an expensive provider review."""
    client = Evidence()
    client.current["changed_files"] = count
    with pytest.raises(VerificationError, match="150"):
        run(client)
    assert not any("/files?" in path for _, path in client.calls)


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "malformed"])
def test_changed_file_count_requires_complete_consistent_inventory(fault):
    """The count cannot conceal omitted pages or duplicate/malformed filenames."""
    client = Evidence()
    if fault == "missing":
        client.files.pop()
    elif fault == "extra":
        client.files.append({"filename": "fourth"})
    elif fault == "duplicate":
        client.files[-1] = client.files[0]
    else:
        client.files[-1] = None
    with pytest.raises(VerificationError):
        changed_files(REPOSITORY, 12, client.current, client)


@pytest.mark.parametrize("field", ["head", "base", "draft", "state", "changed_files", "commits"])
def test_concurrent_source_or_readiness_change_invalidates_preflight(field):
    """Re-read source identities and counts after all evidence reads, before emitting the manual command."""
    client = Evidence()
    client.changed = deepcopy(client.current)
    if field in {"head", "base"}:
        client.changed[field]["sha"] = "4" * 40
    else:
        client.changed[field] = {
            "draft": False,
            "state": "closed",
            "changed_files": 4,
            "commits": 3,
        }[field]
    with pytest.raises(VerificationError):
        run(client)


@pytest.mark.parametrize(
    "fault", ["wrong_base_repo", "dependabot", "not_draft", "different_base", "malformed"]
)
def test_only_open_drafts_against_selected_main_can_prepare_a_review(fault):
    """The operator cannot prepare a request against another base or an exempt bot PR."""
    client = Evidence()
    if fault == "wrong_base_repo":
        client.current["base"]["repo"]["full_name"] = "outside/fork"
    elif fault == "dependabot":
        client.current["user"]["login"] = "dependabot[bot]"
    elif fault == "not_draft":
        client.current["draft"] = False
    elif fault == "different_base":
        client.current["base"]["ref"] = "another"
    else:
        client.current["head"] = None
    with pytest.raises(VerificationError):
        run(client)


def test_contributor_fork_uses_only_base_repository_metadata_without_execution():
    """A public fork needs no secret or fork checkout to obtain the same read-only review preflight."""
    client = Evidence()
    client.current["head"]["repo"]["full_name"] = "contributor/fork"
    assert "No review has been requested." in run(client)
    assert all(
        path == "graphql" or path.startswith(f"repos/{REPOSITORY}/") for _, path in client.calls
    )


@pytest.mark.parametrize("fault", ["outside", "duplicate", "missing", "count", "boolean_count"])
def test_independent_review_sha_must_be_in_complete_pr_history(fault):
    """An unrelated/base SHA or incomplete commit list cannot attest to the independent reviewed version."""
    client = Evidence()
    reviewed = REVIEWED
    if fault == "outside":
        reviewed = BASE
    elif fault == "duplicate":
        client.commits[0] = client.commits[-1]
    elif fault == "missing":
        client.commits.pop()
    elif fault == "count":
        client.current["commits"] = 251
    else:
        client.current["commits"] = True
    with pytest.raises(VerificationError):
        run(client, reviewed)


@pytest.mark.parametrize("fault", ["pending", "failed", "wrong_app", "wrong_head"])
def test_codeql_must_pass_on_the_current_head(fault):
    """A successful check from another app/head or a pending security result cannot authorize a request."""
    client = Evidence()
    check = client.checks[-1]
    if fault == "pending":
        check.update(status="in_progress", conclusion=None)
    elif fault == "failed":
        check["conclusion"] = "failure"
    elif fault == "wrong_app":
        check["app"]["id"] = 15368
    else:
        check["head_sha"] = BASE
    with pytest.raises(VerificationError):
        run(client)


def test_unresolved_findings_block_the_manual_command():
    """Existing unresolved review discussions require disposition before any credit-consuming request."""
    client = Evidence()
    client.unresolved = True
    with pytest.raises(VerificationError, match="threads"):
        run(client)


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending", "completed"])
def test_in_flight_or_substantively_completed_review_is_not_requested_again(status):
    """The real review phase evidence prevents another request after a completed review on this head."""
    client = Evidence()
    review = completed_review()
    review["status"] = status
    client.checks.append(review)
    with pytest.raises(VerificationError, match="already"):
        run(client)


def test_successful_but_skipped_provider_check_is_not_a_completed_review():
    """An automatic skip can precede a deliberate manual request, but cannot pass candidate admission."""
    client = Evidence()
    review = completed_review()
    review["output"]["title"] = "Review rate limited"
    client.checks.append(review)
    assert "No review has been requested." in run(client)
    with pytest.raises(VerificationError):
        require_coderabbit_review(client.checks)


@pytest.mark.parametrize("trusted", [False, True])
def test_manual_head_marker_is_only_trusted_for_repository_participants(trusted):
    """An outside account cannot spoof a request marker and block the owner's queue."""
    client = Evidence()
    client.comments = [
        {
            "id": 1,
            "user": {"type": "User"},
            "author_association": "OWNER" if trusted else "NONE",
            "body": f"@coderabbitai review\n<!-- oep-coderabbit-head:{HEAD} -->",
        }
    ]
    if trusted:
        with pytest.raises(VerificationError, match="already exists"):
            run(client)
    else:
        assert "No review has been requested." in run(client)


@pytest.mark.parametrize("fault", ["duplicate", "malformed", "unbounded"])
def test_request_history_is_complete_and_bounded(fault):
    """Missing identity or an unbounded history cannot silently hide an earlier manual request."""
    client = Evidence()
    comment = {"id": 1, "body": "ordinary comment"}
    client.comments = [comment, comment] if fault == "duplicate" else [None]
    if fault == "unbounded":
        client.comments = [{"id": number, "body": "ordinary"} for number in range(1, 1001)]
    with pytest.raises(VerificationError):
        run(client)
    if fault == "unbounded":
        assert sum("/comments?" in path for _, path in client.calls) == 10


@pytest.mark.parametrize("command", ["@CodeRabbitAI Review", "  @coderabbitai review  "])
def test_duplicate_marker_covers_provider_case_insensitive_commands(command):
    """Capitalization or copy whitespace cannot accidentally request the same head twice."""
    client = Evidence()
    client.comments = [
        {
            "id": 1,
            "user": {"type": "User"},
            "author_association": "OWNER",
            "body": f"{command}\n<!-- oep-coderabbit-head:{HEAD} -->",
        }
    ]
    with pytest.raises(VerificationError, match="already exists"):
        run(client)


@pytest.mark.parametrize("fault", ["title", "result", "phase", "missing_phase", "duplicate_phase"])
def test_fake_green_coderabbit_requires_real_analysis_and_feedback(fault):
    """Provider success alone does not mean the source was analyzed or the feedback was generated."""
    review = completed_review()
    if fault == "title":
        review["output"]["title"] = "Review skipped"
    elif fault == "result":
        review["output"]["summary"] = review["output"]["summary"].replace(
            "Review completed", "Review rate limited"
        )
    elif fault == "phase":
        review["output"]["summary"] = review["output"]["summary"].replace(
            "Run analysis checks | Completed", "Run analysis checks | Skipped"
        )
    elif fault == "missing_phase":
        review["output"]["summary"] = review["output"]["summary"].replace(
            "| Generate review feedback | Completed | 1s |\n", ""
        )
    else:
        review["output"]["summary"] += "| Run analysis checks | Completed | 1s |\n"
    with pytest.raises(VerificationError):
        require_coderabbit_review([review])


def dispatch_event():
    """Describe a deliberate main dispatch with explicit human attestations, not a provider receipt."""
    return {
        "repository": {"full_name": REPOSITORY, "default_branch": "main"},
        "inputs": {
            "pull_request": "12",
            "expected_head": HEAD,
            "expected_base": BASE,
            "codex_reviewed_sha": REVIEWED,
            "independent_codex_review_done": True,
            "findings_dispositioned": "true",
        },
    }


@pytest.mark.parametrize(
    "fault", [None, "event", "branch", "attestation", "number", "review_sha", "files"]
)
def test_dispatch_requires_main_identities_and_affirmative_attestations(
    monkeypatch, tmp_path, fault
):
    """Only an explicitly attested trusted-main request writes a local job summary, never a PR comment."""
    event = dispatch_event()
    event_name, branch = "workflow_dispatch", "refs/heads/main"
    if fault == "event":
        event_name = "pull_request"
    elif fault == "branch":
        branch = "refs/heads/unreviewed"
    elif fault == "attestation":
        event["inputs"]["independent_codex_review_done"] = False
    elif fault == "number":
        event["inputs"]["pull_request"] = True
    elif fault == "review_sha":
        event["inputs"]["codex_reviewed_sha"] = "not-a-sha"
    path, summary = tmp_path / "event.json", tmp_path / "summary"
    path.write_text(json.dumps(event))
    for name, value in {
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": branch,
        "GITHUB_SHA": BASE,
        "GITHUB_TOKEN": "test-token",
        "GITHUB_STEP_SUMMARY": str(summary),
    }.items():
        monkeypatch.setenv(name, value)
    client = Evidence()
    if fault == "files":
        client.current["changed_files"] = 151
    monkeypatch.setattr(review_preflight, "GitHub", lambda token: client)
    assert review_preflight.main() == (1 if fault else 0)
    assert summary.exists() is (fault is None)


def test_cli_explains_the_150_file_limit_without_printing_credentials(
    monkeypatch, tmp_path, capsys
):
    """A rejected 151-file dispatch gives actionable split advice without exposing the token."""
    event = tmp_path / "event.json"
    event.write_text(json.dumps(dispatch_event()))
    for name, value in {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": BASE,
        "GITHUB_TOKEN": "never-print-this-token",
    }.items():
        monkeypatch.setenv(name, value)
    client = Evidence(151)
    monkeypatch.setattr(review_preflight, "GitHub", lambda token: client)
    assert review_preflight.main() == 1
    output = capsys.readouterr().out
    assert "150 changed files" in output and "Split larger PRs" in output
    assert "never-print-this-token" not in output


@pytest.mark.parametrize("field", ["head", "base", "head_repo", "base_repo", "user"])
def test_malformed_nested_pr_identity_is_a_clean_refusal(field):
    """Null/list nested identities cannot bypass a same-repository check or leak an AttributeError."""
    client = Evidence()
    if field.endswith("_repo"):
        client.current[field.removesuffix("_repo")]["repo"] = None
    else:
        client.current[field] = []
    with pytest.raises(VerificationError):
        run(client)


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        {"repository": None},
        {"repository": []},
        {"repository": {"full_name": REPOSITORY, "default_branch": "main"}, "inputs": None},
        {"repository": {"full_name": REPOSITORY, "default_branch": "main"}, "inputs": []},
    ],
)
def test_malformed_event_shapes_fail_closed_without_api_access(monkeypatch, tmp_path, event):
    """Invalid input is a clean refusal before any credentialed API operation is attempted."""
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    for name, value in {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": BASE,
    }.items():
        monkeypatch.setenv(name, value)

    def unexpected_client(token):
        """A malformed event must not construct an API client."""
        pytest.fail("API client constructed for invalid event")

    monkeypatch.setattr(review_preflight, "GitHub", unexpected_client)
    assert review_preflight.main() == 1


def test_preflight_workflow_and_native_config_have_no_automatic_or_write_path():
    """Retain explicit structural proof for trusted-main selection, read permissions and disabled native triggers."""
    workflow = (ROOT / ".github/workflows/review-preflight.yaml").read_text()
    assert "  workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow.split("    inputs:", 1)[0]
    assert "issue_comment:" not in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "pull-requests: read" in workflow and ": write" not in workflow
    assert "cancel-in-progress: false" in workflow
    configuration = (
        (ROOT / ".coderabbit.yaml")
        .read_text()
        .split("  auto_review:", 1)[1]
        .split("  path_instructions:", 1)[0]
    )
    for setting in (
        "enabled: false",
        "auto_incremental_review: false",
        "labels: []",
        'description_keyword: ""',
    ):
        assert setting in configuration
