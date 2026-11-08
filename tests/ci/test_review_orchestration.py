"""Exercise authorized review orchestration, durable recovery and provider isolation without billing."""

import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.ci import review_orchestration as review
from script.ci.check_commit_signatures import VerificationError

REPOSITORY = "patrickvorgers/open-energy-planner"
HEAD = "2" * 40
BASE = "1" * 40
NOW = datetime(2030, 1, 2, 12, tzinfo=UTC)
AVAILABLE = (
    "<!-- This is an auto-generated reply by CodeRabbit -->\n"
    "Your [plan](https://docs.coderabbit.ai/management/plans) includes PR reviews "
    "subject to [rate limits](https://docs.coderabbit.ai/management/plans#rate-limits). "
    "Reviews are available now."
)
LIMITED = (
    "<!-- This is an auto-generated reply by CodeRabbit -->\n"
    "You're currently rate limited under our [Fair Usage Limits Policy]"
    "(https://docs.coderabbit.ai/management/rate-limits#fair-usage-limits-policy). "
    "Your current included review allowance is based on your included PR review "
    "attempts over the past 7 days. Your next review will be available in 30 minutes."
)


class UserPublisher:
    """Expose only user identity and provider-comment publication through the second credential."""

    def __init__(self, owner):
        """Share durable comments while preserving a distinct authenticated user account."""
        self.owner = owner
        self.user = {"type": "User", "login": "review-publisher", "id": 4242}

    def get(self, path):
        """Return the account attached to this token without granting metadata-client access."""
        assert path == "user"
        return deepcopy(self.user)

    def post(self, path, payload):
        """Permit only a provider command and retain its real user author after uncertain writes."""
        assert payload["query"] == review.ADD_COMMENT
        assert payload["variables"]["body"].startswith(("@coderabbitai", "@kody"))
        before = len(self.owner.comments)
        try:
            return self.owner.post(path, payload)
        finally:
            for comment in self.owner.comments[before:]:
                comment["user"] = deepcopy(self.user)
                comment["author_association"] = "COLLABORATOR"


@pytest.fixture(autouse=True)
def configured_publisher_token(monkeypatch):
    """Supply a synthetic second credential without reading host credentials in entrypoint tests."""
    monkeypatch.setenv("OEP_REVIEW_COMMAND_TOKEN", "synthetic-command-token")


class GitHubEvidence:
    """Emulate bounded GitHub reads and durable comment mutations using independent wire fixtures."""

    def __init__(self):
        """Create an eligible Draft and an explicit maintainer command, without any provider work."""
        self.now = NOW
        self.comments = []
        self.publisher = UserPublisher(self)
        self.permission = "write"
        self.main = BASE
        self.unresolved = False
        self.lose_response = False
        self.fail_before_send = False
        self.mutate_after_intent = False
        self.calls = []
        self.current = {
            "number": 12,
            "node_id": "PR_12",
            "state": "open",
            "draft": True,
            "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}},
            "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPOSITORY}},
            "user": {"login": "author", "type": "User", "id": 1001},
            "changed_files": 1,
            "commits": 1,
        }
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
        self.command = self.comment("/oep-reviewer review", "developer", "User")

    def comment(self, body, login="github-actions[bot]", kind="Bot"):
        """Persist one uniquely identified synthetic comment, including its immutable author metadata."""
        number = 100 + len(self.comments)
        row = {
            "id": number,
            "node_id": f"COMMENT_{number}",
            "body": body,
            "created_at": self.now.isoformat(),
            "user": {
                "login": login,
                "type": kind,
                "id": 41898282 if login == "github-actions[bot]" else 99,
            },
            "author_association": "OWNER" if kind == "User" else "NONE",
        }
        self.comments.append(row)
        return row

    def get(self, path):
        """Serve independently specified identities, ancestry and exact-head check pages."""
        self.calls.append(("GET", path))
        if path.endswith("/permission"):
            return {"permission": "write" if "/review-publisher/" in path else self.permission}
        if path.endswith(f"/pulls/{self.current['number']}"):
            return deepcopy(self.current)
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": self.main}}
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": BASE}, "status": "ahead"}
        assert f"/commits/{self.current['head']['sha']}/check-runs?" in path
        return {"check_runs": deepcopy(self.checks), "total_count": len(self.checks)}

    def get_list(self, path):
        """Return real bounded metadata lists rather than trusting advertised counts."""
        self.calls.append(("GET", path))
        if "/comments?" in path:
            page = int(path.rsplit("=", 1)[1])
            return deepcopy(self.comments[(page - 1) * 100 : page * 100])
        if "/files?" in path:
            return [{"filename": "docs/development/example.md"}]
        if "/commits?" in path:
            return [{"sha": self.current["head"]["sha"]}]
        assert "/pulls?state=open" in path
        return [deepcopy(self.current)]

    def post(self, path, payload):
        """Apply only known comment mutations and the existing read-only discussion query."""
        assert path == "graphql"
        self.calls.append(("POST", payload["query"]))
        variables = payload["variables"]
        if "updateIssueComment" in payload["query"]:
            row = next(row for row in self.comments if row["node_id"] == variables["id"])
            row["body"] = variables["body"]
            return {
                "data": {
                    "updateIssueComment": {
                        "issueComment": {"id": row["node_id"], "body": row["body"]}
                    }
                }
            }
        if "addComment" in payload["query"]:
            body = variables["body"]
            is_command = body.startswith("@")
            if is_command and self.fail_before_send:
                raise VerificationError("Synthetic uncertain publication")
            row = self.comment(body)
            if body.startswith("<!-- oep-review-state:") and self.mutate_after_intent:
                self.current["head"]["sha"] = "3" * 40
            if is_command and self.lose_response:
                raise VerificationError("Synthetic response loss after durable write")
            return {
                "data": {
                    "addComment": {
                        "commentEdge": {
                            "node": {
                                "id": row["node_id"],
                                "databaseId": row["id"],
                                "body": body,
                                "createdAt": row["created_at"],
                            }
                        }
                    }
                }
            }
        assert "reviewThreads" in payload["query"]
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

    def run(self, explicit=False, minutes=0):
        """Execute the real controller against persisted metadata after an optional elapsed interval."""
        self.now = NOW + timedelta(minutes=minutes)
        review.process_pr(
            REPOSITORY,
            12,
            self,
            self.now,
            self.command["id"] if explicit else None,
            self.publisher,
        )

    def state(self):
        """Decode durable state after rereading comments, as a restarted worker would."""
        return review.read_state(deepcopy(self.comments))

    def provider_commands(self):
        """Collect published provider commands independently of internal counters or phases."""
        return [
            row["body"].splitlines()[0]
            for row in self.comments
            if row["body"].startswith(("@coderabbitai", "@kody"))
        ]

    def reply(self, body):
        """Publish a genuine provider-identity response after the most recent controller command."""
        return self.comment(body, "coderabbitai[bot]")

    def kody_result(self, conclusion="success", title="Code Review Complete"):
        """Supply a new exact-head provider check with independently specified completion text."""
        self.checks.append(
            {
                "id": 20,
                "name": "Kody Code Review",
                "head_sha": HEAD,
                "app": {"id": 413034},
                "status": "completed",
                "conclusion": conclusion,
                "output": {
                    "title": title,
                    "summary": "Review finished successfully. Suggestions (if any) were posted as PR/file comments.",
                },
            }
        )


def test_no_explicit_command_means_no_provider_request():
    """Periodic resumes cannot start a review merely because an eligible PR exists."""
    client = GitHubEvidence()
    client.comments.clear()
    client.run()
    assert client.provider_commands() == []
    assert client.state() is None


