"""Cancel live candidates with the same PR source as a failed app test run."""

import json
from os import environ
from pathlib import Path

from script.ci.check_commit_signatures import GitHub, VerificationError, sha
from script.ci.check_main_ci_reuse import positive_id, require, timestamp

REPOSITORY = "patrickvorgers/open-energy-planner"
ROOT = f"repos/{REPOSITORY}"
MEASUREMENT_PATH = ".github/workflows/repowise-coverage.yaml"
CANDIDATE_PATH = ".github/workflows/checks.yaml"
MEASUREMENT_JOB = "Measure Python app coverage"
APP_TEST_STEP = "Run app unit tests and export coverage"
ACTIVE = {"queued", "requested", "pending", "waiting", "in_progress"}


def linked_revision(run, repository_id, path):
    """Bind a same-repository Actions run to exactly one main PR revision."""
    links = run.get("pull_requests")
    if (
        run.get("event") != "pull_request"
        or run.get("path") != path
        or (run.get("repository") or {}).get("id") != repository_id
        or (run.get("head_repository") or {}).get("id") != repository_id
        or not isinstance(links, list)
        or len(links) != 1
    ):
        return None
    link = links[0]
    if link["base"]["ref"] != "main" or link["head"]["sha"] != run.get("head_sha"):
        return None
    return positive_id(link["number"]), sha(link["base"]["sha"]), sha(link["head"]["sha"])


def is_current(client, revision, repository_id):
    """Check that the upstream PR still wants CI for the observed head and base."""
    number, base, head = revision
    pull = client.get(f"{ROOT}/pulls/{number}")
    return (
        pull.get("number") == number
        and pull.get("state") == "open"
        and pull.get("draft") is False
        and pull["base"]["ref"] == "main"
        and pull["base"]["sha"] == base
        and pull["head"]["sha"] == head
        and (pull["head"].get("repo") or {}).get("id") == repository_id
    )


def test_failure_at(client, run_id, attempt):
    """Read GitHub job metadata. An upload, installation or report failure cannot qualify."""
    payload = client.get(f"{ROOT}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    jobs = payload["jobs"]
    require(len(jobs) == payload["total_count"], "Incomplete measurement job inventory")
    matches = [job for job in jobs if job.get("name") == MEASUREMENT_JOB]
    require(len(matches) <= 1, "Ambiguous app test job")
    if not matches or matches[0].get("conclusion") != "failure":
        return None
    steps = [step for step in matches[0]["steps"] if step.get("name") == APP_TEST_STEP]
    require(len(steps) <= 1, "Ambiguous app test step")
    if not steps or steps[0].get("conclusion") != "failure":
        return None
    return timestamp(steps[0]["completed_at"])


def eligible_candidate(run, revision, repository_id, failure_at):
    """Leave finished runs and retries started after the failure alone."""
    if run.get("status") not in ACTIVE:
        return False
    if linked_revision(run, repository_id, CANDIDATE_PATH) != revision:
        return False
    attempt = positive_id(run["run_attempt"])
    created = timestamp(run["created_at"])
    started = timestamp(run["run_started_at"]) if attempt > 1 else created
    return started <= failure_at


def cancel_failed_app_candidates(event, client):
    """Cancel matching Checks candidates using GitHub metadata from trusted main."""
    repository = event["repository"]
    if event.get("action") != "completed" or repository.get("full_name") != REPOSITORY:
        return ()
    repository_id = positive_id(repository["id"])
    observed = event["workflow_run"]
    revision = linked_revision(observed, repository_id, MEASUREMENT_PATH)
    if revision is None or observed.get("conclusion") != "failure":
        return ()
    run_id, attempt = positive_id(observed["id"]), positive_id(observed["run_attempt"])
    path = f"{ROOT}/actions/runs/{run_id}"
    run = client.get(path)
    if (
        run.get("id") != run_id
        or linked_revision(run, repository_id, MEASUREMENT_PATH) != revision
        or run.get("run_attempt") != attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
    ):
        return ()
    failure_at = test_failure_at(client, run_id, attempt)
    if failure_at is None or not is_current(client, revision, repository_id):
        return ()
    _, base, head = revision
    comparison = client.get(f"{ROOT}/compare/{base}...{head}")
    if comparison.get("behind_by") != 0 or comparison["merge_base_commit"]["sha"] != base:
        return ()
    payload = client.get(
        f"{ROOT}/actions/workflows/checks.yaml/runs?event=pull_request&head_sha={head}&per_page=100"
    )
    candidates = payload["workflow_runs"]
    require(len(candidates) == payload["total_count"], "Incomplete candidate inventory")
    cancelled = []
    for candidate in candidates:
        if not eligible_candidate(candidate, revision, repository_id, failure_at):
            continue
        candidate_id = positive_id(candidate["id"])
        candidate_path = f"{ROOT}/actions/runs/{candidate_id}"
        current = client.get(candidate_path)
        if (
            current.get("id") != candidate_id
            or current.get("run_attempt") != candidate["run_attempt"]
            or not eligible_candidate(current, revision, repository_id, failure_at)
        ):
            continue
        if not is_current(client, revision, repository_id):
            break
        try:
            client.cancel_workflow_run(REPOSITORY, candidate_id)
        except VerificationError:
            if client.get(candidate_path).get("status") != "completed":
                raise
        else:
            cancelled.append(candidate_id)
    return tuple(cancelled)


def main():
    """Report only verified candidate run IDs and fixed error messages."""
    try:
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        cancelled = cancel_failed_app_candidates(event, GitHub(environ["GITHUB_TOKEN"]))
    except (VerificationError, OSError, KeyError, TypeError, ValueError, AttributeError):
        print("Unable to verify or cancel the matching test candidate.")
        return 1
    if cancelled:
        print("Cancellation requested for Checks run(s):", *cancelled)
    else:
        print("No active Checks candidate matched a verified app test failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
