"""Reuse exact verified merges with full or selected coverage and original artifact proof.

The successful quality job publishes its tested Git tree in a fixed step-name
contract. GitHub Actions job metadata owns that evidence. PR descriptions,
labels and arbitrary commit statuses cannot supply it. This is the same
owner-controlled workflow boundary as candidate admission, not protection
against repository writers who can change the workflow itself.
"""

import json
import re
import sys
from datetime import UTC, datetime
from os import environ
from pathlib import Path
from subprocess import SubprocessError

from script.ci.check_commit_signatures import GitHub, VerificationError, sha
from script.ci.proof_git import base_test_only_profile, checkout_tree

WORKFLOW_PATH = ".github/workflows/checks.yaml"
QUALITY_JOB = "Quality and integration (AMD64)"
FULL_SUITE_STEP = "Native checks, planner scenarios, app smoke and real HA handoff"
COMPLETION_STEP = "Full CI suite completion"
TREE_STEP_PREFIX = "Full CI source tree: "
TEST_TREE_STEP_PREFIX = "Test-only CI source tree: "
SELECTED_TREE_STEP_PREFIX = "Selected CI source tree: "
BUNDLE_ARTIFACT = "installation-bundle-amd64"
REPORT_ARTIFACT = "test-reports-amd64"
API_PAGE_SIZE = 100


def require(condition, message):
    """Reject incomplete or inconsistent reuse evidence with a credential-free reason."""
    if not condition:
        raise VerificationError(message)


def positive_id(value):
    """Require an actual positive API integer before inserting it into an API path."""
    require(type(value) is int and value > 0, "Invalid evidence identifier")
    return value


def timestamp(value):
    """Parse an explicit offset-aware API timestamp before comparing artifact lifetimes."""
    require(isinstance(value, str), "Missing evidence timestamp")
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "Evidence timestamp has no timezone")
    return result


