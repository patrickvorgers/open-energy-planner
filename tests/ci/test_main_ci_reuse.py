"""Only exact successfully tested merge trees with original bundles can skip duplicate CI."""

import json
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from http.client import IncompleteRead

import pytest

from script.ci.check_commit_signatures import GitHub, VerificationError
from script.ci.check_main_ci_reuse import check, main

REPOSITORY = "owner/planner"
BASE = "1" * 40
HEAD = "2" * 40
MERGE = "3" * 40
TREE = "4" * 40
ROOT = "repos/owner/planner"
NOW = datetime(2026, 10, 28, 20, tzinfo=UTC)


class Evidence:
    """Serve independent GitHub metadata with mutable records and an exact request log."""

    def __init__(self):
        """Describe one merged PR, successful full attempt and original unexpired bundle."""
        self.event = {
            "repository": {"full_name": REPOSITORY},
            "ref": "refs/heads/main",
            "after": MERGE,
            "forced": False,
        }
        self.commit = {
            "sha": MERGE,
            "tree": {"sha": TREE},
            "parents": [{"sha": BASE}, {"sha": HEAD}],
            "verification": {"verified": True, "reason": "valid"},
        }
        self.pull = {
            "number": 12,
            "merged": True,
            "merge_commit_sha": MERGE,
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPOSITORY}},
            "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}},
        }
        self.run = {
            "id": 80,
            "workflow_id": 60,
            "path": ".github/workflows/checks.yaml",
            "head_sha": HEAD,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2,
            "run_number": 90,
            "repository": {"full_name": REPOSITORY},
            "head_repository": {"full_name": REPOSITORY},
        }
        self.quality = {
            "id": 100,
            "name": "Quality and integration (AMD64)",
            "run_id": 80,
            "run_attempt": 2,
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-28T19:00:00Z",
            "completed_at": "2026-10-28T19:30:00Z",
            "steps": [
                {
                    "name": "Native checks, planner scenarios, app smoke and real HA handoff",
                    "number": 8,
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": "Full CI suite completion",
                    "number": 10,
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": "Full CI source tree: " + TREE,
                    "number": 11,
                    "status": "completed",
                    "conclusion": "success",
                },
            ],
        }
        self.bundle = {
            "id": 120,
            "name": "installation-bundle-amd64",
            "expired": False,
            "expires_at": "2026-11-11T19:29:00Z",
            "created_at": "2026-10-28T19:29:00Z",
            "workflow_run": {"id": 80, "head_sha": HEAD},
            "digest": "sha256:" + "a" * 64,
        }
        self.associated = [self.pull]
        self.runs = {"total_count": 1, "workflow_runs": [self.run]}
        self.jobs = {"total_count": 1, "jobs": [self.quality]}
        self.artifacts = {"total_count": 1, "artifacts": [self.bundle]}
        self.final_run = None
        self.requests = []
        self.run_reads = 0

    def get(self, path):
        """Require every read to use the intended immutable run, attempt and commit identity."""
        self.requests.append(path)
        runs_path = (
            f"{ROOT}/actions/workflows/checks.yaml/runs?event=pull_request"
            f"&head_sha={HEAD}&per_page=100"
        )
        if path == runs_path:
            self.run_reads += 1
            return deepcopy(
                {"total_count": 1, "workflow_runs": [self.final_run]}
                if self.final_run is not None and self.run_reads > 1
                else self.runs
            )
        records = {
            f"{ROOT}/git/commits/{MERGE}": self.commit,
            f"{ROOT}/pulls/12": self.pull,
            f"{ROOT}/actions/workflows/checks.yaml": {
                "id": 60,
                "path": ".github/workflows/checks.yaml",
            },
            f"{ROOT}/actions/runs/80/attempts/2/jobs?per_page=100": self.jobs,
            f"{ROOT}/actions/runs/80/artifacts?per_page=100": self.artifacts,
            f"{ROOT}/actions/runs/80": self.final_run if self.final_run is not None else self.run,
        }
        return deepcopy(records[path])

    def get_list(self, path):
        """Provide only the commit's bounded associated-PR list."""
        self.requests.append(path)
        assert path == f"{ROOT}/commits/{MERGE}/pulls?per_page=100"
        return deepcopy(self.associated)


@pytest.fixture
def evidence():
    """Keep every test's candidate and installation evidence isolated."""
    return Evidence()


