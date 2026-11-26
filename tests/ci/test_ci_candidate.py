"""Candidate CI rejects stale identities and missing or unsuccessful trusted review/check evidence."""

import json
from copy import deepcopy
from pathlib import Path

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
        "user": {"login": "developer", "id": 99, "type": "User"},
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
            (9001, "Repowise review", 3768688),
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
    checks[-1]["output"] = repowise_output(9001)
    return event, current, checks


def repowise_output(run_id, *, head=HEAD, base=BASE):
    """Represent the report and final revision metadata published by the RepoWise check."""
    state = json.dumps({"head": head, "base": base, "run": run_id})
    return {"text": f"### Review points and new health findings (0)\n\n```json\n{state}\n```"}


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
        self.comments = []
        self.repowise = [
            {
                "id": 302,
                "user": {"type": "User", "id": 99},
                "body": f"<!-- oep-repowise-disposition:{HEAD}:{BASE}:9001 -->\nReviewed the current signals.",
            },
        ]
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
        if "reviewDecision" in payload["query"]:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "state": "OPEN",
                            "headRefOid": HEAD,
                            "baseRefOid": BASE,
                            "reviewDecision": None,
                            "latestOpinionatedReviews": {
                                "totalCount": 0,
                                "nodes": [],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            },
                        }
                    }
                }
            }
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
        if "/issues/12/comments?" in path:
            page = int(path.rsplit("=", 1)[1])
            comments = self.repowise + self.comments
            return deepcopy(comments[(page - 1) * 100 : page * 100])
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
    original_listing = client.get_list

    def listing(path):
        """Page the independent inventory without omitting binary or deleted changes."""
        if "/comments?" in path:
            return original_listing(path)
        page = int(path.rsplit("=", 1)[1])
        return files[(page - 1) * 100 : page * 100]

    client.get_list = listing
    if count is True or count > 150:
        with pytest.raises(VerificationError, match="150"):
            check(event, REPOSITORY, client)
    else:
        assert check(event, REPOSITORY, client) == HEAD


@pytest.mark.parametrize(
    "fault",
    [
        "missing-scan",
        "pending",
        "skipped",
        "wrong-app",
        "stale-scan",
        "stale-head",
        "stale-base",
        "missing-output",
        "malformed-output",
        "missing-disposition",
        "empty-disposition",
        "wrong-actor",
        "old-disposition",
    ],
)
def test_candidate_requires_fresh_repowise_disposition(candidate, fault):
    """No stale report or attestation can accompany a substantively reviewed candidate."""
    event, current, checks = candidate
    client = Evidence(current, checks)
    scan = checks[-1]
    (disposition,) = client.repowise
    if fault == "missing-scan":
        checks.pop()
    elif fault == "pending":
        scan.update(status="in_progress", conclusion=None)
    elif fault == "skipped":
        scan["conclusion"] = "skipped"
    elif fault == "wrong-app":
        scan["app"]["id"] = 1
    elif fault == "stale-scan":
        scan["id"] = 9002
    elif fault == "stale-head":
        scan["output"] = repowise_output(9001, head="3" * 40)
    elif fault == "stale-base":
        scan["output"] = repowise_output(9001, base="3" * 40)
    elif fault == "missing-output":
        scan.pop("output")
    elif fault == "malformed-output":
        scan["output"]["text"] = scan["output"]["text"].replace("{", "[", 1)
    elif fault == "missing-disposition":
        client.repowise.pop()
    elif fault == "empty-disposition":
        disposition["body"] = disposition["body"].partition("\n")[0]
    elif fault == "wrong-actor":
        disposition["user"]["id"] = 100
    else:
        disposition["body"] = disposition["body"].replace(":9001", ":9000")
    with pytest.raises(VerificationError, match="RepoWise"):
        check(event, REPOSITORY, client)


def test_ready_rescan_uses_check_output_when_bot_comment_still_names_old_run(candidate):
    """A Ready rescan needs its own disposition even when the bot keeps its earlier comment."""
    event, current, checks = candidate
    event["action"] = "ready_for_review"
    client = Evidence(current, checks)
    client.comments.append(
        {
            "id": 301,
            "user": {"login": "repowise-bot[bot]", "type": "Bot", "id": 286013898},
            "performed_via_github_app": {"id": 3768688},
            "body": f'<!-- repowise-bot:v4 {{"head":"{HEAD}","base":"{BASE}","run":9001}} -->',
        }
    )
    rescan = {**checks[-1], "id": 9002, "status": "in_progress", "conclusion": None}
    checks.append(rescan)
    with pytest.raises(VerificationError, match="finish successfully"):
        check(event, REPOSITORY, client)

    rescan.update(status="completed", conclusion="success", output=repowise_output(9002))
    with pytest.raises(VerificationError, match="disposition"):
        check(event, REPOSITORY, client)

    client.repowise[0]["body"] = client.repowise[0]["body"].replace(":9001", ":9002")
    assert check(event, REPOSITORY, client) == HEAD


@pytest.mark.parametrize("completed", [False, True])
def test_candidate_rejects_repowise_rescan_during_admission(candidate, completed):
    """Recheck the scan before admission, even when the replacement already passed."""
    event, current, checks = candidate
    client = Evidence(current, checks)
    original_listing = client.get_list

    def list_with_rescan(path):
        """Publish a newer app result after admission first read the checks."""
        if "/issues/12/comments?" in path and checks[-1]["id"] == 9001:
            checks.append(
                {
                    **checks[-1],
                    "id": 9002,
                    "status": "completed" if completed else "in_progress",
                    "conclusion": "success" if completed else None,
                    "output": repowise_output(9002),
                }
            )
        return original_listing(path)

    client.get_list = list_with_rescan
    with pytest.raises(VerificationError, match="RepoWise"):
        check(event, REPOSITORY, client)


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
        "Automated Review is disabled — Enable 'Automated Code Review' in General Settings",
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


