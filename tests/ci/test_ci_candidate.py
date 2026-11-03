"""Candidate CI rejects stale identities and missing or unsuccessful trusted review/check evidence."""

import json
from copy import deepcopy

import pytest

from script.ci import check_ci_candidate
from script.ci.check_ci_candidate import available_runner_labels, check, main, runner_labels
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
        )
    ]
    return event, current, checks


@pytest.fixture
def personal_candidate(candidate):
    """Request only the repository owner's personal runner from a private same-repository PR."""
    event, current, checks = candidate
    event["repository"]["private"] = True
    event["repository"]["owner"] = {"login": "owner", "type": "User"}
    event["sender"] = {"login": "owner"}
    for pull_request in (event["pull_request"], current):
        pull_request["labels"] = [{"name": "ci-runner: prefer-local"}]
        pull_request["head"]["repo"] = {"full_name": REPOSITORY}
        pull_request["user"] = {"login": "owner"}
    return event, current, checks


def test_personal_runner_preserves_review_and_exact_identity_gates(personal_candidate):
    """The opted-in author selects personal capacity without bypassing current-head evidence."""
    event, current, checks = personal_candidate
    assert runner_labels(event, REPOSITORY) == ["self-hosted", "X64", "oep-ci-local"]
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD
    checks[2]["conclusion"] = "failure"
    with pytest.raises(VerificationError, match="CodeRabbit"):
        check(event, REPOSITORY, Evidence(current, checks))


@pytest.mark.parametrize("action", ["ready_for_review", "labeled"])
def test_ready_and_retry_preserve_author_owned_runner_routing(personal_candidate, action):
    """Ready uses the same owner-only routing and requester boundary as an explicit retry."""
    event, current, checks = personal_candidate
    event["action"] = action
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD
    event["sender"]["login"] = "someone-else"
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD
    assert runner_labels(event, REPOSITORY) == ["ubuntu-24.04"]


@pytest.mark.parametrize("author", ["alice", "bob", "Another-Developer"])
def test_other_authors_always_use_github_even_with_local_preference(personal_candidate, author):
    """A contributor cannot select the owner's hardware by applying a local preference label."""
    event, current, checks = personal_candidate
    event["sender"]["login"] = author
    for pull_request in (event["pull_request"], current):
        pull_request["user"]["login"] = author
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD
    assert runner_labels(event, REPOSITORY) == ["ubuntu-24.04"]


@pytest.mark.parametrize("owner", ["alice", "bob", "Another-Owner"])
def test_owner_only_policy_uses_metadata_without_names_in_code_or_labels(personal_candidate, owner):
    """Changing the personal repository owner needs no hardcoded login or runner-label migration."""
    event, _, _ = personal_candidate
    repository = f"{owner}/planner"
    event["repository"].update(full_name=repository, owner={"login": owner, "type": "User"})
    event["sender"]["login"] = owner
    event["pull_request"]["head"]["repo"]["full_name"] = repository
    event["pull_request"]["user"]["login"] = owner
    assert runner_labels(event, repository) == ["self-hosted", "X64", "oep-ci-local"]


@pytest.mark.parametrize(
    "owner",
    [
        None,
        {},
        {"login": "owner"},
        {"login": "owner", "type": "Organization"},
        {"login": "another-owner", "type": "User"},
    ],
)
def test_unknown_or_organization_owner_never_selects_local_hardware(personal_candidate, owner):
    """Missing, mismatched and organization owner evidence retains hosted execution."""
    event, _, _ = personal_candidate
    event["repository"]["owner"] = owner
    assert runner_labels(event, REPOSITORY) == ["ubuntu-24.04"]


