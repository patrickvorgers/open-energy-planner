"""Candidate CI rejects stale identities and missing or unsuccessful trusted review/check evidence."""

import json
from copy import deepcopy

import pytest

from script.ci.check_ci_candidate import check, main
from script.ci.check_commit_signatures import VerificationError

REPOSITORY = "owner/planner"
BASE = "1" * 40
HEAD = "2" * 40


@pytest.fixture
def candidate():
    """Provide an explicit label event and current-head evidence from the real service identities."""
    event = {
        "number": 12,
        "repository": {"full_name": REPOSITORY},
        "action": "labeled",
        "label": {"name": "ci: run"},
        "pull_request": {"base": {"sha": BASE}, "head": {"sha": HEAD}},
    }
    current = {
        "state": "open",
        "draft": False,
        "base": {"sha": BASE},
        "head": {"sha": HEAD},
        "user": {"login": "developer"},
        "changed_files": 3,
    }
    checks = [
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
            (3, "CodeRabbit", 347564),
            (4, "CodeQL", 57789),
        )
    ]
    checks[2]["output"] = {
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
    }
    return event, current, checks


@pytest.mark.parametrize("action", ["ready_for_review", "labeled"])
def test_runner_preference_cannot_change_admission_requirements(candidate, action):
    """A stale personal preference remains inert and cannot bypass current-head review."""
    event, current, checks = candidate
    event["action"] = action
    for pull_request in (event["pull_request"], current):
        pull_request["labels"] = [{"name": "ci-runner: prefer-local"}]
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD
    checks[2]["conclusion"] = "failure"
    with pytest.raises(VerificationError, match="CodeRabbit"):
        check(event, REPOSITORY, Evidence(current, checks))


class Evidence:
    """Serve bounded fake API pages and optionally change the PR before final admission."""

    def __init__(self, current, checks, changed=None):
        """Keep independent current identities and test-controlled check rows."""
        self.current = current
        self.checks = checks
        self.changed = changed
        self.pr_reads = 0
        self.requests = []

    def get(self, path):
        """Return PR metadata or one independently paginated hundred-record check page."""
        self.requests.append(path)
        if path == "repos/owner/planner/pulls/12":
            self.pr_reads += 1
            return deepcopy(self.changed if self.changed and self.pr_reads > 1 else self.current)
        if path == f"repos/{REPOSITORY}/compare/{BASE}...{HEAD}?per_page=1":
            return {"merge_base_commit": {"sha": BASE}, "status": "ahead"}
        assert path.startswith(
            f"repos/owner/planner/commits/{HEAD}/check-runs?filter=all&per_page=100&page="
        )
        page = int(path.rsplit("=", 1)[1])
        return {
            "check_runs": self.checks[(page - 1) * 100 : page * 100],
            "total_count": len(self.checks),
        }

    def post(self, path, payload):
        """The fixture's completed review has no unresolved discussions."""
        assert path == "graphql"
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    def get_list(self, path):
        """Return every changed file without concealing additions, removals or binary changes."""
        assert path == "repos/owner/planner/pulls/12/files?per_page=100&page=1"
        return [{"filename": f"file-{index}.py"} for index in range(3)]


def test_reviewed_candidate_is_bound_to_current_head_twice(candidate):
    """A label admits exactly the independently checked head after both current PR reads agree."""
    event, current, checks = candidate
    client = Evidence(current, checks)
    assert check(event, REPOSITORY, client) == "2" * 40
    assert client.pr_reads == 2


@pytest.mark.parametrize("count", [150, 151, True])
def test_candidate_file_limit_cannot_be_bypassed_by_a_manual_review(candidate, count):
    """A substantively reviewed PR must still fit the complete 150-file inventory at admission."""
    event, current, checks = candidate
    current["changed_files"] = count
    client = Evidence(current, checks)
    files = [{"filename": f"file-{index}.bin", "status": "removed"} for index in range(150)]

    def listing(path):
        """Page the independent inventory without omitting binary or deleted changes."""
        page = int(path.rsplit("=", 1)[1])
        return files[(page - 1) * 100 : page * 100]

    client.get_list = listing
    if count is True or count > 150:
        with pytest.raises(VerificationError, match="150"):
            check(event, REPOSITORY, client)
    else:
        assert check(event, REPOSITORY, client) == HEAD