def test_available_coderabbit_is_preferred_and_duplicate_command_is_idempotent():
    """Fresh included capacity selects one CodeRabbit request despite repeated event delivery."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.run()
    client.run(explicit=True)
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]
    assert client.state()["phase"] == "coderabbit"


def test_confirmed_quota_selects_kody_and_findings_do_not_trigger_a_fallback():
    """Successful Kody review with unresolved findings completes orchestration but cannot dispose of findings."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(LIMITED)
    client.run()
    client.kody_result()
    client.unresolved = True
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit", "@kody start-review"]
    assert client.state()["phase"] == "complete"
    assert client.unresolved
    assert not any("convertPullRequestToDraft" in str(call) for call in client.calls)


def test_kody_technical_failure_waits_then_queries_fresh_coderabbit_capacity():
    """The controller waits for the reported deadline before retrying availability, never sleeping."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(LIMITED)
    client.run()
    client.kody_result("failure", "Code Review Failed")
    client.run(minutes=5)
    assert client.state()["phase"] == "waiting"
    client.run(minutes=29)
    assert len(client.provider_commands()) == 2
    client.run(minutes=30)
    client.reply(AVAILABLE)
    client.run(minutes=30)
    assert client.provider_commands() == [
        "@coderabbitai rate limit",
        "@kody start-review",
        "@coderabbitai rate limit",
        "@coderabbitai review",
    ]
    assert client.state()["phase"] == "coderabbit"


@pytest.mark.parametrize(
    "answer,initial_phase",
    [
        ("<!-- This is an auto-generated reply by CodeRabbit -->\nService unavailable", "quota"),
        (AVAILABLE + LIMITED, "blocked"),
        (
            "<!-- This is an auto-generated reply by CodeRabbit -->\nQuota availability unknown.",
            "blocked",
        ),
    ],
)
def test_unknown_or_contradictory_availability_never_infers_quota(answer, initial_phase):
    """Unrecognized and contradictory status replies block without invoking Kody or buying credits."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(answer)
    client.run()
    assert client.state()["phase"] == initial_phase
    client.run(minutes=16)
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "blocked"


@pytest.mark.parametrize("permission", ["read", "triage", "none", None])
def test_unauthorized_request_cannot_publish_provider_commands(permission):
    """A commenter who neither owns this PR nor has write authority cannot request its review."""
    client = GitHubEvidence()
    client.permission = permission
    with pytest.raises(VerificationError, match="permission"):
        client.run(explicit=True)
    assert client.provider_commands() == []
    assert client.state() is None


def test_fake_state_and_fake_provider_identity_are_ignored():
    """A contributor cannot spoof a durable bot state or a fresh availability response."""
    client = GitHubEvidence()
    client.comment('<!-- oep-review-state:{"phase":"complete"} -->\n', "developer", "User")
    client.run(explicit=True)
    client.comment(AVAILABLE, "coderabbitai[bot]", "User")
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "quota"


@pytest.mark.parametrize("mutation", ["head", "base", "main", "closed", "ready"])
def test_revision_or_draft_change_supersedes_waiting_work(mutation):
    """A resume never requests a provider for an obsolete head, base or closed/ready PR."""
    client = GitHubEvidence()
    client.run(explicit=True)
    if mutation in {"head", "base"}:
        client.current[mutation]["sha"] = "3" * 40
    elif mutation == "main":
        client.main = "3" * 40
    elif mutation == "closed":
        client.current["state"] = "closed"
    else:
        client.current["draft"] = False
    client.reply(AVAILABLE)
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "superseded"


def test_response_loss_recovers_published_command_without_resending():
    """A restart recovers the durable outbox command when publication succeeded but its response was lost."""
    client = GitHubEvidence()
    client.lose_response = True
    with pytest.raises(VerificationError):
        client.run(explicit=True)
    assert client.state()["phase"] == "sending"
    client.lose_response = False
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "quota"


def test_uncertain_publication_without_command_blocks_instead_of_retrying():
    """A missing command after an interrupted send is uncertain, never permission for duplicate publication."""
    client = GitHubEvidence()
    client.fail_before_send = True
    with pytest.raises(VerificationError):
        client.run(explicit=True)
    client.fail_before_send = False
    client.run()
    assert client.provider_commands() == []
    assert client.state()["phase"] == "blocked"


def test_identity_is_rechecked_after_intent_before_publication():
    """A push arriving during durable-intent publication prevents the provider command."""
    client = GitHubEvidence()
    client.mutate_after_intent = True
    with pytest.raises(VerificationError):
        client.run(explicit=True)
    assert client.provider_commands() == []


@pytest.mark.parametrize("check", ["Quick checks", "GitHub Verified PR commits", "CodeQL"])
def test_failed_cheap_gate_prevents_review_request(check):
    """All original prerequisite gates remain necessary before spending review capacity."""
    client = GitHubEvidence()
    next(row for row in client.checks if row["name"] == check)["conclusion"] = "failure"
    with pytest.raises(VerificationError):
        client.run(explicit=True)
    assert client.provider_commands() == []


def test_unanswered_quota_query_blocks_without_interpreting_outage_as_quota():
    """A missing reply times out without fallback to another reviewer."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.run(minutes=16)
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "blocked"


def test_kody_content_failure_or_unknown_result_does_not_trigger_technical_fallback():
    """Only explicit technical failure can activate the delayed CodeRabbit route."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(LIMITED)
    client.run()
    client.kody_result("failure", "Code Review Complete")
    client.run()
    client.run(minutes=60)
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit", "@kody start-review"]


def test_durable_state_round_trip_contains_no_raw_provider_body():
    """Saved state retains decisions and identities without copying untrusted provider responses."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(LIMITED)
    client.run()
    state = client.state()
    assert state["resume"] == "2030-01-02T12:30:00+00:00"
    assert "Fair Usage" not in json.dumps(state)
    assert state["head"] == HEAD
    assert state["base"] == BASE


def test_periodic_resume_recovers_an_explicit_command_whose_actions_event_was_replaced():
    """A lost queued event does not lose the manually posted request stored in PR comments."""
    client = GitHubEvidence()
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_fixed_prerequisite_requires_a_new_explicit_command():
    """A blocked command does not turn a later successful check into an automatic review trigger."""
    client = GitHubEvidence()
    client.checks[0]["conclusion"] = "failure"
    with pytest.raises(VerificationError):
        client.run(explicit=True)
    client.checks[0]["conclusion"] = "success"
    client.run()
    assert client.provider_commands() == []
    client.command = client.comment("/oep-reviewer review", "developer", "User")
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_replayed_old_command_cannot_review_a_newer_head():
    """An earlier explicit event cannot authorize a changed revision after acceptance."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.current["head"]["sha"] = "3" * 40
    client.run(explicit=True)
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_quota_can_disappear_between_query_and_actual_review():
    """A confirmed limit on the actual CodeRabbit command selects Kody without repeating CodeRabbit."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.run()
    client.reply(LIMITED)
    client.run()
    assert client.provider_commands() == [
        "@coderabbitai rate limit",
        "@coderabbitai review",
        "@kody start-review",
    ]


def test_stale_availability_does_not_authorize_another_provider():
    """A delayed worker does not treat an old availability report as fresh capacity evidence."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.run(minutes=20)
    assert client.state()["phase"] == "quota"
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai rate limit"]


def test_kody_skip_is_not_a_technical_failure():
    """Skipped review evidence blocks rather than triggering a competing reviewer."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(LIMITED)
    client.run()
    client.kody_result("skipped", "Code Review Skipped")
    client.checks[-1]["output"]["summary"] = (
        "No New Commits (No changes detected since last review)"
    )
    client.run()
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit", "@kody start-review"]


def test_no_fallback_while_a_rate_limited_provider_still_reports_running():
    """A quota comment cannot start Kody while CodeRabbit's actual check remains active."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.run()
    client.checks.append(
        {
            "id": 20,
            "name": "CodeRabbit",
            "head_sha": HEAD,
            "app": {"id": 347564},
            "status": "in_progress",
            "conclusion": None,
        }
    )
    client.reply(LIMITED)
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]
    client.checks[-1].update(status="completed", conclusion="skipped")
    client.run()
    assert client.provider_commands()[-1] == "@kody start-review"