def test_exact_tested_merge_reuses_original_attempt_and_bundle(evidence):
    """A matching complete proof returns the original 90.2 identity, never a new main build."""
    assert check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW) == {
        "reuse": "true",
        "tested_run_id": "80",
        "installation_artifact_id": "120",
        "installation_build": "90.2",
        "tested_tree": "4" * 40,
    }
    assert evidence.run_reads == 2


@pytest.mark.parametrize("operating_system", ["Windows", "Linux"])
def test_personal_candidate_reuses_exact_proof_without_repeating_on_main(
    evidence, operating_system
):
    """A personal runner changes execution location, never artifact or source-tree requirements."""
    evidence.quality["runner_name"] = "personal-fixture"
    evidence.quality["labels"] = ["self-hosted", "X64", "oep-ci-local", operating_system]
    result = check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)
    assert result["reuse"] == "true"
    assert result["installation_artifact_id"] == "120"
    assert result["tested_tree"] == TREE
    evidence.commit["tree"]["sha"] = "5" * 40
    with pytest.raises(VerificationError):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


@pytest.fixture(params=["ci", "selected:browser,ci"])
def focused_product_evidence(evidence, monkeypatch, request):
    """Describe exact CI-only or browser coverage with both original artifact kinds."""
    evidence.selected_profile = request.param
    evidence.quality["steps"][0]["name"] = "Selected native and integration suites"
    evidence.quality["steps"][1]["name"] = "Selected CI suite completion"
    evidence.quality["steps"][2]["name"] = (
        "Selected CI source tree: " + TREE + f" ({request.param})"
    )
    reports = {**evidence.bundle, "id": 121, "name": "test-reports-amd64"}
    evidence.artifacts = {"total_count": 2, "artifacts": [evidence.bundle, reports]}

    def base_policy(base, tested, *, selector):
        """Classify the actual first-parent change independently of the candidate's marker."""
        assert (base, tested, selector) == (BASE, MERGE, "candidate_reuse_profile")
        return request.param

    monkeypatch.setattr("script.ci.check_main_ci_reuse.base_test_only_profile", base_policy)
    return evidence


def test_exact_focused_product_reuses_original_bundle_and_reports(focused_product_evidence):
    """Exact selected coverage can avoid duplicate main tests without pretending to be full validation."""
    evidence = focused_product_evidence
    assert check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW) == {
        "reuse": "true",
        "tested_run_id": "80",
        "tested_tree": TREE,
        "installation_artifact_id": "120",
        "installation_build": "90.2",
        "candidate_profile": evidence.selected_profile,
        "test_report_artifact_id": "121",
    }


@pytest.mark.parametrize(
    "fault",
    [
        "policy_unavailable",
        "policy_wider",
        "other_tree",
        "completion_skipped",
        "missing_reports",
        "expired_reports",
        "missing_bundle",
        "wrong_attempt",
        "newer_run",
        "unsigned",
        "mixed_markers",
        "unordered",
    ],
)
def test_focused_product_reuse_rejects_missing_or_wider_evidence(
    focused_product_evidence, monkeypatch, fault
):
    """Focused product evidence cannot bypass identity, actual coverage, original artifacts or latest-run gates."""
    evidence = focused_product_evidence
    if fault in {"policy_unavailable", "policy_wider"}:
        monkeypatch.setattr(
            "script.ci.check_main_ci_reuse.base_test_only_profile",
            lambda *args, **kwargs: (
                None if fault == "policy_unavailable" else "selected:browser,ci,ha-history"
            ),
        )
    elif fault == "other_tree":
        evidence.quality["steps"][2]["name"] = (
            "Selected CI source tree: " + "5" * 40 + f" ({evidence.selected_profile})"
        )
    elif fault == "completion_skipped":
        evidence.quality["steps"][1]["conclusion"] = "skipped"
    elif fault in {"missing_reports", "missing_bundle"}:
        missing = (
            "test-reports-amd64" if fault == "missing_reports" else "installation-bundle-amd64"
        )
        evidence.artifacts["artifacts"] = [
            row for row in evidence.artifacts["artifacts"] if row["name"] != missing
        ]
        evidence.artifacts["total_count"] = 1
    elif fault == "expired_reports":
        evidence.artifacts["artifacts"][1]["expired"] = True
    elif fault == "wrong_attempt":
        evidence.artifacts["artifacts"][1]["created_at"] = "2026-10-28T18:59:00Z"
    elif fault == "newer_run":
        evidence.final_run = {**evidence.run, "status": "in_progress", "run_attempt": 3}
    elif fault == "unsigned":
        evidence.commit["verification"]["verified"] = False
    elif fault == "mixed_markers":
        evidence.quality["steps"].append(
            {**evidence.quality["steps"][2], "name": "Full CI source tree: " + TREE}
        )
    else:
        evidence.quality["steps"][2]["number"] = 9
    with pytest.raises(VerificationError):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