@pytest.mark.parametrize("check_index", [0, 1, 2, 3])
@pytest.mark.parametrize(
    "conclusion,status",
    [
        (None, "in_progress"),
        ("failure", "completed"),
        ("cancelled", "completed"),
        ("neutral", "completed"),
        ("skipped", "completed"),
    ],
)
def test_unsuccessful_or_pending_check_does_not_admit_candidate(
    candidate, check_index, conclusion, status
):
    """Each prerequisite must finish successfully. Skipped signature metadata without a prior success also fails."""
    event, current, checks = candidate
    checks[check_index].update(conclusion=conclusion, status=status)
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, Evidence(current, checks))


@pytest.mark.parametrize("check_index", [0, 1, 2, 3])
@pytest.mark.parametrize("fault", ["missing", "forged_app", "different_head"])
def test_untrusted_or_missing_evidence_does_not_admit_candidate(candidate, check_index, fault):
    """A similarly named check from another app or revision cannot satisfy the gate."""
    event, current, checks = candidate
    if fault == "missing":
        checks.pop(check_index)
    elif fault == "forged_app":
        checks[check_index]["app"]["id"] = 999
    else:
        checks[check_index]["head_sha"] = "3" * 40
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, Evidence(current, checks))


def test_later_failed_review_on_second_page_supersedes_old_success(candidate):
    """A failing later review cannot hide behind an earlier passing review or the first page."""
    event, current, checks = candidate
    checks.extend({"id": number, "name": "Other", "head_sha": HEAD} for number in range(5, 101))
    checks.append({**checks[2], "id": 101, "conclusion": "failure"})
    client = Evidence(current, checks)
    with pytest.raises(VerificationError, match="CodeRabbit"):
        check(event, REPOSITORY, client)
    assert any(path.endswith("page=2") for path in client.requests)


@pytest.mark.parametrize("fault", ["head", "base", "closed", "draft"])
@pytest.mark.parametrize("during_check", [False, True])
def test_changed_or_unready_pr_cannot_admit_old_candidate(candidate, fault, during_check):
    """Revisions, retargeting and readiness are checked before and after reading check evidence."""
    event, current, checks = candidate
    changed = deepcopy(current)
    if fault in ("head", "base"):
        changed[fault]["sha"] = "3" * 40
    elif fault == "closed":
        changed["state"] = "closed"
    else:
        changed["draft"] = True
    client = Evidence(current, checks, changed) if during_check else Evidence(changed, checks)
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, client)


@pytest.mark.parametrize("check_index", [0, 1])
def test_skipped_metadata_check_retains_existing_success(candidate, check_index):
    """Skipped metadata-only quick/signature jobs do not invalidate an existing checked head."""
    event, current, checks = candidate
    checks.append({**checks[check_index], "id": 5, "conclusion": "skipped"})
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD


def test_dependabot_keeps_existing_review_exception_and_other_checks(candidate):
    """Dependabot needs both quick checks and verified commits, while CodeRabbit remains exempt."""
    event, current, checks = candidate
    current["user"]["login"] = "dependabot[bot]"
    checks.pop(2)
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD
    checks[0]["conclusion"] = "failure"
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, Evidence(current, checks))


@pytest.mark.parametrize("check_name", ["Quick checks", "GitHub Verified PR commits", "CodeQL"])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "neutral", None])
def test_dependabot_exception_keeps_all_nonreview_prerequisites(candidate, check_name, conclusion):
    """An exempt bot still needs successful cheap checks, signatures and CodeQL on its exact revision."""
    event, current, checks = candidate
    current["user"]["login"] = "dependabot[bot]"
    checks[:] = [row for row in checks if row["name"] != "CodeRabbit"]
    blocked = next(row for row in checks if row["name"] == check_name)
    blocked.update(
        conclusion=conclusion, status="in_progress" if conclusion is None else "completed"
    )
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, Evidence(current, checks))


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_bot_updates_with_retained_label_do_not_request_a_candidate(candidate, action):
    """Preserving bot readiness cannot turn a new commit or old label into candidate authorization."""
    event, current, checks = candidate
    event["action"] = action
    current["user"]["login"] = "dependabot[bot]"
    current["labels"] = [{"name": "ci: run"}]
    checks.pop(2)
    with pytest.raises(VerificationError, match="Ready for review or a ci: run retry"):
        check(event, REPOSITORY, Evidence(current, checks))


def test_dependabot_candidate_still_rejects_unresolved_findings(candidate):
    """The review-provider exception cannot dismiss an existing unresolved review finding."""
    event, current, checks = candidate
    current["user"]["login"] = "dependabot[bot]"
    checks.pop(2)

    class OpenFindingEvidence(Evidence):
        """Report one independent unresolved review thread for the bot revision."""

        def post(self, path, payload):
            """Retain the normal API envelope while returning a real blocking discussion."""
            response = super().post(path, payload)
            response["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"] = [
                {"id": "unresolved-dependency-finding", "isResolved": False}
            ]
            return response

    with pytest.raises(VerificationError, match="Open or invalid review threads remain"):
        check(event, REPOSITORY, OpenFindingEvidence(current, checks))