def test_bounded_quota_rechecks_never_repeat_kody():
    """Repeated genuine quota limits terminate after eight queries without looping between reviewers."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(LIMITED)
    client.run()
    client.kody_result("failure", "Code Review Failed")
    client.run()
    for attempt in range(1, 8):
        client.run(minutes=attempt * 30)
        client.reply(LIMITED)
        client.run(minutes=attempt * 30)
    client.run(minutes=240)
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands().count("@coderabbitai rate limit") == 8
    assert client.provider_commands().count("@kody start-review") == 1


@pytest.mark.parametrize(
    "event_name,body,permission,routed,requested",
    [
        ("schedule", "/oep-reviewer review", "write", True, True),
        ("workflow_dispatch", "/oep-reviewer review", "write", True, True),
        ("check_run", "/oep-reviewer review", "write", True, True),
        ("issue_comment", "/oep-reviewer review", "write", True, True),
        ("issue_comment", "/oep-reviewer review\n", "write", True, True),
        ("issue_comment", "/oep-reviewer review\r\n", "write", True, True),
        ("issue_comment", " \t\n/oep-reviewer review\r\n \t", "write", True, True),
        ("issue_comment", "Please /oep-reviewer review", "write", True, False),
        ("issue_comment", "/oep-reviewer review extra", "write", True, False),
        ("issue_comment", "ordinary discussion", "write", False, False),
        ("issue_comment", "/oep-reviewer review\r\n", "read", True, False),
    ],
)
def test_real_entrypoint_runs_only_trusted_main_metadata(
    tmp_path, monkeypatch, event_name, body, permission, routed, requested
):
    """The actual job filter routes whitespace commands to exact authorized, deduplicated parsing."""
    client = GitHubEvidence()
    client.command["body"] = body
    client.permission = permission
    event = {
        "repository": {"full_name": REPOSITORY, "default_branch": "main"},
        "action": "created",
        "issue": {"number": 12, "pull_request": {"url": "synthetic"}},
        "comment": client.command,
        "check_run": {"app": {"id": 347564}},
    }
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-not-a-token")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setattr(
        review,
        "GitHub",
        lambda token: client.publisher if token == "synthetic-command-token" else client,
    )
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/review-orchestration.yaml"
    condition = re.search(r"    if: >-\n(.*?)    runs-on:", workflow.read_text(), re.DOTALL)[1]
    # Evaluate this fixed boolean subset with GitHub's case-insensitive string contains semantics.
    expression = " ".join(condition.split()).replace("&&", "and").replace("||", "or")
    context = json.loads(
        json.dumps(
            {
                "ref": "refs/heads/main",
                "repository": REPOSITORY,
                "event_name": event_name,
                "event": event,
            }
        ),
        object_hook=lambda fields: SimpleNamespace(**fields),
    )
    admitted = bool(
        eval(
            expression,
            {"__builtins__": {}},
            {
                "github": context,
                "contains": lambda body, command: command.casefold() in body.casefold(),
            },
        )
    )
    assert admitted is routed
    expected_commands = ["@coderabbitai rate limit"] if requested else []
    if admitted:
        assert review.main() == (1 if permission == "read" else 0)
        assert client.provider_commands() == expected_commands
        assert review.main() == (1 if permission == "read" else 0)
    assert client.provider_commands() == expected_commands
    monkeypatch.setenv("GITHUB_REF", "refs/heads/contributor")
    assert review.main() == 1
    assert client.provider_commands() == expected_commands


def test_manual_preflight_also_rejects_a_controller_authored_same_head_review():
    """The direct recovery route cannot duplicate a review already dispatched by the bot."""
    client = GitHubEvidence()
    client.comment(f"@coderabbitai review\n<!-- oep-coderabbit-head:{HEAD} -->")
    with pytest.raises(VerificationError, match="already exists"):
        review.preflight(REPOSITORY, 12, HEAD, BASE, client)


def test_automatic_disabled_kody_metadata_does_not_block_an_explicit_review():
    """The genuine automatic-disabled Kody skip on a Draft is not existing review work."""
    client = GitHubEvidence()
    client.kody_result("skipped", "Code Review Skipped")
    client.checks[-1]["output"]["summary"] = (
        "Automated Review is disabled — Enable 'Automated Code Review' in General Settings"
    )
    client.run(explicit=True)
    assert client.provider_commands() == ["@coderabbitai rate limit"]


@pytest.mark.parametrize("availability", [AVAILABLE, LIMITED])
def test_merge_only_skip_allows_quota_then_one_appropriate_review(availability):
    """Merge-only metadata prefers available CodeRabbit or permits one forced Kody fallback."""
    client = GitHubEvidence()
    client.kody_result("skipped", "Code Review Skipped")
    client.checks[-1]["output"]["summary"] = (
        "Only Merge Commits (All new commits identified as merge commits)"
    )
    client.run(explicit=True)
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    client.reply(availability)
    client.run()
    expected = "@coderabbitai review" if availability == AVAILABLE else "@kody review --force"
    assert client.provider_commands() == ["@coderabbitai rate limit", expected]
    client.run(explicit=True)
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit", expected]
    if availability == LIMITED:
        client.kody_result()
        client.checks[-1]["id"] = 21
        client.run()
        assert client.state()["phase"] == "complete"


@pytest.mark.parametrize(
    "mutation", ["unknown", "pending", "failed", "wrong-head", "wrong-app", "wrong-name"]
)
def test_merge_only_force_requires_concrete_current_provider_evidence(mutation):
    """Similar text from incomplete, failed or unrelated checks cannot justify forced review."""
    client = GitHubEvidence()
    client.kody_result("skipped", "Code Review Skipped")
    check = client.checks[-1]
    check["output"]["summary"] = "Only Merge Commits (All new commits identified as merge commits)"
    if mutation == "unknown":
        check["output"]["summary"] += " unknown"
    elif mutation == "pending":
        check["status"] = "in_progress"
    elif mutation == "failed":
        check["conclusion"] = "failure"
    elif mutation == "wrong-head":
        check["head_sha"] = "3" * 40
    elif mutation == "wrong-app":
        check["app"]["id"] = 999
    else:
        check["name"] = "Unrelated check"
    if mutation == "wrong-app":
        client.run(explicit=True)
        client.reply(LIMITED)
        client.run()
        assert client.provider_commands() == ["@coderabbitai rate limit", "@kody start-review"]
    else:
        with pytest.raises(VerificationError):
            client.run(explicit=True)
        assert client.provider_commands() == []


@pytest.mark.parametrize(
    "change", ["before-quota-reply", "before-publication", "before-publication-valid-command"]
)
def test_merge_only_force_rechecks_provider_evidence_before_sending(monkeypatch, change):
    """Changed Kody evidence, including a different valid command, blocks stale force publication."""
    client = GitHubEvidence()
    client.kody_result("skipped", "Code Review Skipped")
    client.checks[-1]["output"]["summary"] = (
        "Only Merge Commits (All new commits identified as merge commits)"
    )
    client.run(explicit=True)
    client.reply(LIMITED)
    if change == "before-quota-reply":
        client.checks[-1]["output"]["summary"] = "Unknown provider state"
        client.run()
        assert client.state()["phase"] == "blocked"
    else:
        original_post = client.post

        def change_after_intent(path, payload):
            """Remove force evidence after persisting the one-shot publication intent."""
            result = original_post(path, payload)
            if client.state()["phase"] == "sending" and client.state()["target"] == "kody":
                client.checks[-1]["output"]["summary"] = (
                    "Automated Review is disabled — Enable 'Automated Code Review' in General Settings"
                    if change == "before-publication-valid-command"
                    else "Unknown provider state"
                )
            return result

        monkeypatch.setattr(client, "post", change_after_intent)
        with pytest.raises(VerificationError):
            client.run()
        assert client.state()["phase"] == "sending"
        client.run()
        assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_external_force_command_prevents_a_second_fallback():
    """A participant's forced Kody request is ambiguous existing work rather than fallback capacity."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(LIMITED)
    client.comment("@kody review --force", "developer", "User")
    client.run()
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit", "@kody review --force"]


