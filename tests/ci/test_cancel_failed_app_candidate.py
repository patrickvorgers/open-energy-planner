"""Exercise the cancellation decision over representative GitHub REST data."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.ci.cancel_failed_app_candidate import cancel_failed_app_candidates
from script.ci.check_commit_signatures import GitHub, VerificationError

REPO = "patrickvorgers/open-energy-planner"
ROOT = f"repos/{REPO}"
BASE = "a" * 40
HEAD = "b" * 40
OTHER = "c" * 40
LIST = f"{ROOT}/actions/workflows/checks.yaml/runs?event=pull_request&head_sha={HEAD}&per_page=100"


def run(number, workflow, *, status="in_progress", conclusion=None):
    """Use GitHub's PR association and repo identity on each Actions record."""
    return {
        "id": number,
        "event": "pull_request",
        "path": f".github/workflows/{workflow}.yaml",
        "repository": {"id": 24},
        "head_repository": {"id": 24},
        "head_sha": HEAD,
        "pull_requests": [
            {"number": 220, "head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "main"}}
        ],
        "created_at": "2026-11-25T19:47:07Z",
        "run_started_at": "2026-11-25T19:47:07Z",
        "run_attempt": 1,
        "status": status,
        "conclusion": conclusion,
    }


class Actions:
    """Return exact expected API paths and capture only cancellation requests."""

    def __init__(self):
        """Create a failed measurement and an earlier live candidate on the same PR."""
        self.measure = run(71, "repowise-coverage", status="completed", conclusion="failure")
        self.event = {
            "action": "completed",
            "repository": {"id": 24, "full_name": REPO},
            "workflow_run": deepcopy(self.measure),
        }
        self.candidate = run(70, "checks")
        self.pull = {
            "number": 220,
            "state": "open",
            "draft": False,
            "base": {"sha": BASE, "ref": "main"},
            "head": {"sha": HEAD, "repo": {"id": 24}},
        }
        self.test_step = {
            "name": "Run app unit tests and export coverage",
            "conclusion": "failure",
            "completed_at": "2026-11-25T19:53:01Z",
        }
        self.measured_job = {
            "name": "Measure Python app coverage",
            "conclusion": "failure",
            "steps": [self.test_step],
        }
        self.inventory = [deepcopy(self.candidate)]
        self.comparison = {"behind_by": 0, "merge_base_commit": {"sha": BASE}}
        self.cancelled = []

    def get(self, path):
        """Read independent copies so the list and later live read can legitimately differ."""
        return deepcopy(
            {
                f"{ROOT}/actions/runs/71": self.measure,
                f"{ROOT}/actions/runs/71/attempts/1/jobs?per_page=100": {
                    "total_count": 2,
                    "jobs": [self.measured_job, {"name": "Submit test reports to Repowise"}],
                },
                f"{ROOT}/pulls/220": self.pull,
                f"{ROOT}/compare/{BASE}...{HEAD}": self.comparison,
                LIST: {"total_count": len(self.inventory), "workflow_runs": self.inventory},
                f"{ROOT}/actions/runs/70": self.candidate,
            }[path]
        )

    def cancel_workflow_run(self, repository, run_id):
        """Record the mutation without touching the network."""
        self.cancelled.append((repository, run_id))


def test_failed_app_suite_stops_only_the_overlapping_checks_candidate():
    """Match the failing PR, revision and attempt before requesting cancellation."""
    actions = Actions()
    unrelated = run(88, "checks")
    unrelated["pull_requests"][0]["number"] = 221
    actions.inventory.append(unrelated)
    assert cancel_failed_app_candidates(actions.event, actions) == (70,)
    assert actions.cancelled == [(REPO, 70)]


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("test_step", "conclusion", "success"),
        ("test_step", "conclusion", "skipped"),
        ("measured_job", "conclusion", "success"),
        ("measure", "run_attempt", 2),
        ("measure", "id", 72),
        ("measure", "status", "in_progress"),
        ("measure", "conclusion", "success"),
        ("measure", "head_sha", OTHER),
        ("pull", "draft", True),
        ("pull", "state", "closed"),
        ("candidate", "status", "completed"),
        ("candidate", "run_attempt", 2),
        ("candidate", "id", 72),
        ("candidate", "head_sha", OTHER),
        ("candidate", "created_at", "2026-11-25T19:54:00Z"),
        ("candidate", "path", ".github/workflows/quick-checks.yaml"),
        ("comparison", "behind_by", 1),
        ("comparison", "merge_base_commit", {"sha": OTHER}),
    ],
)
def test_missing_or_stale_shared_test_failure_leaves_candidates_running(target, field, value):
    """Disagreeing source, attempts and report-only failures cannot trigger a cancellation."""
    actions = Actions()
    getattr(actions, target)[field] = value
    assert cancel_failed_app_candidates(actions.event, actions) == ()
    assert actions.cancelled == []