@pytest.fixture
def test_only_evidence(evidence, monkeypatch):
    """Describe focused browser evidence with reports and no deployable installation bundle."""
    evidence.quality["steps"][0]["name"] = "Selected native and integration suites"
    evidence.quality["steps"][1]["name"] = "Selected CI suite completion"
    evidence.quality["steps"][2]["name"] = (
        "Test-only CI source tree: " + TREE + " (selected:browser,ci)"
    )
    evidence.bundle["name"] = "test-reports-amd64"

    def base_policy(base, tested):
        """Require independent classification of the actual first-parent and merged tree."""
        assert (base, tested) == (BASE, MERGE)
        return "selected:browser,ci"

    monkeypatch.setattr("script.ci.check_main_ci_reuse.base_test_only_profile", base_policy)
    return evidence


def test_exact_test_only_candidate_reuses_reports_without_a_build(test_only_evidence):
    """A complete focused proof returns report identities and cannot invent a deployable build."""
    evidence = test_only_evidence
    assert check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW) == {
        "reuse": "true",
        "tested_run_id": "80",
        "tested_tree": TREE,
        "test_only_profile": "selected:browser,ci",
        "test_report_artifact_id": "120",
    }


@pytest.mark.parametrize(
    "fault",
    ["policy", "profile", "tree", "completion", "expired", "artifact", "newer_run", "unsigned"],
)
def test_test_only_reuse_rejects_incomplete_or_differently_scoped_proof(
    test_only_evidence, monkeypatch, fault
):
    """Focused proof never permits stale reports, missing completion, other trees or widened policy."""
    evidence = test_only_evidence
    if fault == "policy":
        monkeypatch.setattr(
            "script.ci.check_main_ci_reuse.base_test_only_profile", lambda *args: None
        )
    elif fault == "profile":
        monkeypatch.setattr(
            "script.ci.check_main_ci_reuse.base_test_only_profile",
            lambda *args: "selected:ci,ha-history",
        )
    elif fault == "tree":
        evidence.quality["steps"][2]["name"] = (
            "Test-only CI source tree: " + "5" * 40 + " (selected:browser,ci)"
        )
    elif fault == "completion":
        evidence.quality["steps"][1]["conclusion"] = "skipped"
    elif fault == "expired":
        evidence.bundle["expired"] = True
    elif fault == "artifact":
        evidence.bundle["name"] = "installation-bundle-amd64"
    elif fault == "newer_run":
        evidence.final_run = {**evidence.run, "status": "in_progress", "run_attempt": 3}
    else:
        evidence.commit["verification"]["verified"] = False
    with pytest.raises(VerificationError):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


def test_two_pr_combination_requires_full_ci(evidence):
    """PR B's proof excludes PR A, so the merged A+B tree must be tested as a whole."""
    combined_tree = "5" * 40
    evidence.commit["tree"]["sha"] = combined_tree
    with pytest.raises(VerificationError, match="identical tested tree"):
        check(evidence.event, REPOSITORY, MERGE, combined_tree, evidence, NOW)


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("queued", None),
        ("in_progress", None),
        ("completed", None),
    ],
)
def test_newer_candidate_cannot_hide_behind_older_success(evidence, status, conclusion):
    """Even unordered API rows cannot let an older success supersede a newer non-success."""
    newer = {**evidence.run, "id": 81, "run_number": 91, "status": status, "conclusion": conclusion}
    evidence.runs.update(total_count=2, workflow_runs=[evidence.run, newer])
    with pytest.raises(VerificationError, match="candidate run"):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


def test_new_run_starting_during_validation_blocks_reuse(evidence):
    """Final evidence selection notices a different newer run, not only a rerun of the old ID."""
    evidence.final_run = {
        **evidence.run,
        "id": 81,
        "run_number": 91,
        "status": "in_progress",
        "conclusion": None,
    }
    with pytest.raises(VerificationError, match="changed"):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