def test_manual_kody_request_blocks_before_its_check_is_created():
    """A genuine same-head Kody command is existing work even before a provider check appears."""
    client = GitHubEvidence()
    client.comment(f"@kody start-review\n<!-- oep-kody-head:{HEAD} -->", "developer", "User")
    with pytest.raises(VerificationError, match="already exists"):
        client.run(explicit=True)
    assert client.provider_commands() == ["@kody start-review"]


@pytest.mark.parametrize("site", ["preflight", "advance"])
def test_imported_failure_values_never_enter_public_status(monkeypatch, site):
    """Neither failure boundary may publish arbitrary exception text in its public PR comment."""
    client = GitHubEvidence()
    if site == "advance":
        client.run(explicit=True)

    def private_failure(*args, **kwargs):
        """Inject a private sentinel where an imported verification dependency can fail."""
        raise VerificationError("PRIVATE-REVIEW-SENTINEL-2030")

    monkeypatch.setattr(review, site, private_failure)
    try:
        client.run(explicit=site == "preflight")
    except VerificationError:
        pass
    assert client.state()["phase"] == "blocked"
    assert "PRIVATE-REVIEW-SENTINEL-2030" not in json.dumps(client.comments)
    assert "Inspect" in client.state()["message"]


@pytest.mark.parametrize(
    "failure",
    [
        "command-response",
        "confirmation-before-write",
        "confirmation-response",
        "command-null-data",
        "confirmation-null-data",
    ],
)
def test_advance_publication_failure_recovers_without_blocking_or_resending(monkeypatch, failure):
    """A resumed send preserves its outbox when the command or confirmation response is lost."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    original_post = client.post

    def interrupted_post(path, payload):
        """Fail at a specific real mutation boundary before or after its durable write."""
        body = payload["variables"].get("body", "")
        confirming = "updateIssueComment" in payload["query"] and '"phase": "coderabbit"' in body
        if confirming and failure == "confirmation-before-write":
            raise VerificationError("PRIVATE-REVIEW-PUBLICATION-SENTINEL")
        response = original_post(path, payload)
        if (failure == "command-null-data" and body.startswith("@coderabbitai review")) or (
            confirming and failure == "confirmation-null-data"
        ):
            return {"data": None, "errors": [{"message": "PRIVATE-NULL-GRAPHQL-SENTINEL"}]}
        if (failure == "command-response" and body.startswith("@coderabbitai review")) or (
            confirming and failure == "confirmation-response"
        ):
            raise VerificationError("PRIVATE-REVIEW-PUBLICATION-SENTINEL")
        return response

    monkeypatch.setattr(client, "post", interrupted_post)
    try:
        client.run()
    except VerificationError:
        pass
    assert client.state()["phase"] in {"sending", "coderabbit"}
    monkeypatch.setattr(client, "post", original_post)
    client.run()
    assert client.state()["phase"] == "coderabbit"
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]
    assert "PRIVATE-REVIEW-PUBLICATION-SENTINEL" not in json.dumps(client.comments)


@pytest.mark.parametrize("bad_evidence", ["malformed", "overlimit", "unavailable"])
def test_sweep_records_failure_but_still_processes_the_next_pr(tmp_path, monkeypatch, bad_evidence):
    """One invalid PR cannot starve a different eligible request or turn the overall sweep green."""
    client = GitHubEvidence()
    original_get_list = client.get_list

    def multiple_prs(path):
        """Place a broken PR before the valid independent request in the real sweep inventory."""
        if "/pulls?state=open" in path:
            return [{"number": 11}, deepcopy(client.current)]
        if "/issues/11/comments?" in path:
            if bad_evidence == "unavailable":
                raise VerificationError("PRIVATE-SWEEP-SENTINEL")
            if bad_evidence == "overlimit":
                page = int(path.rsplit("=", 1)[1])
                return [
                    {"id": page * 100 + index, "body": "Ordinary comment"} for index in range(100)
                ]
            return [
                {
                    "id": 99,
                    "node_id": "bad",
                    "body": "<!-- oep-review-state:not-json -->\n",
                    "user": {"login": "github-actions[bot]", "type": "Bot", "id": 41898282},
                }
            ]
        return original_get_list(path)

    monkeypatch.setattr(client, "get_list", multiple_prs)
    event_file = tmp_path / "sweep.json"
    event_file.write_text(
        json.dumps({"repository": {"full_name": REPOSITORY, "default_branch": "main"}}),
        encoding="utf-8",
    )
    for key, value in {
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_TOKEN": "synthetic-not-a-token",
        "GITHUB_EVENT_NAME": "schedule",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        review,
        "GitHub",
        lambda token: client.publisher if token == "synthetic-command-token" else client,
    )
    assert review.main() == 1
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_consumed_duplicate_cannot_start_a_review_after_a_later_push():
    """A duplicate command acknowledged on one revision is not a fresh request for a later revision."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.command = client.comment("/oep-reviewer review", "developer", "User")
    client.run(explicit=True)
    changed_head = "3" * 40
    client.current["head"]["sha"] = changed_head
    for check in client.checks:
        check["head_sha"] = changed_head
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "superseded"
    client.command = client.comment("/oep-reviewer review", "developer", "User")
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai rate limit"]
    assert client.state()["head"] == changed_head


def test_missing_quota_retry_time_blocks_after_kody_technical_failure():
    """A confirmed limit without a retry interval cannot invent a delayed CodeRabbit deadline."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(LIMITED.replace(" Your next review will be available in 30 minutes.", ""))
    client.run()
    client.kody_result("failure", "Code Review Failed")
    client.run(minutes=5)
    client.run(minutes=60)
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit", "@kody start-review"]


@pytest.mark.parametrize("failure", ["unavailable", "oversized"])
def test_global_inventory_failure_still_fails_closed(tmp_path, monkeypatch, capsys, failure):
    """Isolating per-PR errors does not hide a failed global inventory or process an invalid page."""
    client = GitHubEvidence()
    original_get_list = client.get_list

    def invalid_inventory(path):
        """Return an invalid global page or fail its API request with a private sentinel."""
        if "/pulls?state=open" in path:
            if failure == "unavailable":
                raise VerificationError("PRIVATE-INVENTORY-SENTINEL")
            return [deepcopy(client.current) for _ in range(101)]
        return original_get_list(path)

    monkeypatch.setattr(client, "get_list", invalid_inventory)
    event_file = tmp_path / "inventory.json"
    event_file.write_text(
        json.dumps({"repository": {"full_name": REPOSITORY, "default_branch": "main"}}),
        encoding="utf-8",
    )
    for key, value in {
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_TOKEN": "synthetic-not-a-token",
        "GITHUB_EVENT_NAME": "schedule",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        review,
        "GitHub",
        lambda token: client.publisher if token == "synthetic-command-token" else client,
    )
    assert review.main() == 1
    assert client.provider_commands() == []
    assert "PRIVATE-INVENTORY-SENTINEL" not in capsys.readouterr().out


def test_duplicate_command_consumption_requires_current_authority():
    """Revoked write access cannot acknowledge a command into the durable inbox watermark."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.command = client.comment("/oep-reviewer review", "developer", "User")
    client.permission = "read"
    with pytest.raises(VerificationError, match="permission"):
        client.run(explicit=True)
    assert client.state()["consumed"] < client.command["id"]
    assert client.provider_commands() == ["@coderabbitai rate limit"]