def test_a_main_or_fork_measurement_event_never_lists_candidates():
    """The privileged workflow ignores non-PR events and PR work from forks."""
    actions = Actions()
    actions.event["workflow_run"]["event"] = "push"
    assert cancel_failed_app_candidates(actions.event, None) == ()
    actions.event["workflow_run"]["event"] = "pull_request"
    actions.event["workflow_run"]["head_repository"]["id"] = 17
    assert cancel_failed_app_candidates(actions.event, None) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", ".github/workflows/checks.yaml"),
        ("repository", {"id": 17}),
        ("head_repository", None),
        ("pull_requests", None),
        ("pull_requests", {}),
        ("pull_requests", []),
        (
            "pull_requests",
            [
                {"number": 220, "head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "main"}},
                {"number": 221, "head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "main"}},
            ],
        ),
        (
            "pull_requests",
            [{"number": 220, "head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "release"}}],
        ),
        ("head_sha", OTHER),
    ],
)
def test_unbound_measurement_event_never_reads_or_cancels_runs(field, value):
    """Require one unambiguous PR link to the intended workflow, repositories and head."""
    actions = Actions()
    actions.event["workflow_run"][field] = value
    assert cancel_failed_app_candidates(actions.event, None) == ()


def test_initial_candidate_waiting_in_the_queue_is_cancelled_after_the_failure():
    """A slow runner allocation does not turn the original request into a later retry."""
    actions = Actions()
    actions.candidate["run_started_at"] = "2026-11-25T19:54:00Z"
    assert cancel_failed_app_candidates(actions.event, actions) == (70,)


def test_new_attempt_started_after_the_failure_is_not_cancelled():
    """A deliberate second candidate attempt has its own start time."""
    actions = Actions()
    actions.candidate.update(run_attempt=2, run_started_at="2026-11-25T19:54:00Z")
    actions.inventory[0] = deepcopy(actions.candidate)
    assert cancel_failed_app_candidates(actions.event, actions) == ()
    assert actions.cancelled == []


def test_a_candidate_completed_during_the_cancellation_request():
    """Treat the normal completion race as harmless without retrying the mutation."""
    actions = Actions()

    def complete_before_cancellation(repository, run_id):
        assert (repository, run_id) == (REPO, 70)
        actions.candidate["status"] = "completed"
        raise VerificationError("GitHub verification request failed")

    actions.cancel_workflow_run = complete_before_cancellation
    assert cancel_failed_app_candidates(actions.event, actions) == ()


def test_pull_request_changed_after_the_candidate_was_listed():
    """Recheck live PR state before cancelling even when the candidate still matches."""
    actions = Actions()
    read = actions.get

    def move_pr_on_candidate_read(path):
        """Simulate a new PR head after the initial PR read and candidate listing."""
        if path == f"{ROOT}/actions/runs/70":
            actions.pull["head"]["sha"] = OTHER
        return read(path)

    actions.get = move_pr_on_candidate_read
    assert cancel_failed_app_candidates(actions.event, actions) == ()
    assert actions.cancelled == []


def test_cancellation_failure_while_candidate_is_active_propagates():
    """Keep a real cancellation failure visible instead of treating it as a completion race."""
    actions = Actions()

    def reject_cancellation(repository, run_id):
        """Fail the POST while the candidate remains active."""
        assert (repository, run_id) == (REPO, 70)
        raise VerificationError("Synthetic active cancellation failure")

    actions.cancel_workflow_run = reject_cancellation
    with pytest.raises(VerificationError, match="Synthetic active cancellation failure"):
        cancel_failed_app_candidates(actions.event, actions)


def test_github_accepts_the_empty_202_response_to_the_cancel_post():
    """The transport uses the verified candidate endpoint and accepts its empty 202 response."""
    client = GitHub("unit-test-token")

    class EmptyResponse:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

    def open_cancel(request, timeout):
        assert request.full_url == f"https://api.github.com/{ROOT}/actions/runs/70/cancel"
        assert request.get_method() == "POST"
        assert timeout == 20
        return EmptyResponse()

    client._opener = SimpleNamespace(open=open_cancel)
    client.cancel_workflow_run(REPO, 70)


def test_privileged_cancellation_workflow_only_runs_immutable_main_code():
    """Guard the combination of default-branch checkout and narrowly held write authority."""
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/cancel-failed-app-candidate.yaml"
    ).read_text(encoding="utf-8")
    assert "workflow_run:" in workflow
    assert "types: [completed]" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "actions: write" in workflow
    assert "download-artifact" not in workflow
    assert "head_sha" not in workflow