@pytest.mark.parametrize(
    "action,label",
    [
        ("synchronize", "ci: run"),
        ("labeled", "documentation"),
        ("opened", "ci: run"),
        ("labeled", "ci:full"),
        ("labeled", "ci: full"),
        ("labeled", "ci:run"),
        ("labeled", "ci-scope: full"),
        ("labeled", "ci-scope: frontend"),
    ],
)
def test_old_label_or_unrelated_event_cannot_start_full_pr_ci(candidate, action, label):
    """A retained label is not an automatic request to test every new commit."""
    event, current, checks = candidate
    event.update(action=action, label={"name": label})
    client = Evidence(current, checks)
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, client)
    assert not client.requests


@pytest.mark.parametrize("event_name", ["push", "release", "workflow_dispatch"])
def test_non_pr_events_retain_full_pipeline(monkeypatch, event_name):
    """Main, release and manual builds do not require unrelated PR review metadata."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert main() == 0


def test_cli_reports_unavailable_evidence_without_token_contents(
    candidate, monkeypatch, tmp_path, capsys
):
    """An API outage fails before a Docker build and never prints token-bearing exception details."""
    event, _, _ = candidate
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    for name, value in {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_TOKEN": "private-test-token",
    }.items():
        monkeypatch.setenv(name, value)

    def unavailable(self, path):
        """Model the existing bounded client's sanitized service outage contract."""
        raise VerificationError("GitHub verification unavailable or invalid")

    monkeypatch.setattr("script.ci.check_ci_candidate.GitHub.get", unavailable)
    assert main() == 1
    assert "private-test-token" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "evidence",
    [
        {"total_count": None, "check_runs": []},
        {"total_count": 1, "check_runs": []},
        {"total_count": 0, "check_runs": [{"id": 1, "head_sha": HEAD}]},
        {"total_count": 2, "check_runs": [{"id": 1, "head_sha": HEAD}] * 2},
    ],
)
def test_incomplete_or_ambiguous_api_data_cannot_admit_candidate(candidate, evidence):
    """Malformed counts, omitted pages and duplicate records fail closed before builds."""
    event, current, checks = candidate
    client = Evidence(current, checks)
    original_get = client.get

    def get(path):
        """Keep PR metadata valid while returning deliberately inconsistent check evidence."""
        return original_get(path) if path.endswith("/pulls/12") else evidence

    client.get = get
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, client)


@pytest.fixture
def kody_candidate(candidate):
    """Replace CodeRabbit with independently specified successful Kody provider evidence."""
    event, current, checks = deepcopy(candidate)
    checks[2].update(
        name="Kody Code Review",
        app={"id": 413034},
        output={
            "title": "Code Review Complete",
            "summary": "Review finished successfully. Suggestions (if any) were posted as PR/file comments.",
        },
    )
    return event, current, checks


def test_kody_review_alone_admits_candidate(kody_candidate):
    """A substantive Kody review satisfies the same current-head admission as CodeRabbit."""
    event, current, checks = kody_candidate
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD


@pytest.mark.parametrize("other_status", ["skipped", "failure", "in_progress"])
@pytest.mark.parametrize("provider", ["CodeRabbit", "Kody Code Review"])
def test_either_completed_provider_is_sufficient(candidate, kody_candidate, provider, other_status):
    """An unavailable alternative provider does not invalidate the completed selected reviewer."""
    event, current, checks = candidate if provider == "CodeRabbit" else kody_candidate
    alternative = deepcopy(kody_candidate[2][2] if provider == "CodeRabbit" else candidate[2][2])
    alternative.update(id=5, conclusion=other_status, status="completed")
    if other_status == "in_progress":
        alternative.update(status="in_progress", conclusion=None)
    checks.append(alternative)
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "wrong_app",
        "wrong_head",
        "pending",
        "failed",
        "skipped",
        "neutral",
        "starting",
        "warnings",
        "missing_output",
        "unknown_summary",
    ],
)
def test_kody_requires_real_current_head_completion(kody_candidate, fault):
    """Kody skips, warnings, forged checks and unfinished work cannot replace a review."""
    event, current, checks = kody_candidate
    review = checks[2]
    if fault == "missing":
        checks.pop(2)
    elif fault == "wrong_app":
        review["app"]["id"] = 999
    elif fault == "wrong_head":
        review["head_sha"] = "3" * 40
    elif fault == "pending":
        review.update(status="in_progress", conclusion=None)
    elif fault in {"failed", "skipped", "neutral"}:
        review["conclusion"] = {"failed": "failure"}.get(fault, fault)
    elif fault == "starting":
        review["output"]["title"] = "Code Review Starting"
    elif fault == "warnings":
        review["output"]["title"] = "Code Review Completed with Warnings"
    elif fault == "missing_output":
        review.pop("output")
    else:
        review["output"]["summary"] = "Review skipped."
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, Evidence(current, checks))