@pytest.mark.parametrize("answer", [AVAILABLE, LIMITED])
def test_delayed_quota_reply_requests_fresh_capacity_before_any_review(answer):
    """A delayed scheduler rechecks old genuine quota instead of blocking or spending stale capacity."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(answer)
    client.run(minutes=20)
    assert client.state()["phase"] == "quota"
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai rate limit"]
    client.reply(AVAILABLE)
    client.run(minutes=20)
    assert client.provider_commands()[-1] == "@coderabbitai review"


def test_repeated_stale_quota_rechecks_keep_the_original_query_budget():
    """Repeated scheduler delays cannot create an unlimited sequence of availability requests."""
    client = GitHubEvidence()
    client.run(explicit=True)
    for attempt in range(1, 9):
        client.reply(AVAILABLE)
        client.run(minutes=attempt * 20)
    assert client.provider_commands() == ["@coderabbitai rate limit"] * 8
    assert client.state()["phase"] == "blocked"


def test_stale_quota_does_not_requery_while_a_provider_is_active():
    """Delayed evidence cannot start another command while a genuine provider check remains active."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.checks.append(
        {
            "id": 20,
            "name": "CodeRabbit",
            "head_sha": HEAD,
            "app": {"id": 347564},
            "status": "in_progress",
            "conclusion": None,
        }
    )
    client.run(minutes=20)
    assert client.state()["phase"] == "quota"
    assert client.provider_commands() == ["@coderabbitai rate limit"]


@pytest.mark.parametrize("with_actual_limit", [False, True])
def test_review_acknowledgement_does_not_count_as_quota_evidence(with_actual_limit):
    """A generic review acknowledgement neither expires a long review nor makes real quota ambiguous."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.run()
    client.reply(
        "<!-- This is an auto-generated reply by CodeRabbit -->\nReview triggered. Actions performed: Incremental review."
    )
    if with_actual_limit:
        client.reply(LIMITED)
        client.run()
        assert client.state()["phase"] == "kody"
        assert client.provider_commands()[-1] == "@kody start-review"
    else:
        client.checks.append(
            {
                "id": 20,
                "name": "CodeRabbit",
                "head_sha": HEAD,
                "app": {"id": 347564},
                "status": "in_progress",
                "conclusion": None,
            }
        )
        client.run(minutes=20)
        assert client.state()["phase"] == "coderabbit"
        assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]


@pytest.mark.parametrize(
    "mutation,path",
    [
        ("add", ("data",)),
        ("add", ("data", "addComment")),
        ("add", ("data", "addComment", "commentEdge")),
        ("add", ("data", "addComment", "commentEdge", "node")),
        ("update", ("data",)),
        ("update", ("data", "updateIssueComment")),
        ("update", ("data", "updateIssueComment", "issueComment")),
    ],
)
@pytest.mark.parametrize("with_errors", [True, False])
def test_nullable_mutation_payload_uses_canonical_verification_failure(
    monkeypatch, mutation, path, with_errors
):
    """Every nullable GraphQL mutation level fails through VerificationError rather than AttributeError."""
    client = GitHubEvidence()
    client.run(explicit=True)
    payload = {"errors": [{"message": "PRIVATE-NULL-GRAPHQL-SENTINEL"}]} if with_errors else {}
    parent = payload
    for field in path[:-1]:
        parent[field] = {}
        parent = parent[field]
    parent[path[-1]] = None
    monkeypatch.setattr(client, "post", lambda *args, **kwargs: payload)
    with pytest.raises(VerificationError) as captured:
        if mutation == "add":
            review.add_comment("PR_12", "Synthetic command", "synthetic-key", client)
        else:
            review.save_state(client.state(), "PR_12", client)
    assert "PRIVATE-NULL-GRAPHQL-SENTINEL" not in str(captured.value)


@pytest.mark.parametrize(
    "pending_user",
    ["revoked", "developer"],
    ids=["unrelated-denied-user", "original-requester-revoked"],
)
def test_denied_duplicate_and_original_requester_revocation_have_distinct_effects(
    monkeypatch, pending_user
):
    """Denied duplicates stay unconsumed, and an original writer's revoked authority blocks new sends."""
    client = GitHubEvidence()
    client.run(explicit=True)
    original_consumed = client.state()["consumed"]
    client.reply(AVAILABLE)
    client.comment("/oep-reviewer review", pending_user, "User")
    original_get = client.get

    def revoked_permission(path):
        """Simulate permission revoked after an earlier request or from a different participant."""
        if path.endswith(f"/collaborators/{pending_user}/permission"):
            return {"permission": "read"}
        return original_get(path)

    monkeypatch.setattr(client, "get", revoked_permission)
    client.run()
    assert client.state()["consumed"] == original_consumed
    if pending_user == "developer":
        assert client.state()["phase"] == "blocked"
        assert client.provider_commands() == ["@coderabbitai rate limit"]
    else:
        assert client.state()["phase"] == "coderabbit"
        assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]


@pytest.mark.parametrize("own_author", [False, True])
def test_repeated_denied_requests_have_bounded_scan_cost(monkeypatch, own_author):
    """Denied duplicates cost one lookup per scan while eligible active work still advances."""
    observed_counts = []
    for duplicate_count in (1, 100):
        client = GitHubEvidence()
        if own_author:
            client.current["user"] = deepcopy(client.command["user"])
            client.permission = "read"
        client.run(explicit=True)
        original_consumed = client.state()["consumed"]
        client.reply(AVAILABLE)
        for _ in range(duplicate_count):
            denied = client.comment("/oep-reviewer review", "denied", "User")
            denied["user"]["id"] = 2002
        original_get = client.get

        def denied_permission(path, original_get=original_get):
            """Record real API work while denying the unrelated repeated participant."""
            result = original_get(path)
            return {"permission": "read"} if "/denied/permission" in path else result

        monkeypatch.setattr(client, "get", denied_permission)
        client.calls.clear()
        client.run()
        denied_path = f"repos/{REPOSITORY}/collaborators/denied/permission"
        pull_path = f"repos/{REPOSITORY}/pulls/12"
        assert client.calls.count(("GET", denied_path)) == 1
        observed_counts.append(client.calls.count(("GET", pull_path)))
        assert client.state()["consumed"] == original_consumed
        assert client.state()["phase"] == "coderabbit"
        assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]

        client.calls.clear()
        client.run()
        assert client.calls.count(("GET", denied_path)) == 1
    assert observed_counts[0] == observed_counts[1]


def test_next_inbox_scan_reads_changed_permissions():
    """A denial from the previous scan cannot hide newly granted write authority."""
    client = GitHubEvidence()
    client.permission = "read"
    for _ in range(100):
        client.comment("/oep-reviewer review", "developer", "User")
    client.calls.clear()
    client.run()
    assert client.state() is None
    assert client.calls.count(("GET", f"repos/{REPOSITORY}/pulls/12")) == 1
    assert (
        client.calls.count(("GET", f"repos/{REPOSITORY}/collaborators/developer/permission")) == 1
    )
    client.permission = "write"
    client.run()
    assert client.state()["request"] == client.command["id"] + 100
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_discovery_permission_does_not_authorize_later_publication(monkeypatch):
    """Revocation after inbox discovery blocks the live publication authorization check."""
    client = GitHubEvidence()
    original_get = client.get
    permission_reads = 0

    def revoked_after_discovery(path):
        """Allow discovery once and deny every later requester authorization."""
        nonlocal permission_reads
        result = original_get(path)
        if "/developer/permission" in path:
            permission_reads += 1
            return {"permission": "write" if permission_reads == 1 else "read"}
        return result

    monkeypatch.setattr(client, "get", revoked_after_discovery)
    with pytest.raises(VerificationError):
        client.run()
    assert permission_reads == 2
    assert client.provider_commands() == []


