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