@pytest.mark.parametrize("prefer_local", [False, True])
def test_bad_optional_runner_secret_cannot_block_github_fallback(
    personal_candidate, monkeypatch, tmp_path, capsys, prefer_local
):
    """Default and local-preference requests retain hosted capacity when optional lookup setup is invalid."""
    event, current, checks = personal_candidate
    if not prefer_local:
        event["pull_request"]["labels"] = current["labels"] = []
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    output = tmp_path / "outputs"
    for name, value in {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_TOKEN": "workflow-token",
        "OEP_RUNNER_READ_TOKEN": "invalid\nprivate-runner-token",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(name, value)
    clients = []

    def client(token):
        """Keep normal admission evidence valid while rejecting only the optional credential."""
        clients.append(token)
        if token == "workflow-token":
            return Evidence(current, checks)
        raise VerificationError("Invalid optional runner credential")

    monkeypatch.setattr(check_ci_candidate, "GitHub", client)
    assert main() == 0
    assert output.read_text() == 'runner_labels=["ubuntu-24.04"]\n'
    assert len(clients) == (2 if prefer_local else 1)
    assert "private-runner-token" not in capsys.readouterr().out


@pytest.mark.parametrize("fault", ["public", "fork", "labels"])
def test_personal_runner_rejects_foreign_or_untrusted_requests(personal_candidate, fault):
    """Public, fork, other-author and malformed requests cannot select personal hardware."""
    event, current, checks = personal_candidate
    if fault == "public":
        event["repository"]["private"] = False
    elif fault == "fork":
        event["pull_request"]["head"]["repo"]["full_name"] = "outsider/fork"
    else:
        event["pull_request"]["labels"] = [None]
    client = Evidence(current, checks)
    with pytest.raises(VerificationError):
        check(event, REPOSITORY, client)
    assert client.requests == []


@pytest.mark.parametrize("during_check", [False, True])
def test_personal_runner_choice_is_checked_against_current_pr(personal_candidate, during_check):
    """Removing the local preference during admission cannot silently change the requested route."""
    event, current, checks = personal_candidate
    changed = deepcopy(current)
    changed["labels"] = []
    client = Evidence(current, checks, changed) if during_check else Evidence(changed, checks)
    with pytest.raises(VerificationError, match="Runner choice changed"):
        check(event, REPOSITORY, client)


@pytest.mark.parametrize("operating_system", ["linux", "Linux"])
def test_online_idle_personal_runner_is_selected(operating_system):
    """Linux runners can execute the same container candidate with a personal label."""
    requested = ["self-hosted", "X64", "oep-ci-local"]

    class RunnerEvidence:
        """Serve one independently specified online personal runner."""

        def get(self, path):
            """Require the bounded repository endpoint rather than arbitrary runner locations."""
            if policy := private_runner_policy(path):
                return policy
            assert path == "repos/owner/planner/actions/runners?per_page=100&page=1"
            return {
                "total_count": 1,
                "runners": [
                    {
                        "status": "online",
                        "busy": False,
                        "os": operating_system,
                        "labels": [
                            {"name": name}
                            for name in ("self-hosted", "x64", "oep-ci-local", operating_system)
                        ],
                    }
                ],
            }

    assert available_runner_labels(REPOSITORY, requested, RunnerEvidence()) == [
        *requested,
        operating_system.lower(),
    ]


@pytest.mark.parametrize(
    "fault",
    [
        "offline",
        "busy",
        "foreign",
        "missing",
        "denied",
        "malformed",
        "unsupported_os",
        "native_windows",
        "missing_os",
        "missing_os_label",
    ],
)
def test_unavailable_personal_runner_automatically_uses_github(fault):
    """Offline, occupied, foreign and unverifiable runners do not queue or weaken the required tests."""
    requested = ["self-hosted", "X64", "oep-ci-local"]

    class RunnerEvidence:
        """Model independent runner availability and read-permission failures."""

        def get(self, path):
            """Return only the chosen availability defect without accessing actual machines."""
            if fault == "denied":
                raise VerificationError("Unavailable")
            if policy := private_runner_policy(path):
                return policy
            if fault == "malformed":
                return {"total_count": 1, "runners": [None]}
            if fault == "missing":
                return {"total_count": 0, "runners": []}
            operating_system = (
                "macos"
                if fault == "unsupported_os"
                else "windows"
                if fault == "native_windows"
                else "linux"
            )
            names = [
                "self-hosted",
                "X64",
                "oep-ci-unrelated" if fault == "foreign" else "oep-ci-local",
            ]
            if fault != "missing_os_label":
                names.append(operating_system)
            return {
                "total_count": 1,
                "runners": [
                    {
                        "status": "offline" if fault == "offline" else "online",
                        "busy": fault == "busy",
                        "os": None if fault == "missing_os" else operating_system,
                        "labels": [{"name": name} for name in names],
                    }
                ],
            }

    assert available_runner_labels(REPOSITORY, requested, RunnerEvidence()) == ["ubuntu-24.04"]


def test_missing_availability_credential_retains_github_default():
    """An unconfigured repository still runs the candidate on GitHub without additional secrets."""
    assert available_runner_labels(REPOSITORY, ["self-hosted", "X64", "oep-ci-local"], None) == [
        "ubuntu-24.04"
    ]


def test_runner_lookup_is_bounded_even_when_api_reports_more_pages():
    """A large runner inventory cannot consume unbounded admission time."""
    calls = []

    class RunnerEvidence:
        """Provide a repository larger than the explicitly bounded lookup."""

        def get(self, path):
            """Record each page of offline runners with independently specified labels."""
            if policy := private_runner_policy(path):
                return policy
            calls.append(path)
            return {
                "total_count": 1100,
                "runners": [{"status": "offline", "busy": False, "os": "windows", "labels": []}]
                * 100,
            }

    assert available_runner_labels(
        REPOSITORY, ["self-hosted", "X64", "oep-ci-local"], RunnerEvidence()
    ) == ["ubuntu-24.04"]
    assert len(calls) == 10


def private_runner_policy(path):
    """Provide independent server-enforced private fork workflow settings for runner fixtures."""
    if path == "repos/owner/planner":
        return {"private": True, "full_name": "owner/planner"}
    if path == "repos/owner/planner/actions/permissions/fork-pr-workflows-private-repos":
        return {"run_workflows_from_fork_pull_requests": False}
    return None


@pytest.mark.parametrize("fault", ["public", "foreign", "forks_enabled", "unknown_policy"])
def test_unsafe_server_policy_never_selects_personal_capacity(fault):
    """Runner preferences cannot relax the externally enforced private fork workflow precondition."""
    requests = []

    class PolicyEvidence:
        """Keep unsafe repository policy separate from any runner inventory."""

        def get(self, path):
            """Refuse capacity lookup when server policy is missing or permits fork workflow execution."""
            requests.append(path)
            policy = private_runner_policy(path)
            assert policy is not None, "Unsafe policy reached runner inventory"
            if path == "repos/owner/planner":
                if fault == "public":
                    policy["private"] = False
                if fault == "foreign":
                    policy["full_name"] = "another/repository"
            elif fault == "forks_enabled":
                policy["run_workflows_from_fork_pull_requests"] = True
            elif fault == "unknown_policy":
                return {}
            return policy

    assert available_runner_labels(
        REPOSITORY, ["self-hosted", "X64", "oep-ci-local"], PolicyEvidence()
    ) == ["ubuntu-24.04"]
    assert len(requests) == 2


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
            f"repos/owner/planner/commits/{HEAD}/check-runs?filter=latest&per_page=100&page="
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


def test_reviewed_candidate_is_bound_to_current_head_twice(candidate):
    """A label admits exactly the independently checked head after both current PR reads agree."""
    event, current, checks = candidate
    client = Evidence(current, checks)
    assert check(event, REPOSITORY, client) == "2" * 40
    assert client.pr_reads == 2


@pytest.mark.parametrize("check_index", [0, 1, 2])
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


@pytest.mark.parametrize("check_index", [0, 1, 2])
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
    checks.extend({"id": number, "name": "Other", "head_sha": HEAD} for number in range(4, 101))
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
    checks.append({**checks[check_index], "id": 4, "conclusion": "skipped"})
    assert check(event, REPOSITORY, Evidence(current, checks)) == HEAD


def test_dependabot_keeps_existing_review_exception_and_other_checks(candidate):
    """Dependabot needs both quick checks and verified commits, while CodeRabbit remains exempt."""
    event, current, checks = candidate
    current["user"]["login"] = "dependabot[bot]"
    checks.pop()
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