def test_unauthorized_newest_command_does_not_hide_an_older_authorized_request(monkeypatch):
    """The pending inbox selects the newest authorized command instead of starving older valid work."""
    client = GitHubEvidence()
    original_request = client.command["id"]
    client.comment("/oep-reviewer review", "revoked", "User")
    original_get = client.get

    def revoked_permission(path):
        """Deny only the newer unauthorized participant while preserving the original writer."""
        if path.endswith("/collaborators/revoked/permission"):
            return {"permission": "read"}
        return original_get(path)

    monkeypatch.setattr(client, "get", revoked_permission)
    client.run()
    assert client.state()["request"] == client.state()["consumed"] == original_request
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_pending_permission_api_failure_does_not_advance_or_consume(monkeypatch):
    """Permission transport failure is not treated as a known authorization denial."""
    client = GitHubEvidence()
    client.run(explicit=True)
    original_consumed = client.state()["consumed"]
    client.reply(AVAILABLE)
    client.comment("/oep-reviewer review", "revoked", "User")
    original_get = client.get

    def unavailable_permission(path):
        """Fail the live permission lookup without granting or denying repository authority."""
        if path.endswith("/collaborators/revoked/permission"):
            raise VerificationError("PRIVATE-PERMISSION-TRANSPORT-SENTINEL")
        return original_get(path)

    monkeypatch.setattr(client, "get", unavailable_permission)
    with pytest.raises(VerificationError):
        client.run()
    assert client.state()["consumed"] == original_consumed
    assert client.provider_commands() == ["@coderabbitai rate limit"]


@pytest.mark.parametrize("mutation", ["add", "update", "null-author-recovery"])
def test_one_pr_metadata_failure_or_recovery_does_not_starve_the_next_pr(
    tmp_path, monkeypatch, capsys, mutation
):
    """A failed mutation or recovered null-author outbox cannot prevent the next PR from proceeding."""
    bad = GitHubEvidence()
    bad.current.update(number=11, node_id="PR_11")
    bad.current["head"]["sha"] = "4" * 40
    for check in bad.checks:
        check["head_sha"] = "4" * 40
    good = GitHubEvidence()
    if mutation == "null-author-recovery":
        bad.lose_response = True
        with pytest.raises(VerificationError):
            review.process_pr(REPOSITORY, 11, bad, NOW, bad.command["id"], bad.publisher)
        bad.lose_response = False
        bad.comment("A deleted account's ordinary comment")["user"] = None

    class RoutedEvidence:
        """Route actual controller metadata and mutations to two independent synthetic PRs."""

        def get(self, path):
            """Read the selected PR while sharing repository permission and main metadata."""
            selected = bad if "/pulls/11" in path or "4" * 40 in path else good
            return selected.get(path)

        def get_list(self, path):
            """Keep the broken PR first in the live inventory and preserve each comment history."""
            if "/pulls?state=open" in path:
                return [deepcopy(bad.current), deepcopy(good.current)]
            return (bad if "/pulls/11/" in path or "/issues/11/" in path else good).get_list(path)

        def post(self, path, payload):
            """Return nullable data after the selected first-PR mutation has actually been stored."""
            variables = payload["variables"]
            selected = (
                bad
                if variables.get("number") == 11
                or variables.get("subject") == "PR_11"
                or '"head": "' + "4" * 40 + '"' in variables.get("body", "")
                else good
            )
            response = selected.post(path, payload)
            if selected is bad and (
                (mutation == "add" and "addComment" in payload["query"])
                or (mutation == "update" and "updateIssueComment" in payload["query"])
            ):
                return {"data": None, "errors": [{"message": "PRIVATE-NULL-SWEEP-SENTINEL"}]}
            return response

    event_file = tmp_path / "null-sweep.json"
    event_file.write_text(
        json.dumps({"repository": {"full_name": REPOSITORY, "default_branch": "main"}}),
        encoding="utf-8",
    )
    for key, value in {
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_TOKEN": "synthetic-not-a-token",
        "GITHUB_EVENT_NAME": "schedule",
    }.items():
        monkeypatch.setenv(key, value)

    class RoutedPublisher:
        """Publish each command with the same user while retaining independent PR storage."""

        def get(self, path):
            """Read the shared user credential identity."""
            return good.publisher.get(path)

        def post(self, path, payload):
            """Dispatch provider commands to their fixed subject without permitting status writes."""
            selected = bad if payload["variables"]["subject"] == "PR_11" else good
            return selected.publisher.post(path, payload)

    monkeypatch.setattr(
        review,
        "GitHub",
        lambda token: RoutedPublisher() if token == "synthetic-command-token" else RoutedEvidence(),
    )
    if mutation == "null-author-recovery":
        assert review.main() == 0
        assert bad.provider_commands() == ["@coderabbitai rate limit"]
        assert bad.state()["phase"] == "quota"
    else:
        assert review.main() == 1
        assert bad.provider_commands() == []
    assert good.provider_commands() == ["@coderabbitai rate limit"]
    assert "PRIVATE-NULL-SWEEP-SENTINEL" not in capsys.readouterr().out


@pytest.mark.parametrize("quota", ["stale", "fresh-limit", "none"])
@pytest.mark.parametrize("minutes", [119, 120, 121])
def test_active_review_always_respects_original_completion_deadline(quota, minutes):
    """Quota early returns cannot extend an unfinished review beyond its two-hour completion budget."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.run()
    client.checks.append(
        {
            "id": 20,
            "name": "CodeRabbit",
            "head_sha": HEAD,
            "app": {"id": 347564},
            "status": "in_progress",
            "conclusion": None,
        }
    )
    if quota == "stale":
        client.reply(LIMITED)
    elif quota == "fresh-limit":
        client.now = NOW + timedelta(minutes=minutes)
        client.reply(LIMITED)
    client.run(minutes=minutes)
    assert client.state()["phase"] == ("blocked" if minutes > 120 else "coderabbit")
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]
    assert client.checks[-1]["status"] == "in_progress"
    if minutes > 120:
        assert "Inspect" in client.state()["message"]


@pytest.mark.parametrize("quota", ["stale", "fresh-limit", "none"])
def test_substantive_completion_has_priority_over_expired_review_budget(quota):
    """A genuinely completed current-head review remains accepted before deadline failure handling."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.run()
    if quota == "stale":
        client.reply(LIMITED)
    elif quota == "fresh-limit":
        client.now = NOW + timedelta(minutes=121)
        client.reply(LIMITED)
    client.checks.append(
        {
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
                    "| Prepare review scope | Completed | 1s |\n"
                    "| Prepare repository workspace | Completed | 1s |\n"
                    "| Run analysis checks | Completed | 1s |\n"
                    "| Generate review feedback | Completed | 1s |\n"
                    "| Publish review results | Completed | 1s |\n"
                ),
            },
        }
    )
    client.run(minutes=121)
    assert client.state()["phase"] == "complete"
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]


def test_reviewer_slash_command_starts_only_the_explicit_request():
    """The application slash command starts quota selection without mentioning a GitHub user."""
    client = GitHubEvidence()
    client.command["body"] = "/oep-reviewer review"
    client.run(explicit=True)
    assert client.provider_commands() == ["@coderabbitai rate limit"]


@pytest.mark.parametrize("body", ["@oep review", "/oep review", "@oep-reviewer review"])
def test_legacy_and_mention_commands_never_start_from_history(body):
    """Historical mentions and superseded names cannot create a request during a later sweep."""
    client = GitHubEvidence()
    client.command["body"] = body
    client.run()
    assert client.provider_commands() == []
    assert client.state() is None


