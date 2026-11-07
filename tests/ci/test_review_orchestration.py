"""Exercise authorized review orchestration, durable recovery and provider isolation without billing."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from script.ci import review_orchestration as review
from script.ci.check_commit_signatures import VerificationError

REPOSITORY = "owner/planner"
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


class GitHubEvidence:
    """Emulate bounded GitHub reads and durable comment mutations using independent wire fixtures."""

    def __init__(self):
        """Create an eligible Draft and an explicit maintainer command, without any provider work."""
        self.now = NOW
        self.comments = []
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
            "user": {"login": "developer"},
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
        self.command = self.comment("@oep review", "developer", "User")

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
            return {"permission": self.permission}
        if path.endswith("/pulls/12"):
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
        review.process_pr(REPOSITORY, 12, self, self.now, self.command["id"] if explicit else None)

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
    "answer",
    [
        "<!-- This is an auto-generated reply by CodeRabbit -->\nService unavailable",
        AVAILABLE + LIMITED,
    ],
)
def test_unknown_or_contradictory_availability_never_infers_quota(answer):
    """Unrecognized and contradictory status replies block without invoking Kody or buying credits."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.reply(answer)
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "blocked"


@pytest.mark.parametrize("permission", ["read", "triage", "none", None])
def test_unauthorized_request_cannot_publish_provider_commands(permission):
    """Public commenters cannot spend repository review capacity."""
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
    client.command = client.comment("@oep review", "developer", "User")
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
    assert client.state()["phase"] == "blocked"
    assert client.provider_commands() == ["@coderabbitai rate limit"]


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
    "event_name", ["issue_comment", "schedule", "workflow_dispatch", "check_run"]
)
def test_real_entrypoint_runs_only_trusted_main_metadata(tmp_path, monkeypatch, event_name):
    """The workflow entrypoint handles explicit requests and callbacks without executing PR code."""
    client = GitHubEvidence()
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
    monkeypatch.setattr(review, "GitHub", lambda token: client)
    assert review.main() == 0
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    monkeypatch.setenv("GITHUB_REF", "refs/heads/contributor")
    assert review.main() == 1
    assert client.provider_commands() == ["@coderabbitai rate limit"]


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
    "failure", ["command-response", "confirmation-before-write", "confirmation-response"]
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
    monkeypatch.setattr(review, "GitHub", lambda token: client)
    assert review.main() == 1
    assert client.provider_commands() == ["@coderabbitai rate limit"]


def test_consumed_duplicate_cannot_start_a_review_after_a_later_push():
    """A duplicate command acknowledged on one revision is not a fresh request for a later revision."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.command = client.comment("@oep review", "developer", "User")
    client.run(explicit=True)
    changed_head = "3" * 40
    client.current["head"]["sha"] = changed_head
    for check in client.checks:
        check["head_sha"] = changed_head
    client.run()
    assert client.provider_commands() == ["@coderabbitai rate limit"]
    assert client.state()["phase"] == "superseded"
    client.command = client.comment("@oep review", "developer", "User")
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
    monkeypatch.setattr(review, "GitHub", lambda token: client)
    assert review.main() == 1
    assert client.provider_commands() == []
    assert "PRIVATE-INVENTORY-SENTINEL" not in capsys.readouterr().out


def test_duplicate_command_consumption_requires_current_authority():
    """Revoked write access cannot acknowledge a command into the durable inbox watermark."""
    client = GitHubEvidence()
    client.run(explicit=True)
    client.command = client.comment("@oep review", "developer", "User")
    client.permission = "read"
    with pytest.raises(VerificationError, match="permission"):
        client.run(explicit=True)
    assert client.state()["consumed"] < client.command["id"]
    assert client.provider_commands() == ["@coderabbitai rate limit"]