def check(event, repository, commit_sha, tree_sha, client, now=None):
    """Return original run/bundle identities only after every reuse invariant passes.

    Only ordinary two-parent, GitHub-verified merges onto main qualify. Inspect
    the associated merged PR and its newest successful Checks candidate. Require
    the exact current-attempt quality job, suite completion, tested-tree marker
    and unexpired original artifact. Focused reuse additionally requires the
    first parent's independent selection and exact profile. Recheck the latest run.
    Any exception is handled by the caller as a request for the full pipeline.
    """
    require(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or "")
        and event.get("repository", {}).get("full_name") == repository
        and event.get("ref") == "refs/heads/main"
        and event.get("after") == sha(commit_sha)
        and event.get("forced") is False,
        "Not an ordinary main push",
    )
    tree_sha = sha(tree_sha)
    root = f"repos/{repository}"
    commit = client.get(f"{root}/git/commits/{commit_sha}")
    require(
        commit.get("sha") == commit_sha
        and commit.get("tree", {}).get("sha") == tree_sha
        and commit.get("verification", {}).get("verified") is True
        and commit.get("verification", {}).get("reason") == "valid",
        "Main merge identity or signature differs",
    )
    parents = commit.get("parents")
    require(isinstance(parents, list) and len(parents) == 2, "Not a two-parent merge")
    base_sha, head_sha = (sha(parent["sha"]) for parent in parents)
    associated = client.get_list(f"{root}/commits/{commit_sha}/pulls?per_page={API_PAGE_SIZE}")
    require(len(associated) < API_PAGE_SIZE, "Incomplete PR association evidence")
    matching = [
        pull
        for pull in associated
        if pull.get("merge_commit_sha") == commit_sha and pull.get("base", {}).get("ref") == "main"
    ]
    require(len(matching) == 1, "Main merge has no unique PR association")
    number = positive_id(matching[0]["number"])
    pull = client.get(f"{root}/pulls/{number}")
    require(
        pull.get("merged") is True
        and pull.get("merge_commit_sha") == commit_sha
        and pull.get("base", {}).get("ref") == "main"
        and pull.get("base", {}).get("sha") == base_sha
        and pull.get("head", {}).get("sha") == head_sha
        and all(
            pull.get(side, {}).get("repo", {}).get("full_name") == repository
            for side in ("base", "head")
        ),
        "Merged PR does not identify this main merge",
    )
    workflow = client.get(f"{root}/actions/workflows/checks.yaml")
    workflow_id = positive_id(workflow["id"])
    require(workflow.get("path") == WORKFLOW_PATH, "Unexpected candidate workflow")

    def latest_run():
        """Select the newest complete run list without hiding later failures or pending work."""
        runs = client.get(
            f"{root}/actions/workflows/checks.yaml/runs?event=pull_request"
            f"&head_sha={head_sha}&per_page={API_PAGE_SIZE}"
        )
        candidates = runs.get("workflow_runs")
        require(
            isinstance(candidates, list)
            and type(runs.get("total_count")) is int
            and runs["total_count"] == len(candidates)
            and 0 < len(candidates) <= API_PAGE_SIZE,
            "Missing or incomplete candidate run evidence",
        )
        require(
            len({positive_id(candidate["id"]) for candidate in candidates}) == len(candidates)
            and len({positive_id(candidate["run_number"]) for candidate in candidates})
            == len(candidates),
            "Ambiguous candidate run evidence",
        )
        return max(candidates, key=lambda candidate: positive_id(candidate["run_number"]))

    run = latest_run()
    run_id = positive_id(run["id"])
    attempt = positive_id(run["run_attempt"])
    build_number = positive_id(run["run_number"])

    def eligible_run(value):
        """Bind success to this exact repository, workflow, head and attempt."""
        return (
            all(
                type(value.get(field)) is int
                for field in ("id", "workflow_id", "run_attempt", "run_number")
            )
            and value.get("id") == run_id
            and value.get("workflow_id") == workflow_id
            and value.get("path") == WORKFLOW_PATH
            and value.get("head_sha") == head_sha
            and value.get("event") == "pull_request"
            and value.get("status") == "completed"
            and value.get("conclusion") == "success"
            and value.get("run_attempt") == attempt
            and value.get("run_number") == build_number
            and all(
                value.get(field, {}).get("full_name") == repository
                for field in ("repository", "head_repository")
            )
        )

    require(eligible_run(run), "Untrusted or unsuccessful candidate run")
    jobs = client.get(
        f"{root}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page={API_PAGE_SIZE}"
    )
    rows = jobs.get("jobs")
    require(
        isinstance(rows, list)
        and type(jobs.get("total_count")) is int
        and jobs["total_count"] == len(rows) <= API_PAGE_SIZE,
        "Incomplete candidate job evidence",
    )
    quality = [job for job in rows if job.get("name") == QUALITY_JOB]
    require(len(quality) == 1, "Missing or ambiguous quality job")
    quality = quality[0]
    require(
        quality.get("status") == "completed"
        and quality.get("conclusion") == "success"
        and quality.get("run_id") == run_id
        and quality.get("run_attempt") == attempt
        and quality.get("head_sha") == head_sha,
        "Quality job did not pass this candidate attempt",
    )
    steps = quality.get("steps")
    require(isinstance(steps, list), "Missing candidate suite step evidence")
    tree_steps = [
        step
        for step in steps
        if step.get("status") == "completed"
        and step.get("conclusion") == "success"
        and step.get("name", "").startswith(
            (TREE_STEP_PREFIX, TEST_TREE_STEP_PREFIX, SELECTED_TREE_STEP_PREFIX)
        )
    ]
    require(len(tree_steps) == 1, "Missing or ambiguous tested tree proof")
    test_only = tree_steps[0]["name"].startswith(TEST_TREE_STEP_PREFIX)
    focused_product = tree_steps[0]["name"].startswith(SELECTED_TREE_STEP_PREFIX)
    focused = test_only or focused_product
    profile = (
        base_test_only_profile(base_sha, commit_sha)
        if test_only
        else base_test_only_profile(base_sha, commit_sha, selector="candidate_reuse_profile")
        if focused_product
        else None
    )
    require(
        not focused or profile is not None, "Immutable base policy does not prove focused coverage"
    )
    suite_step = "Selected native and integration suites" if focused else FULL_SUITE_STEP
    completion_step = "Selected CI suite completion" if focused else COMPLETION_STEP
    tree_step = (
        f"{TEST_TREE_STEP_PREFIX}{tree_sha} ({profile})"
        if test_only
        else f"{SELECTED_TREE_STEP_PREFIX}{tree_sha} ({profile})"
        if focused_product
        else TREE_STEP_PREFIX + tree_sha
    )
    if not focused and tree_steps[0]["name"] != tree_step:
        performance = base_test_only_profile(
            base_sha, commit_sha, selector="performance_queries_required"
        )
        proven_profile = tree_steps[0]["name"].removeprefix(TREE_STEP_PREFIX + tree_sha + " (")
        require(
            proven_profile in {"full)", "full+performance-queries)"},
            "Candidate suite or identical tested tree not proven",
        )
        require(
            performance is not True or proven_profile == "full+performance-queries)",
            "Immutable base policy requires query performance evidence",
        )
        tree_step = TREE_STEP_PREFIX + tree_sha + " (" + proven_profile
    matched_steps = []
    for name in (suite_step, completion_step, tree_step):
        matching_steps = [step for step in steps if step.get("name") == name]
        require(
            len(matching_steps) == 1
            and matching_steps[0].get("status") == "completed"
            and matching_steps[0].get("conclusion") == "success",
            "Candidate suite or identical tested tree not proven",
        )
        matched_steps.append(matching_steps[0])
    require(
        positive_id(matched_steps[2].get("number"))
        > positive_id(matched_steps[1].get("number"))
        > positive_id(matched_steps[0].get("number")),
        "Tree proof did not follow suite completion",
    )
    artifacts = client.get(f"{root}/actions/runs/{run_id}/artifacts?per_page={API_PAGE_SIZE}")
    rows = artifacts.get("artifacts")
    require(
        isinstance(rows, list)
        and type(artifacts.get("total_count")) is int
        and artifacts["total_count"] == len(rows) <= API_PAGE_SIZE,
        "Incomplete artifact evidence",
    )

    def original_artifact(artifact_name):
        """Require an original artifact created during this successful candidate attempt."""
        matching_artifacts = [
            artifact for artifact in rows if artifact.get("name") == artifact_name
        ]
        require(len(matching_artifacts) == 1, f"Missing or ambiguous original {artifact_name}")
        artifact = matching_artifacts[0]
        artifact_id = positive_id(artifact["id"])
        require(
            artifact.get("expired") is False
            and timestamp(artifact.get("expires_at")) > (now or datetime.now(UTC))
            and timestamp(quality.get("started_at"))
            <= timestamp(artifact.get("created_at"))
            <= timestamp(quality.get("completed_at"))
            and artifact.get("workflow_run", {}).get("id") == run_id
            and artifact.get("workflow_run", {}).get("head_sha") == head_sha
            and isinstance(artifact.get("digest"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]),
            f"Original {artifact_name} is expired or belongs to another candidate attempt",
        )
        return artifact_id

    artifact_id = original_artifact(REPORT_ARTIFACT if test_only else BUNDLE_ARTIFACT)
    report_id = original_artifact(REPORT_ARTIFACT) if focused_product else None
    require(
        eligible_run(latest_run()),
        "Candidate changed while reading its evidence",
    )
    result = {
        "reuse": "true",
        "tested_run_id": str(run_id),
        "tested_tree": tree_sha,
    }
    if test_only:
        return {**result, "test_only_profile": profile, "test_report_artifact_id": str(artifact_id)}
    return {
        **result,
        "installation_artifact_id": str(artifact_id),
        "installation_build": f"{build_number}.{attempt}",
        **(
            {"candidate_profile": profile, "test_report_artifact_id": str(report_id)}
            if focused_product
            else {}
        ),
    }


