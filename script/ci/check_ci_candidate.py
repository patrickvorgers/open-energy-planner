"""Require current-head review and cheap checks before Ready or a manual candidate retry."""

import json
import re
import sys
from os import environ
from pathlib import Path

from script.ci.check_commit_signatures import GitHub, VerificationError, identity
from script.ci.pr_readiness import require_resolved_threads

REQUEST_CI_LABEL = "ci: run"
CHECKS_PER_PAGE = 100
MAX_CHECK_PAGES = 10
GITHUB_ACTIONS_APP_ID = 15368
CODERABBIT_APP_ID = 347564
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
        return current

    current = current_pr()
    file_count = changed_files(repository, number, current, client)
    checks = current_checks(repository, head, client)
    require_prerequisites(checks)
    if current.get("user", {}).get("login") != "dependabot[bot]":
        require_coderabbit_review(checks)
    require_resolved_threads(repository, number, client)
    final_count = current_pr().get("changed_files")
    if type(final_count) is not int or final_count != file_count:
        raise VerificationError("PR file count changed during admission")
    return head


def main():
    """Gate PR label requests with a read-only token. Other events use their own CI routing."""
    if environ.get("GITHUB_EVENT_NAME") != "pull_request":
        print("No PR candidate admission needed for this event.")
    else:
        try:
            event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
            head = check(event, environ["GITHUB_REPOSITORY"], GitHub(environ["GITHUB_TOKEN"]))
        except (VerificationError, KeyError, OSError, ValueError, TypeError) as error:
            print(
                f"Candidate not admitted: {error}"
                if isinstance(error, VerificationError)
                else "Candidate evidence unavailable or invalid."
            )
            return 1
        print(f"Reviewed candidate admitted: {head}, base: {event['pull_request']['base']['sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