@pytest.mark.parametrize("status", ["skipped", "failure", "cancelled", None])
@pytest.mark.parametrize("target", ["run", "quality", "suite", "tree"])
def test_missing_or_unsuccessful_full_result_does_not_qualify(evidence, target, status):
    """A green wrapper cannot hide skipped, pending or failed full-suite/proof steps."""
    record = {
        "run": evidence.run,
        "quality": evidence.quality,
        "suite": evidence.quality["steps"][0],
        "tree": evidence.quality["steps"][2],
    }[target]
    record["conclusion"] = status
    with pytest.raises(VerificationError):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


@pytest.mark.parametrize(
    "fault",
    [
        "direct_push",
        "force_push",
        "unsigned",
        "wrong_tree",
        "wrong_head",
        "wrong_merge",
        "fork",
        "other_workflow",
        "other_event",
        "old_attempt",
        "no_runs",
        "no_jobs",
        "incomplete_jobs",
        "duplicate_quality",
        "no_marker",
        "expired",
        "expired_date",
        "old_artifact",
        "other_artifact_head",
        "missing_artifact",
        "duplicate_artifact",
        "bad_digest",
        "rerun_started",
        "ambiguous_pr",
    ],
)
def test_untrusted_stale_or_incomplete_evidence_retains_full_checks(evidence, fault):
    """Independent identity and availability failures never produce permission to skip CI."""
    if fault == "direct_push":
        evidence.commit["parents"].pop()
    elif fault == "force_push":
        evidence.event["forced"] = True
    elif fault == "unsigned":
        evidence.commit["verification"]["verified"] = False
    elif fault == "wrong_tree":
        evidence.commit["tree"]["sha"] = "5" * 40
    elif fault == "wrong_head":
        evidence.pull["head"]["sha"] = "5" * 40
    elif fault == "wrong_merge":
        evidence.pull["merge_commit_sha"] = "5" * 40
    elif fault == "fork":
        evidence.run["head_repository"]["full_name"] = "other/planner"
    elif fault == "other_workflow":
        evidence.run["workflow_id"] = 61
    elif fault == "other_event":
        evidence.run["event"] = "workflow_dispatch"
    elif fault == "old_attempt":
        evidence.quality["run_attempt"] = 1
    elif fault == "no_runs":
        evidence.runs["workflow_runs"] = []
    elif fault == "no_jobs":
        evidence.jobs.update(total_count=0, jobs=[])
    elif fault == "incomplete_jobs":
        evidence.jobs["total_count"] = 2
    elif fault == "duplicate_quality":
        evidence.jobs.update(total_count=2, jobs=[evidence.quality, evidence.quality])
    elif fault == "no_marker":
        evidence.quality["steps"].pop()
    elif fault == "expired":
        evidence.bundle["expired"] = True
    elif fault == "expired_date":
        evidence.bundle["expires_at"] = "2026-10-28T19:59:00Z"
    elif fault == "old_artifact":
        evidence.bundle["created_at"] = "2026-10-27T19:29:00Z"
    elif fault == "other_artifact_head":
        evidence.bundle["workflow_run"]["head_sha"] = "5" * 40
    elif fault == "missing_artifact":
        evidence.artifacts.update(total_count=0, artifacts=[])
    elif fault == "duplicate_artifact":
        evidence.artifacts.update(total_count=2, artifacts=[evidence.bundle, evidence.bundle])
    elif fault == "bad_digest":
        evidence.bundle["digest"] = "unverified"
    elif fault == "rerun_started":
        evidence.final_run = {**evidence.run, "status": "in_progress", "run_attempt": 3}
    else:
        evidence.associated.append(evidence.pull)
    with pytest.raises(VerificationError):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


def test_list_transport_retains_object_only_signature_contract(monkeypatch):
    """Associated PR lists are supported without admitting lists as signature metadata."""
    client = GitHub("fixture-only")
    monkeypatch.setattr(client, "_read", lambda path: [{"number": 12}])
    assert client.get_list("repos/owner/planner/commits/fixture/pulls") == [{"number": 12}]
    with pytest.raises(VerificationError):
        client.get("repos/owner/planner/git/commits/fixture")
    monkeypatch.setattr(client, "_read", lambda path: {})
    with pytest.raises(VerificationError):
        client.get_list("repos/owner/planner/commits/fixture/pulls")