def test_blocked_historical_request_stays_inert_after_command_rename():
    """Renaming the trigger never revives a blocked request or rewrites its original comment."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.run(minutes=16)
    assert client.state()["phase"] == "blocked"
    client.command["body"] = "@oep review"
    original_comments = deepcopy(client.comments)
    client.run(minutes=17)
    assert client.comments == original_comments
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_status_comment_identifies_oep_with_its_existing_logo():
    """The durable status presents the OEP logo and readable ownership without raw provider text."""
    client = GitHubEvidence()
    client.run(explicit=True)
    statuses = [row for row in client.comments if row["body"].startswith("<!-- oep-review-state:")]
    assert len(statuses) == 1
    heading = statuses[0]["body"].splitlines()[1]
    assert heading == (
        '## <img src="https://raw.githubusercontent.com/patrickvorgers/open-energy-planner/'
        "39f6ee5f559df8a5b493884fd3b5a4ce93394188/"
        'custom_components/open_energy_planner/brand/icon.png" '
        'alt="OEP logo" width="24" height="24"> OEP review'
    )
    assert "Status: Quota" in statuses[0]["body"]
    assert client.state()["phase"] == "quota"


def test_provider_commands_use_the_user_while_status_remains_bot_owned():
    """Separate credentials publish provider commands and durable controller status under distinct authors."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.run()
    commands = [row for row in client.comments if row["body"].startswith("@coderabbitai")]
    statuses = [row for row in client.comments if row["body"].startswith("<!-- oep-review-state:")]
    assert [row["body"].splitlines()[0] for row in commands] == [
        "@coderabbitai rate limit",
        "@coderabbitai review",
    ]
    assert all(row["user"]["type"] == "User" and row["user"]["id"] == 4242 for row in commands)
    assert all(row["user"]["type"] == "Bot" and row["user"]["id"] == 41898282 for row in statuses)
    assert client.state()["publisher_id"] == 4242


@pytest.mark.parametrize(
    "user",
    [
        {"type": "Bot", "login": "github-actions", "id": 41898282},
        {"type": "User", "login": "review-publisher", "id": True},
        {"type": "User", "login": "review-publisher", "id": 0},
        {"type": "User", "login": "invalid/name", "id": 4242},
    ],
)
def test_invalid_command_account_never_falls_back_to_the_workflow_bot(user):
    """A bot or malformed account blocks the request before any provider command is published."""
    client = GitHubEvidence()
    client.publisher.user = user
    with pytest.raises(review.CommandAccountError):
        client.run(explicit=True)
    assert client.provider_commands() == []
    assert client.state()["phase"] == "blocked"
    assert "OEP_REVIEW_COMMAND_TOKEN" in client.state()["message"]


@pytest.mark.parametrize("publisher", [None, "metadata"])
def test_missing_or_reused_metadata_credential_cannot_send_provider_commands(publisher):
    """An absent command credential never causes a fallback to the metadata credential."""
    client = GitHubEvidence()
    client.publisher = client if publisher == "metadata" else None
    with pytest.raises(review.CommandAccountError):
        client.run(explicit=True)
    assert client.provider_commands() == []
    assert client.state()["phase"] == "blocked"


def test_command_account_must_retain_live_repository_write_access():
    """A publisher without repository write authority cannot use an otherwise authorized request."""
    client = GitHubEvidence()
    original_get = client.get

    def permissions(path):
        """Retain requester authority while denying the separate publisher's repository access."""
        if path.endswith("/collaborators/review-publisher/permission"):
            return {"permission": "read"}
        return original_get(path)

    client.get = permissions
    with pytest.raises(review.CommandAccountError):
        client.run(explicit=True)
    assert client.provider_commands() == []


def test_rotating_to_another_user_cannot_continue_an_existing_request():
    """Changing the configured account blocks rather than switching sender during a durable request."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.publisher.user = {"type": "User", "login": "another-writer", "id": 5252}
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "blocked"
    assert "OEP_REVIEW_COMMAND_TOKEN" in client.state()["message"]


def test_failed_user_identity_read_hides_private_details_and_publishes_no_command():
    """Credential lookup failure produces owned recovery advice without copying private error values."""
    client = GitHubEvidence()

    def unavailable(path):
        """Simulate a failed credential lookup without allowing a publication attempt."""
        raise VerificationError("PRIVATE-PUBLISHER-SENTINEL")

    client.publisher.get = unavailable
    with pytest.raises(review.CommandAccountError) as error:
        client.run(explicit=True)
    assert "PRIVATE-PUBLISHER-SENTINEL" not in str(error.value)
    assert "PRIVATE-PUBLISHER-SENTINEL" not in json.dumps(client.comments)
    assert client.provider_commands() == []


def test_outbox_recovery_rejects_a_marker_from_another_user():
    """Only the authenticated command account can supply a lost-response publication receipt."""
    client = GitHubEvidence()
    client.lose_response = True
    with pytest.raises(VerificationError):
        client.run(explicit=True)
    command = next(row for row in client.comments if row["body"].startswith("@coderabbitai"))
    command["user"] = {"type": "User", "login": "another-writer", "id": 5252}
    client.lose_response = False
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "blocked"


def test_missing_secret_at_real_entrypoint_records_blocked_without_using_bot_token(
    tmp_path, monkeypatch
):
    """The actual workflow entrypoint exposes missing command setup and never publishes as its bot."""
    client = GitHubEvidence()
    event_file = tmp_path / "missing-secret.json"
    event_file.write_text(
        json.dumps(
            {
                "repository": {"full_name": REPOSITORY, "default_branch": "main"},
                "action": "created",
                "issue": {"number": 12, "pull_request": {"url": "synthetic"}},
                "comment": client.command,
            }
        ),
        encoding="utf-8",
    )
    for key, value in {
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_TOKEN": "synthetic-metadata-token",
        "GITHUB_EVENT_NAME": "issue_comment",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OEP_REVIEW_COMMAND_TOKEN")
    monkeypatch.setattr(review, "GitHub", lambda token: client)
    assert review.main() == 1
    assert client.state()["phase"] == "blocked"
    assert "OEP_REVIEW_COMMAND_TOKEN" in client.state()["message"]
    assert client.provider_commands() == []


def test_legacy_bot_request_cannot_automatically_adopt_the_user_credential():
    """An active old bot-owned publication cannot change its account when new credentials appear."""
    client = GitHubEvidence()
    client.run(explicit=True)
    state = client.state()
    del state["publisher_id"]
    review.save_state(state, "PR_12", client)
    command = next(row for row in client.comments if row["body"].startswith("@coderabbitai"))
    command["user"] = {"type": "Bot", "login": "github-actions[bot]", "id": 41898282}
    client.reply(AVAILABLE)
    client.run()
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_upstream_pr_author_without_write_can_request_review_of_own_fork_contribution():
    """A real PR author may request review of a fork contribution submitted to the upstream repo."""
    client = GitHubEvidence()
    client.current["user"] = {"type": "User", "login": "developer", "id": 99}
    client.current["head"]["repo"]["full_name"] = "contributor/oep-fork"
    client.command["author_association"] = "NONE"
    client.permission = "read"
    assert review.requester_authorized(REPOSITORY, 12, client.command, client)


@pytest.mark.parametrize("explicit", [True, False])
def test_fork_author_can_start_and_resume_an_upstream_review_without_write(explicit):
    """The PR author is found in both direct events and the durable inbox without association privileges."""
    client = GitHubEvidence()
    client.current["user"] = {"type": "User", "login": "developer", "id": 99}
    client.current["head"]["repo"]["full_name"] = "contributor/oep-fork"
    client.command["author_association"] = "FIRST_TIMER"
    client.permission = "read"
    client.run(explicit=explicit)
    client.reply(AVAILABLE)
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit", "@coderabbitai review"]
    assert client.state()["publisher_id"] == 4242


@pytest.mark.parametrize("permission", ["admin", "maintain", "write"])
def test_upstream_writers_can_request_review_for_another_author(permission):
    """Each documented upstream writer role may review a PR belonging to another GitHub user."""
    client = GitHubEvidence()
    client.permission = permission
    client.command["author_association"] = "NONE"
    assert client.current["user"]["id"] != client.command["user"]["id"]
    client.run(explicit=True)
    assert client.provider_commands() == ["@coderabbitai rate limit"]


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"])
def test_forged_association_does_not_authorize_another_users_pr(association):
    """Repository-looking comment associations cannot replace live authorship or write authority."""
    client = GitHubEvidence()
    client.permission = "read"
    client.command["author_association"] = association
    client.run()
    assert client.provider_commands() == []
    assert client.state() is None


@pytest.mark.parametrize(
    "user",
    [
        {"type": "Bot", "login": "developer", "id": 1001},
        {"type": "User", "login": "developer", "id": True},
        {"type": "User", "login": "developer", "id": 0},
        {"type": "User", "login": "developer", "id": -1},
        {"type": "User", "login": "developer"},
        {"type": "User", "login": "bad/name", "id": 1001},
        None,
    ],
)
def test_malformed_or_bot_requester_cannot_claim_the_author_identity(user):
    """Only a positive numeric GitHub User ID can exercise the own-PR authorization path."""
    client = GitHubEvidence()
    client.command["user"] = user
    assert not review.requester_authorized(REPOSITORY, 12, client.command, client)
    assert client.provider_commands() == []


@pytest.mark.parametrize(
    "base",
    [
        {"repo": {"full_name": "contributor/oep-fork"}, "ref": "main"},
        {"repo": {"full_name": "other/upstream"}, "ref": "main"},
        {"repo": {"full_name": "patrickvorgers/open-energy-planner"}, "ref": "feature"},
        {"repo": None, "ref": "main"},
        None,
    ],
)
def test_even_a_writer_cannot_authorize_a_fork_hosted_or_wrong_target_pr(base):
    """The target repository and branch guard applies before either author or writer privileges."""
    client = GitHubEvidence()
    client.current["base"] = base
    assert not review.requester_authorized(REPOSITORY, 12, client.command, client)
    assert client.provider_commands() == []


def test_fork_repository_context_cannot_authorize_even_the_matching_pr_author():
    """A PR hosted inside a fork cannot borrow the upstream authorization or publisher credential."""
    client = GitHubEvidence()
    client.current["user"] = deepcopy(client.command["user"])
    assert not review.requester_authorized("contributor/oep-fork", 12, client.command, client)
    assert client.calls == []


def test_own_pr_permission_never_relaxes_the_publisher_write_requirement():
    """Being the PR author cannot authorize a read-only publishing account."""
    client = GitHubEvidence()
    client.current["user"] = deepcopy(client.publisher.user)
    original_get = client.get

    def publisher_read_only(path):
        """Keep the requester authorized while denying write permission to the PR author publisher."""
        if path.endswith("/collaborators/review-publisher/permission"):
            return {"permission": "read"}
        return original_get(path)

    client.get = publisher_read_only
    with pytest.raises(review.CommandAccountError):
        client.run(explicit=True)
    assert client.provider_commands() == []


def test_lost_response_recovery_rechecks_original_writer_before_another_command():
    """Reconciling an existing publication cannot authorize a new review after requester revocation."""
    client = GitHubEvidence()
    client.lose_response = True
    with pytest.raises(VerificationError):
        client.run(explicit=True)
    client.lose_response = False
    client.permission = "read"
    client.reply(AVAILABLE)
    client.run()
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_deleted_original_request_cannot_authorize_another_provider_command():
    """A removed request is unavailable authority even when its old quota publication remains."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    client.comments.remove(client.command)
    client.run()
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_matching_author_name_with_a_different_numeric_id_is_not_ownership():
    """A matching display login does not replace the live PR author's numeric GitHub identity."""
    client = GitHubEvidence()
    client.current["user"] = {"type": "User", "login": "developer", "id": 1001}
    client.permission = "read"
    assert not review.requester_authorized(REPOSITORY, 12, client.command, client)


