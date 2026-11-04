"""Require current-head review and cheap checks before Ready or a manual candidate retry."""

import json
import re
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
KODY_APP_ID = 413034
CODEQL_APP_ID = 57789
MAX_PR_FILES = 150
PREREQUISITE_CHECKS = (
    ("Quick checks", GITHUB_ACTIONS_APP_ID),
    ("GitHub Verified PR commits", GITHUB_ACTIONS_APP_ID),
    ("CodeQL", CODEQL_APP_ID),
)


def changed_files(repository, number, current, client):
    """Require a complete inventory of at most 150 changed files, without filtering file kinds."""
    count = current.get("changed_files")
    if type(count) is not int or not 1 <= count <= MAX_PR_FILES:
        raise VerificationError("PR must contain between 1 and 150 changed files. Split larger PRs")
    names = set()
    for page in range(1, 3):
        rows = client.get_list(f"repos/{repository}/pulls/{number}/files?per_page=100&page={page}")
        if not isinstance(rows, list) or not rows or len(rows) > 100:
            raise VerificationError("Incomplete changed-file evidence")
        for row in rows:
            name = row.get("filename") if isinstance(row, dict) else None
            if not isinstance(name, str) or not name or name in names:
                raise VerificationError("Invalid or duplicate changed-file evidence")
            names.add(name)
        if len(names) == count:
            return count
        if len(names) > count or len(rows) < 100:
            raise VerificationError("Changed-file count does not match its inventory")
    raise VerificationError("Changed-file evidence exceeds the page limit")


def current_checks(repository, head, client):
    """Read complete, bounded check evidence tied to exactly one immutable head."""
    checks = []
    for page in range(1, MAX_CHECK_PAGES + 1):
        result = client.get(
            f"repos/{repository}/commits/{head}/check-runs?filter=all&per_page={CHECKS_PER_PAGE}&page={page}"
        )
        rows = result.get("check_runs")
        total = result.get("total_count")
        if (
            not isinstance(rows, list)
            or len(rows) > CHECKS_PER_PAGE
            or type(total) is not int
            or total < 0
            or any(not isinstance(row, dict) or row.get("head_sha") != head for row in rows)
        ):
            raise VerificationError("Invalid or stale check-run evidence")
        checks.extend(rows)
        if any(type(row.get("id")) is not int or row["id"] <= 0 for row in checks) or len(
            {row["id"] for row in checks}
        ) != len(checks):
            raise VerificationError("Ambiguous check-run evidence")
        if len(checks) == total:
            return checks
        if not rows or len(checks) > total:
            raise VerificationError("Incomplete check-run evidence")
    raise VerificationError("Check-run evidence exceeds the page limit")


def latest_check(checks, name, app_id):
    """Select the service's newest evidence, preserving head proof across completed metadata skips."""
    matching = [
        row
        for row in checks
        if row.get("name") == name
        and isinstance(row.get("app"), dict)
        and row["app"].get("id") == app_id
        and not (
            row.get("conclusion") == "skipped"
            and (
                app_id == GITHUB_ACTIONS_APP_ID
                or (app_id == CODERABBIT_APP_ID and row.get("status") == "completed")
            )
        )
    ]
    return max(matching, key=lambda row: row["id"]) if matching else None


def require_prerequisites(checks):
    """Require successful current-head lint, complete signature verification and CodeQL."""
    for name, app_id in PREREQUISITE_CHECKS:
        latest = latest_check(checks, name, app_id)
        if not latest:
            raise VerificationError(f"Missing current-head evidence: {name}")
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise VerificationError(f"Current-head check has not passed: {name}")


def require_coderabbit_review(checks):
    """Reject green skipped/rate-limited results without completed substantive review phases."""
    latest = latest_check(checks, "CodeRabbit", CODERABBIT_APP_ID)
    if not latest or latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise VerificationError("Current-head check has not passed: CodeRabbit")
    output = latest.get("output")
    if not isinstance(output, dict) or output.get("title") != "Finished":
        raise VerificationError("CodeRabbit did not complete an actual review")
    summary = output.get("summary")
    if not isinstance(summary, str) or not re.match(
        r"\AResult: Success - Review completed(?:\r?\n|\Z)", summary
    ):
        raise VerificationError("CodeRabbit review completion is unavailable")
    required = {
        "Prepare review scope",
        "Prepare repository workspace",
        "Run analysis checks",
        "Generate review feedback",
        "Publish review results",
    }
    phases = {}
    for line in summary.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] in required:
            if cells[0] in phases:
                raise VerificationError("Ambiguous CodeRabbit review phases")
            phases[cells[0]] = cells[1]
    if set(phases) != required or any(status != "Completed" for status in phases.values()):
        raise VerificationError(
            "CodeRabbit scope, workspace, analysis or feedback did not complete"
        )


def require_automated_review(checks):
    """Accept substantive current-head CodeRabbit or Kody evidence from the actual provider app.

    Provider completion does not dispose of findings. The caller still checks all
    review threads, and the operator must evaluate outside-diff feedback too.
    """
    try:
        require_coderabbit_review(checks)
        return
    except VerificationError as error:
        coderabbit_error = str(error)
    latest = latest_check(checks, "Kody Code Review", KODY_APP_ID)
    if latest and latest.get("status") == "completed" and latest.get("conclusion") == "success":
        output = latest.get("output")
        if (
            isinstance(output, dict)
            and output.get("title") == "Code Review Complete"
            and output.get("summary")
            == "Review finished successfully. Suggestions (if any) were posted as PR/file comments."
        ):
            return
    raise VerificationError(
        f"No completed current-head CodeRabbit or Kody-ai review. CodeRabbit: {coderabbit_error}"
    )


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
    file_count = changed_files(repository, number, current, client)
    checks = current_checks(repository, head, client)
    require_prerequisites(checks)
    if current.get("user", {}).get("login") != "dependabot[bot]":
        require_automated_review(checks)
    require_resolved_threads(repository, number, client)
    final_count = current_pr().get("changed_files")
    if type(final_count) is not int or final_count != file_count:
        raise VerificationError("PR file count changed during admission")
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