@pytest.mark.parametrize("event_name", ["pull_request", "workflow_dispatch", "release"])
def test_other_events_never_reuse_main_evidence(monkeypatch, tmp_path, event_name):
    """Manual, release and PR runs retain their tests even with no API credentials."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert main() == 0
    assert (tmp_path / "output").read_text() == "reuse=false\n"


@pytest.fixture
def cli(evidence, monkeypatch, tmp_path):
    """Run the real decision and output writer against a fixed clock and controlled checkout."""
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(evidence.event))
    for key, value in {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_SHA": MERGE,
        "GITHUB_TOKEN": "private-fixture-token",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output",
        lambda command, **kwargs: MERGE if command[-1] == "HEAD" else TREE,
    )
    monkeypatch.setattr("script.ci.proof_git.subprocess.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("script.ci.check_main_ci_reuse.GitHub", lambda token: evidence)
    monkeypatch.setattr("script.ci.check_main_ci_reuse.check", partial(check, now=NOW))
    return tmp_path


def test_cli_links_original_bundle_and_does_not_invent_main_build(cli, monkeypatch):
    """A successful shortcut exposes the original candidate build even in a differently numbered run."""
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "999")
    assert main() == 0
    output = dict(line.split("=", 1) for line in (cli / "output").read_text().splitlines())
    assert output == {
        "reuse": "true",
        "tested_run_id": "80",
        "installation_artifact_id": "120",
        "installation_build": "90.2",
        "tested_tree": "4" * 40,
    }
    assert "No new installation build was created" in (cli / "summary").read_text()


def test_cli_test_only_summary_links_reports_without_authorizing_deployment(
    cli, test_only_evidence
):
    """The main summary identifies focused evidence and never supplies a deployable build identity."""
    assert main() == 0
    outputs = dict(line.split("=", 1) for line in (cli / "output").read_text().splitlines())
    assert outputs["reuse"] == "true"
    assert outputs["test_only_profile"] == "selected:browser,ci"
    assert "installation_build" not in outputs
    assert "installation_artifact_id" not in outputs
    summary = (cli / "summary").read_text()
    assert "/actions/runs/80/artifacts/120" in summary
    assert "No installation bundle or deployment is authorized" in summary


def test_api_outage_requests_full_ci_without_leaking_credentials(
    cli, evidence, monkeypatch, capsys
):
    """Unavailable evidence writes reuse=false rather than failing closed into a skipped suite."""

    def unavailable(path):
        """Model the shared transport's sanitized API outage."""
        raise VerificationError("Unavailable")

    monkeypatch.setattr(evidence, "get", unavailable)
    assert main() == 0
    assert (cli / "output").read_text() == "reuse=false\n"
    assert "private-fixture-token" not in (cli / "summary").read_text() + capsys.readouterr().out


@pytest.mark.parametrize(
    "record,field,value",
    [
        ("commit", "tree", None),
        ("commit", "verification", []),
        ("run", "repository", None),
        ("run", "run_attempt", True),
        ("quality", "steps", [None]),
        ("quality", "steps", None),
        ("quality", "started_at", "not a date"),
        ("bundle", "expires_at", "2026-11-11T19:29:00"),
        ("bundle", "workflow_run", []),
        ("bundle", "id", "120\nreuse=true"),
        ("jobs", "total_count", True),
        ("artifacts", "total_count", True),
    ],
)
def test_malformed_api_shapes_request_full_checks(cli, evidence, record, field, value):
    """Malformed external JSON cannot turn an unavailable proof into a successful shortcut."""
    getattr(evidence, record)[field] = value
    assert main() == 0
    assert (cli / "output").read_text() == "reuse=false\n"


def test_truncated_http_response_selects_full_ci(cli, monkeypatch, capsys):
    """A truncated chunked body passes through the real transport's sanitized fallback."""
    client = GitHub("private-fixture-token")

    class Response:
        """Model a successful HTTP status whose chunked body ends prematurely."""

        status = 200

        def __enter__(self):
            """Expose the response through urllib's context-manager contract."""
            return self

        def __exit__(self, *arguments):
            """Close without suppressing the read failure."""

        def read(self, limit):
            """Fail during the actual bounded body read without returning partial credentials."""
            raise IncompleteRead(b"private-fixture-token", 100)

    class Transport:
        """Fail inside the standard HTTP protocol layer without exposing partial private bytes."""

        def open(self, request, timeout):
            """Return the controlled response without making a network request."""
            return Response()

    client._opener = Transport()
    monkeypatch.setattr("script.ci.check_main_ci_reuse.GitHub", lambda token: client)
    assert main() == 0
    assert (cli / "output").read_text() == "reuse=false\n"
    assert "private-fixture-token" not in (cli / "summary").read_text() + capsys.readouterr().out


