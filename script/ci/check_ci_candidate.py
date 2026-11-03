"""Require current-head review and cheap checks before Ready or a manual candidate retry."""

import json
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
