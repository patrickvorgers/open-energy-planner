"""Require current-head review and cheap checks before Ready or a manual candidate retry."""

import json
import sys
from os import environ
from pathlib import Path

from script.ci.check_commit_signatures import GitHub, VerificationError, identity
from script.ci.pr_readiness import require_resolved_threads

REQUEST_CI_LABEL = "ci: run"
LOCAL_RUNNER_LABEL = "ci-runner: prefer-local"
HOSTED_RUNNER_LABELS = ("ubuntu-24.04",)
LOCAL_RUNNER_LABELS = ("self-hosted", "X64")
PERSONAL_RUNNER_LABEL = "oep-ci-local"
RUNNERS_PER_PAGE = 100
MAX_RUNNER_PAGES = 10
CHECKS_PER_PAGE = 100
MAX_CHECK_PAGES = 10
GITHUB_ACTIONS_APP_ID = 15368
CODERABBIT_APP_ID = 347564


def runner_labels(event, repository):
    """Reserve optional local capacity for the private personal repository owner's own requests."""
    pull_request = event["pull_request"]
    labels = pull_request.get("labels", [])
    if not isinstance(labels, list) or any(not isinstance(label, dict) for label in labels):
        raise VerificationError("Invalid candidate runner labels")
    if not any(label.get("name") == LOCAL_RUNNER_LABEL for label in labels):
        return list(HOSTED_RUNNER_LABELS)
    owner = event.get("repository", {}).get("owner")
    author = pull_request.get("user", {}).get("login")
    sender = event.get("sender", {}).get("login")
    if (
        not isinstance(owner, dict)
        or owner.get("type") != "User"
        or not isinstance(owner.get("login"), str)
        or owner["login"].lower() != repository.split("/")[0].lower()
        or not isinstance(author, str)
        or author.lower() != owner["login"].lower()
        or not isinstance(sender, str)
        or sender.lower() != owner["login"].lower()
    ):
        return list(HOSTED_RUNNER_LABELS)
    if (
        event.get("repository", {}).get("private") is not True
        or pull_request.get("head", {}).get("repo", {}).get("full_name") != repository
    ):
        raise VerificationError("Local runners require a private same-repository PR")
    return [*LOCAL_RUNNER_LABELS, PERSONAL_RUNNER_LABEL]


def available_runner_labels(repository, requested, client):
    """Use a personal runner only when bounded metadata proves it is online and idle now."""
    if requested == list(HOSTED_RUNNER_LABELS) or client is None:
        return list(HOSTED_RUNNER_LABELS)
    seen = 0
    try:
        settings = client.get(f"repos/{repository}")
        fork_policy = client.get(
            f"repos/{repository}/actions/permissions/fork-pr-workflows-private-repos"
        )
        if (
            settings.get("private") is not True
            or settings.get("full_name") != repository
            or fork_policy.get("run_workflows_from_fork_pull_requests") is not False
        ):
            raise VerificationError("Personal runners require disabled private fork PR workflows")
        for page in range(1, MAX_RUNNER_PAGES + 1):
            response = client.get(
                f"repos/{repository}/actions/runners?per_page={RUNNERS_PER_PAGE}&page={page}"
            )
            runners = response.get("runners")
            total = response.get("total_count")
            if (
                not isinstance(runners, list)
                or type(total) is not int
                or total < 0
                or any(not isinstance(runner, dict) for runner in runners)
            ):
                raise VerificationError("Invalid runner availability metadata")
            seen += len(runners)
            if seen > total:
                raise VerificationError("Inconsistent runner availability metadata")
            for runner in runners:
                labels = runner.get("labels")
                if not isinstance(labels, list) or any(
                    not isinstance(label, dict) or not isinstance(label.get("name"), str)
                    for label in labels
                ):
                    raise VerificationError("Invalid runner label metadata")
                available = {label["name"].lower() for label in labels}
                operating_system = runner.get("os")
                if not isinstance(operating_system, str):
                    continue
                operating_system = operating_system.lower()
                dispatch_labels = [*requested, operating_system]
                if (
                    runner.get("status") == "online"
                    and runner.get("busy") is False
                    and operating_system == "linux"
                    and {label.lower() for label in dispatch_labels} <= available
                ):
                    # shortcut: availability is not reserved, add a dispatch watchdog if disconnects leave queued jobs.
                    return dispatch_labels
            if seen == total:
                break
            if not runners:
                raise VerificationError("Incomplete runner availability metadata")
    except VerificationError:
        print("Personal runner availability unavailable. Using GitHub-hosted capacity.")
    return list(HOSTED_RUNNER_LABELS)