def test_checkout_difference_cannot_reuse_other_commit_evidence(cli, monkeypatch):
    """Even valid remote proof cannot authorize reuse for another local checkout."""
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: "5" * 40
    )
    assert main() == 0
    assert (cli / "output").read_text() == "reuse=false\n"


def test_ambiguous_or_early_tree_proof_is_rejected(evidence):
    """Tree evidence is unique and must be recorded after the actual full suite."""
    evidence.quality["steps"].append(
        {**evidence.quality["steps"][2], "name": "Full CI source tree: " + "5" * 40}
    )
    with pytest.raises(VerificationError, match="ambiguous"):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)
    evidence.quality["steps"].pop()
    evidence.quality["steps"][2]["number"] = 1
    with pytest.raises(VerificationError, match="follow"):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


@pytest.mark.parametrize("state", ["missing", "failure", "skipped", "before_suite"])
def test_reuse_requires_ordered_successful_suite_completion(evidence, state):
    """Older candidates and unsuccessful or premature completion markers force a full rerun."""
    step = evidence.quality["steps"][1]
    assert step["name"] == "Full CI suite completion"
    if state == "missing":
        evidence.quality["steps"].remove(step)
    elif state == "before_suite":
        step["number"] = 7
    else:
        step["conclusion"] = state
    with pytest.raises(VerificationError):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


@pytest.mark.parametrize("required", [False, True, None])
@pytest.mark.parametrize("profile", ["full", "full+performance-queries"])
def test_additive_full_proof_obeys_independent_first_parent_query_policy(
    evidence, monkeypatch, required, profile
):
    """Full functional proof cannot hide omitted performance when the trusted policy explicitly requires it."""
    evidence.quality["steps"][-1]["name"] = f"Full CI source tree: {TREE} ({profile})"

    def base_policy(base, merged, *, selector):
        """Check the actual merge against its immutable first parent, independently of candidate policy."""
        assert (base, merged, selector) == (BASE, MERGE, "performance_queries_required")
        return required

    monkeypatch.setattr("script.ci.check_main_ci_reuse.base_test_only_profile", base_policy)
    if required is not True or profile == "full+performance-queries":
        assert check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)["reuse"] == "true"
    else:
        with pytest.raises(VerificationError, match="query performance"):
            check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


def test_skipped_tree_markers_do_not_hide_a_successful_full_proof(evidence, monkeypatch):
    """GitHub lists skipped test-only and selected markers beside the actual successful full marker."""
    evidence.quality["steps"][2]["name"] = "Full CI source tree: " + TREE + " (full)"
    for index, prefix in enumerate(("Test-only CI source tree: ", "Selected CI source tree: "), 12):
        evidence.quality["steps"].append(
            {
                "name": prefix + TREE + " (full)",
                "number": index,
                "status": "completed",
                "conclusion": "skipped",
            }
        )
    monkeypatch.setattr(
        "script.ci.check_main_ci_reuse.base_test_only_profile", lambda *args, **kwargs: False
    )
    result = check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)
    assert result["reuse"] == "true"
    assert result["installation_artifact_id"] == "120"


def test_skipped_full_marker_does_not_hide_successful_focused_product(focused_product_evidence):
    """The separate focused marker remains usable when the workflow's full marker is skipped."""
    evidence = focused_product_evidence
    evidence.quality["steps"].append(
        {
            "name": "Full CI source tree: " + TREE + " (selected:browser,ci)",
            "number": 12,
            "status": "completed",
            "conclusion": "skipped",
        }
    )
    result = check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)
    assert result["candidate_profile"] == evidence.selected_profile


def test_two_successful_conflicting_tree_markers_remain_ambiguous(evidence):
    """Ignoring skipped steps must not accept contradictory successful proof kinds."""
    evidence.quality["steps"].append(
        {
            "name": "Test-only CI source tree: " + TREE + " (selected:browser,ci)",
            "number": 12,
            "status": "completed",
            "conclusion": "success",
        }
    )
    with pytest.raises(VerificationError, match="ambiguous"):
        check(evidence.event, REPOSITORY, MERGE, TREE, evidence, NOW)


def test_checkout_timeout_retains_full_main_checks(cli, monkeypatch):
    """A stalled Git metadata read cannot escape fail-closed main reuse handling."""

    def timeout(command, **kwargs):
        """Model the bounded checkout read expiring before evidence can be reused."""
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", timeout)
    assert main() == 0
    assert (cli / "output").read_text() == "reuse=false\n"