@pytest.mark.parametrize("status", ["in_progress", "failure", "skipped"])
def test_latest_kody_attempt_supersedes_old_success(kody_candidate, status):
    """A later incomplete Kody attempt prevents reuse of its earlier successful review."""
    event, current, checks = kody_candidate
    latest = {**checks[2], "id": 5, "status": "completed", "conclusion": status}
    if status == "in_progress":
        latest.update(status="in_progress", conclusion=None)
    checks.append(latest)
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, Evidence(current, checks))


def test_kody_does_not_bypass_open_findings(kody_candidate):
    """Successful Kody evidence cannot admit a candidate with unresolved discussions."""
    event, current, checks = kody_candidate

    class OpenFinding(Evidence):
        """Serve one unresolved review thread independent of the selected provider."""

        def post(self, path, payload):
            """Return a complete page containing an actionable unresolved thread."""
            response = super().post(path, payload)
            response["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"] = [
                {"id": "finding", "isResolved": False}
            ]
            return response

    with pytest.raises(VerificationError, match="threads"):
        check(event, REPOSITORY, OpenFinding(current, checks))


@pytest.mark.parametrize("provider", ["CodeRabbit", "Kody-ai"])
@pytest.mark.parametrize("fault", ["diverged", "missing", "wrong-base", "behind", "outage"])
def test_every_reviewer_requires_base_inclusion(candidate, kody_candidate, provider, fault):
    """Neither reviewer can admit a head without trusted proof that it contains the intended base."""
    event, current, checks = candidate if provider == "CodeRabbit" else kody_candidate

    class MissingBase(Evidence):
        """Return independently malformed or unavailable ancestry evidence."""

        def get(self, path):
            """Keep ordinary evidence valid while breaking only the base comparison."""
            if "/compare/" not in path:
                return super().get(path)
            if fault == "outage":
                raise VerificationError("Comparison unavailable")
            return {
                "merge_base_commit": None
                if fault == "missing"
                else {"sha": "3" * 40 if fault == "wrong-base" else BASE},
                "status": fault if fault in {"diverged", "behind"} else "ahead",
            }

    with pytest.raises(VerificationError):
        check(event, REPOSITORY, MissingBase(current, checks))


@pytest.mark.parametrize("action", ["ready_for_review", "labeled"])
@pytest.mark.parametrize(
    "summary",
    [
        "No New Commits (No changes detected since last review)",
        "No changed files in this pull request.",
    ],
)
def test_kody_no_new_commits_skip_preserves_completed_review(kody_candidate, action, summary):
    """Ready metadata does not invalidate substantive Kody evidence for the unchanged head."""
    event, current, checks = kody_candidate
    event["action"] = action
    checks.append(
        {
            **checks[2],
            "id": 5,
            "conclusion": "skipped",
            "output": {
                "title": "Code Review Skipped",
                "summary": summary,
            },
        }
    )
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD


@pytest.mark.parametrize("earlier", ["missing", "failure", "in_progress", "skipped"])
@pytest.mark.parametrize(
    "summary",
    [
        "No New Commits (No changes detected since last review)",
        "No changed files in this pull request.",
    ],
)
def test_kody_metadata_skip_cannot_supply_or_hide_review_evidence(kody_candidate, earlier, summary):
    """A no-change skip needs real success and cannot hide an actual later failed or pending attempt."""
    event, current, checks = kody_candidate
    metadata = {
        **checks[2],
        "id": 6,
        "conclusion": "skipped",
        "output": {
            "title": "Code Review Skipped",
            "summary": summary,
        },
    }
    if earlier == "missing":
        checks.pop(2)
    else:
        attempt = {**checks[2], "id": 5, "conclusion": earlier}
        if earlier == "in_progress":
            attempt.update(status="in_progress", conclusion=None)
        checks.append(attempt)
    checks.append(metadata)
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, Evidence(current, checks))