def check(event, repository, client):
    """Reject stale requests, missing evidence and pending/failed current-head checks.

    Ready for review or the retry label requests one candidate run. Its operator
    must first address every finding, including outside-diff comments. A successful review check proves
    review completion, not the disposition of every finding. Final merge review
    must still inspect threads and the full test result on the same head.
    """
    number, base, head = identity(event, repository)
    if event.get("action") != "ready_for_review" and (
        event.get("action") != "labeled" or event.get("label", {}).get("name") != REQUEST_CI_LABEL
    ):
        raise VerificationError("Candidate CI requires Ready for review or a ci: run retry")
    requested_runners = runner_labels(event, repository)
    path = f"repos/{repository}/pulls/{number}"

    def current_pr():
        """Bind admission to the open, ready PR's original immutable base and head."""
        current = client.get(path)
        if (
            current.get("state") != "open"
            or current.get("draft") is not False
            or not isinstance(current.get("head"), dict)
            or not isinstance(current.get("base"), dict)
            or current.get("head", {}).get("sha") != head
            or current.get("base", {}).get("sha") != base
        ):
            raise VerificationError(
                "PR changed or is not ready. Request CI on the current candidate"
            )
        if runner_labels({**event, "pull_request": current}, repository) != requested_runners:
            raise VerificationError(
                "Runner choice changed. Request CI again on the intended runner"
            )
        return current

    current = current_pr()
    checks = []
    for page in range(1, MAX_CHECK_PAGES + 1):
        result = client.get(
            f"repos/{repository}/commits/{head}/check-runs?filter=latest&per_page={CHECKS_PER_PAGE}&page={page}"
        )
        rows = result.get("check_runs")
        total = result.get("total_count")
        if not isinstance(rows, list) or type(total) is not int or total < 0:
            raise VerificationError("Invalid check-run evidence")
        if any(not isinstance(row, dict) or row.get("head_sha") != head for row in rows):
            raise VerificationError("Check-run evidence does not identify this candidate")
        checks.extend(rows)
        if any(type(row.get("id")) is not int for row in checks) or len(
            {row["id"] for row in checks}
        ) != len(checks):
            raise VerificationError("Ambiguous check-run evidence")
        if len(checks) == total:
            break
        if not rows or len(checks) > total:
            raise VerificationError("Incomplete check-run evidence")
    else:
        raise VerificationError("Check-run evidence exceeds the page limit")

    required = [
        ("Quick checks", GITHUB_ACTIONS_APP_ID),
        ("GitHub Verified PR commits", GITHUB_ACTIONS_APP_ID),
    ]
    if current.get("user", {}).get("login") != "dependabot[bot]":
        required.append(("CodeRabbit", CODERABBIT_APP_ID))
    for name, app_id in required:
        matching = [
            row
            for row in checks
            if row.get("name") == name
            and isinstance(row.get("app"), dict)
            and row["app"].get("id") == app_id
            and not (app_id == GITHUB_ACTIONS_APP_ID and row.get("conclusion") == "skipped")
        ]
        if not matching or any(type(row.get("id")) is not int for row in matching):
            raise VerificationError(f"Missing current-head evidence: {name}")
        latest = max(matching, key=lambda row: row["id"])
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise VerificationError(f"Current-head check has not passed: {name}")
    require_resolved_threads(repository, number, client)
    current_pr()
    return head


def main():
    """Gate PR label requests with a read-only token. Other events use their own CI routing."""
    selected_runners = list(HOSTED_RUNNER_LABELS)
    if environ.get("GITHUB_EVENT_NAME") != "pull_request":
        print("No PR candidate admission needed for this event.")
    else:
        try:
            event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
            head = check(event, environ["GITHUB_REPOSITORY"], GitHub(environ["GITHUB_TOKEN"]))
            requested_runners = runner_labels(event, environ["GITHUB_REPOSITORY"])
            runner_token = environ.get("OEP_RUNNER_READ_TOKEN")
            runner_client = None
            if requested_runners != list(HOSTED_RUNNER_LABELS) and runner_token:
                try:
                    runner_client = GitHub(runner_token)
                except VerificationError:
                    pass
            selected_runners = available_runner_labels(
                environ["GITHUB_REPOSITORY"],
                requested_runners,
                runner_client,
            )
        except (VerificationError, KeyError, OSError, ValueError, TypeError) as error:
            print(
                f"Candidate not admitted: {error}"
                if isinstance(error, VerificationError)
                else "Candidate evidence unavailable or invalid."
            )
            return 1
        print(f"Reviewed candidate admitted: {head}, base: {event['pull_request']['base']['sha']}")
    if output := environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as destination:
            destination.write(f"runner_labels={json.dumps(selected_runners)}\n")
    print(f"Candidate runner labels: {', '.join(selected_runners)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