@pytest.mark.parametrize(
    "earlier",
    [
        "missing",
        "failure",
        "in_progress",
        "skipped",
        "warnings",
        "unknown_skip",
        "wrong_app",
        "wrong_head",
    ],
)
@pytest.mark.parametrize(
    "summary",
    [
        "No New Commits (No changes detected since last review)",
        "No changed files in this pull request.",
        "Automated Review is disabled — Enable 'Automated Code Review' in General Settings",
    ],
)
def test_kody_metadata_skip_cannot_supply_or_hide_review_evidence(kody_candidate, earlier, summary):
    """Known metadata needs authentic current-head success and cannot hide a later substantive problem."""
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
    elif earlier == "wrong_app":
        checks[2]["app"] = {"id": 999}
    elif earlier == "wrong_head":
        checks[2]["head_sha"] = "3" * 40
    else:
        attempt = {**checks[2], "id": 5, "conclusion": earlier}
        if earlier == "in_progress":
            attempt.update(status="in_progress", conclusion=None)
        elif earlier == "warnings":
            attempt.update(
                conclusion="success",
                output={
                    "title": "Code Review Completed with Warnings",
                    "summary": "Review finished with warnings.",
                },
            )
        elif earlier == "unknown_skip":
            attempt.update(
                conclusion="skipped",
                output={"title": "Code Review Skipped", "summary": "Unrecognized skip reason"},
            )
        checks.append(attempt)
    checks.append(metadata)
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, Evidence(current, checks))


@pytest.mark.parametrize("provider", ["greptile", "codex"])
@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "bot-id",
        "bot-login",
        "bot-type",
        "app-id",
        "stale-head",
        "missing-publication",
        "duplicate-publication",
        "incomplete",
        "check-only",
        "reaction-only",
    ],
)
def test_new_provider_admission_requires_authentic_exact_head_publication(
    candidate, provider, mutation
):
    """Only observed complete provider evidence can replace CodeRabbit, never spoofed or stale status."""
    event, current, checks = candidate
    checks[:] = [row for row in checks if row["name"] != "CodeRabbit"]
    client = Evidence(current, checks)
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures/review_providers.json")
        .read_text(encoding="utf-8")
        .replace("@HEAD@", HEAD)
        .replace("@REPOSITORY@", REPOSITORY)
    )
    comment = fixtures[f"{provider}_comment"]
    client.comments = [comment]
    if provider == "greptile":
        checks.append(fixtures["greptile_check"])
    if mutation == "bot-id":
        comment["user"]["id"] = 99
    elif mutation == "bot-login":
        comment["user"]["login"] = "impostor[bot]"
    elif mutation == "bot-type":
        comment["user"]["type"] = "User"
    elif mutation == "app-id":
        comment["performed_via_github_app"]["id"] = 99
    elif mutation == "stale-head":
        comment["body"] = comment["body"].replace(HEAD, "3" * 40)
    elif mutation in {"missing-publication", "check-only"}:
        client.comments = []
    elif mutation == "duplicate-publication":
        client.comments.append({**deepcopy(comment), "id": 202})
    elif mutation == "incomplete":
        if provider == "greptile":
            checks[-1]["output"]["summary"] = "Review was skipped"
        else:
            comment["body"] = comment["body"].replace(
                '"status":"completed"', '"status":"in_progress"'
            )
    elif mutation == "reaction-only":
        comment["body"] = "Review received. No major issues."
        comment["reactions"] = {"eyes": 1, "+1": 1}
    if mutation == "none":
        assert check(event, REPOSITORY, client) == HEAD
    else:
        with pytest.raises(VerificationError):
            check(event, REPOSITORY, client)


@pytest.mark.parametrize("provider", ["greptile", "codex"])
def test_new_provider_success_cannot_bypass_formal_change_requests(candidate, provider):
    """Authenticated provider completion never grants authority to ignore a formal objection."""
    from tests.ci.test_formal_reviews import FormalEvidence, review_page

    event, current, checks = candidate
    checks[:] = [row for row in checks if row["name"] != "CodeRabbit"]
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures/review_providers.json")
        .read_text(encoding="utf-8")
        .replace("@HEAD@", HEAD)
        .replace("@REPOSITORY@", REPOSITORY)
    )
    if provider == "greptile":
        checks.append(fixtures["greptile_check"])
    response = review_page([{"id": "PRR_objection", "state": "CHANGES_REQUESTED"}])
    client = FormalEvidence(current, checks, pages=[response])
    client.comments = [fixtures[f"{provider}_comment"]]
    with pytest.raises(VerificationError, match="CHANGES_REQUESTED"):
        check(event, REPOSITORY, client)


@pytest.mark.parametrize(
    "label", [r"docs: add \[Unreleased\] entry", r"fix \[nested \[label\]\]", r"fix \\ path"]
)
def test_greptile_review_accepts_escaped_markdown_commit_subject(candidate, label):
    """Markdown escapes in a commit label do not weaken or prevent exact-repository full-head proof."""
    event, current, checks = candidate
    checks[:] = [row for row in checks if row["name"] != "CodeRabbit"]
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures/review_providers.json")
        .read_text(encoding="utf-8")
        .replace("@HEAD@", HEAD)
        .replace("@REPOSITORY@", REPOSITORY)
    )
    checks.append(fixtures["greptile_check"])
    client = Evidence(current, checks)
    comment = fixtures["greptile_comment"]
    comment["body"] = comment["body"].replace('"Example change"', label)
    client.comments = [comment]
    assert check(event, REPOSITORY, client) == HEAD
    comment["body"] = comment["body"].replace(HEAD, "3" * 40)
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, client)