def main():
    """Keep full checks on any missing evidence and publish only validated reuse identities."""
    result = {"reuse": "false"}
    if environ.get("GITHUB_EVENT_NAME") == "push":
        try:
            event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
            commit_sha = sha(environ["GITHUB_SHA"])
            tree_sha = checkout_tree(commit_sha)
            result = check(
                event,
                environ["GITHUB_REPOSITORY"],
                commit_sha,
                tree_sha,
                GitHub(environ["GITHUB_TOKEN"]),
            )
        except VerificationError as error:
            print(f"Full main CI retained: {error}.")
        except (
            OSError,
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            IndexError,
            SubprocessError,
        ):
            print(
                "Full main CI retained. Exact successful candidate and required artifact not proven."
            )
    else:
        print("Full candidate, manual and release checks retained.")
    with Path(environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write("".join(f"{name}={value}\n" for name, value in result.items()))
    summary = "Full checks required. No candidate evidence was reused.\n"
    if result["reuse"] == "true":
        run_url = (
            f"https://github.com/{environ['GITHUB_REPOSITORY']}/actions/runs/"
            f"{result['tested_run_id']}"
        )
        if "test_only_profile" in result:
            summary = (
                f"Identical test-only Git tree: `{result['tested_tree']}`.\n\n"
                f"Immutable base policy confirms `{result['test_only_profile']}`. "
                f"Reuse [successful candidate run {result['tested_run_id']}]({run_url}) and its "
                f"[original test reports]({run_url}/artifacts/{result['test_report_artifact_id']}). "
                "No installation bundle or deployment is authorized by this evidence.\n"
            )
        else:
            summary = (
                f"Identical tested Git tree: `{result['tested_tree']}`.\n\n"
                f"Reuse [successful candidate run {result['tested_run_id']}]({run_url}) and its "
                f"[original installation bundle {result['installation_build']}]"
                f"({run_url}/artifacts/{result['installation_artifact_id']}). "
                "No new installation build was created.\n"
                + (
                    f"Immutable base policy confirms focused coverage `{result['candidate_profile']}`. "
                    "This is exact selected-suite evidence, not full validation or deployment authorization.\n"
                    if "candidate_profile" in result
                    else ""
                )
            )
        print(summary.strip())
    with Path(environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