def test_entrypoint_refuses_a_fork_hosted_workflow_before_accessing_credentials(
    tmp_path, monkeypatch
):
    """A cloned main workflow cannot acquire the upstream credential path in another repository."""
    event_file = tmp_path / "fork-context.json"
    event_file.write_text(
        json.dumps({"repository": {"full_name": "contributor/oep-fork", "default_branch": "main"}}),
        encoding="utf-8",
    )
    for key, value in {
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": "contributor/oep-fork",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "schedule",
    }.items():
        monkeypatch.setenv(key, value)
    credentials = []

    def forbidden_credential(token):
        """Record any attempt to create an API client in the rejected repository context."""
        credentials.append(token)
        raise AssertionError("A fork workflow must not initialize credentials")

    monkeypatch.setattr(review, "GitHub", forbidden_credential)
    assert review.main() == 1
    assert credentials == []


@pytest.mark.parametrize("user", [None, {}, {"type": "User", "login": "deleted-user", "id": None}])
def test_unavailable_comment_author_cannot_break_pending_inbox_discovery(user):
    """Deleted or malformed comment authors are ignored without authorizing or crashing a sweep."""
    client = GitHubEvidence()
    client.command["user"] = user
    client.run()
    assert client.provider_commands() == []
    assert client.state() is None


@pytest.mark.parametrize(
    "published", [True, False], ids=["recover-user-receipt", "block-missing-receipt"]
)
def test_null_author_outbox_recovery_preserves_identity_and_never_duplicates(published):
    """A deleted unrelated author cannot crash reconciliation or authorize a duplicate publication."""
    client = GitHubEvidence()
    client.lose_response = published
    client.fail_before_send = not published
    with pytest.raises(VerificationError):
        client.run(explicit=True)
    client.lose_response = False
    client.fail_before_send = False
    client.comment("Ordinary comment from an unavailable account")["user"] = None
    client.run()
    assert client.state()["publisher_id"] == 4242
    assert client.state()["phase"] == ("quota" if published else "blocked")
    assert client.provider_commands() == (["@coderabbitai rate limit"] if published else [])
    client.run()
    assert client.provider_commands() == (["@coderabbitai rate limit"] if published else [])


@pytest.mark.parametrize("failure", ["revoked", "deleted", "unavailable"])
def test_original_requester_failure_explains_the_public_status_without_private_data(failure):
    """A lost request or requester authority points users to authorization rather than provider quota."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(AVAILABLE)
    if failure == "revoked":
        client.permission = "read"
    elif failure == "deleted":
        client.comments.remove(client.command)
    else:
        original_get = client.get

        def unavailable_permission(path):
            """Simulate a failed requester permission read with a private provider error."""
            if path.endswith("/collaborators/developer/permission"):
                raise VerificationError("PRIVATE-REQUESTER-SENTINEL")
            return original_get(path)

        client.get = unavailable_permission
    client.run()
    assert client.state()["phase"] == "blocked"
    statuses = [
        row["body"] for row in client.comments if row["body"].startswith("<!-- oep-review-state:")
    ]
    assert "Check the request comment" in statuses[-1]
    assert "requester" in statuses[-1]
    assert "CodeRabbit availability could not be verified" not in statuses[-1]
    assert "PRIVATE-REQUESTER-SENTINEL" not in json.dumps(client.comments)
    assert client.provider_commands() == ["@coderabbitai rate limit"]


@pytest.mark.parametrize("failure", ["revoked", "deleted"])
def test_requester_failure_during_start_uses_authority_recovery_advice(failure):
    """A request withdrawn after its first status is created gets authorization-specific advice."""
    client = GitHubEvidence()
    original_post = client.post

    def withdraw_after_initial_status(path, payload):
        """Change the original request once its initial durable status has actually been written."""
        result = original_post(path, payload)
        if payload["query"] == review.ADD_COMMENT and payload["variables"]["body"].startswith(
            "<!-- oep-review-state:"
        ):
            if failure == "revoked":
                client.permission = "read"
            else:
                client.comments.remove(client.command)
        return result

    client.post = withdraw_after_initial_status
    with pytest.raises(review.RequesterAuthorityError):
        client.run(explicit=True)
    assert client.state()["phase"] == "blocked"
    statuses = [
        row["body"] for row in client.comments if row["body"].startswith("<!-- oep-review-state:")
    ]
    assert "Check the request comment" in statuses[-1]
    assert "PR authorship or repository write access" in statuses[-1]
    assert "CodeRabbit availability could not be verified" not in statuses[-1]
    assert client.provider_commands() == []
