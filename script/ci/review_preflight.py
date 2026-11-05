"""Check a frozen Draft before a person requests one CodeRabbit review. Never post or spend credits."""

import json
import re
from os import environ
from pathlib import Path

from script.ci.check_ci_candidate import (
    CODERABBIT_APP_ID,
    changed_files,
    current_checks,
    latest_check,
    require_coderabbit_review,
    require_included_base,
    require_prerequisites,
)
from script.ci.check_commit_signatures import GitHub, VerificationError, sha
from script.ci.pr_readiness import require_resolved_threads

MAX_PAGES = 10


def require_pr_history(repository, number, current, client):
    """Require complete bounded PR history containing the frozen head."""
    expected = current.get("commits")
    if type(expected) is not int or not 1 <= expected <= 250:
        raise VerificationError("A complete PR history of at most 250 commits is required")
    commits = set()
    for page in range(1, 4):
        rows = client.get_list(
            f"repos/{repository}/pulls/{number}/commits?per_page=100&page={page}"
        )
        if not isinstance(rows, list) or not rows or len(rows) > 100:
            raise VerificationError("Incomplete PR commit evidence")
        for row in rows:
            commit = sha(row.get("sha") if isinstance(row, dict) else None)
            if commit in commits:
                raise VerificationError("Duplicate PR commit evidence")
            commits.add(commit)
        if len(commits) == expected:
            if current["head"]["sha"] not in commits:
                raise VerificationError("Frozen head is not in this PR history")
            return
        if len(commits) > expected or len(rows) < 100:
            raise VerificationError("PR commit count does not match its history")
    raise VerificationError("PR commit evidence exceeds the page limit")


def require_no_request(repository, number, head, client):
    """Reject an existing same-head manual command from a trusted repository participant."""
    marker = f"<!-- oep-coderabbit-head:{head} -->"
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        rows = client.get_list(
            f"repos/{repository}/issues/{number}/comments?per_page=100&page={page}"
        )
        if not isinstance(rows, list) or len(rows) > 100:
            raise VerificationError("Invalid review-request history")
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or row["id"] in seen
                or not isinstance(row.get("body"), str)
            ):
                raise VerificationError("Incomplete or duplicate review-request history")
            seen.add(row["id"])
            user = row.get("user")
            trusted = (
                isinstance(user, dict)
                and user.get("type") == "User"
                and row.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"}
            )
            lines = [line.strip() for line in row["body"].splitlines()]
            if (
                trusted
                and marker in lines
                and any(line.lower() == "@coderabbitai review" for line in lines)
            ):
                raise VerificationError("A manual CodeRabbit request already exists for this head")
        if len(rows) < 100:
            return
    raise VerificationError("Review-request history exceeds the page limit")


def preflight(repository, number, head, base, client):
    """Require current prerequisites and identity before emitting a manual command, without API writes."""
    if (
        not isinstance(repository, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository)
        or type(number) is not int
        or number <= 0
    ):
        raise VerificationError("Invalid review request identity")
    head, base = sha(head), sha(base)
    path = f"repos/{repository}/pulls/{number}"

    def current_pr():
        """Read an open Draft against main without checking out or executing contributor fork code."""
        current = client.get(path)
        if (
            current.get("state") != "open"
            or current.get("draft") is not True
            or any(not isinstance(current.get(side), dict) for side in ("head", "base", "user"))
            or any(not isinstance(current[side].get("repo"), dict) for side in ("head", "base"))
            or current.get("head", {}).get("sha") != head
            or current.get("base", {}).get("sha") != base
            or not isinstance(current["head"]["repo"].get("full_name"), str)
            or not re.fullmatch(
                r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", current["head"]["repo"]["full_name"]
            )
            or current.get("base", {}).get("repo", {}).get("full_name") != repository
            or current.get("base", {}).get("ref") != "main"
            or current.get("user", {}).get("login") == "dependabot[bot]"
        ):
            raise VerificationError(
                "Review preflight requires the selected open Draft against main"
            )
        main_object = client.get(f"repos/{repository}/git/ref/heads/main").get("object")
        if not isinstance(main_object, dict) or main_object.get("sha") != base:
            raise VerificationError(
                "Selected base is not the live main tip. Update the PR and retest"
            )
        return current

    current = current_pr()
    require_included_base(repository, head, base, client)
    file_count = changed_files(repository, number, current, client)
    require_pr_history(repository, number, current, client)
    checks = current_checks(repository, head, client)
    require_prerequisites(checks)
    review = latest_check(checks, "CodeRabbit", CODERABBIT_APP_ID)
    if review:
        if review.get("status") != "completed":
            raise VerificationError("CodeRabbit is already reviewing this head")
        try:
            require_coderabbit_review(checks)
        except VerificationError:
            pass
        else:
            raise VerificationError("CodeRabbit already completed an actual review of this head")
    require_resolved_threads(repository, number, client)
    require_no_request(repository, number, head, client)
    final = current_pr()
    if (
        type(final.get("changed_files")) is not int
        or final["changed_files"] != file_count
        or type(final.get("commits")) is not int
        or final["commits"] != current.get("commits")
    ):
        raise VerificationError("PR changed during review preflight")
    return (
        f"## Manual CodeRabbit review preflight\n\nPR: {number}\n\n"
        f"Head: `{head}`\n\nBase: `{base}`\n\n"
        f"Changed files: {file_count}\n\n"
        "The operator attests that existing findings are dispositioned. "
        "This is not machine proof of their correctness. CodeRabbit provides the independent review. "
        "No review has been requested.\n\n"
        "Keep the head frozen, coordinate the shared queue and verify actual provider availability. "
        "Recheck this head immediately before posting the following comment manually:\n\n"
        f"```text\n@coderabbitai review\n<!-- oep-coderabbit-head:{head} -->\n```\n\n"
        "A successful preflight does not reserve quota or prevent another person from posting. "
        "Never retry automatically. A rate-limited request needs deliberate operator handling.\n"
    )


def main():
    """Accept only a trusted-main dispatch with an affirmative finding-disposition attestation."""
    try:
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        repository = environ["GITHUB_REPOSITORY"]
        if not isinstance(event, dict) or not isinstance(event.get("repository"), dict):
            raise VerificationError("Invalid workflow event")
        if (
            environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environ.get("GITHUB_REF") != "refs/heads/main"
            or event.get("repository", {}).get("default_branch") != "main"
            or event.get("repository", {}).get("full_name") != repository
            or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository)
        ):
            raise VerificationError("Review preflight requires a trusted main workflow dispatch")
        sha(environ["GITHUB_SHA"])
        inputs = event["inputs"]
        if not isinstance(inputs, dict):
            raise VerificationError("Invalid workflow inputs")
        if (
            inputs.get("findings_dispositioned") is not True
            and inputs.get("findings_dispositioned") != "true"
        ):
            raise VerificationError("Existing finding disposition must be attested")
        number = inputs["pull_request"]
        if not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", number):
            raise VerificationError("Invalid pull request number")
        summary = preflight(
            repository,
            int(number),
            sha(inputs["expected_head"]),
            sha(inputs["expected_base"]),
            GitHub(environ["GITHUB_TOKEN"]),
        )
        Path(environ["GITHUB_STEP_SUMMARY"]).write_text(summary, encoding="utf-8")
    except VerificationError as error:
        print(f"Review preflight failed: {error}. No review was requested.")
        return 1
    except (KeyError, TypeError, ValueError, OSError):
        print(
            "Review preflight failed. No review was requested. Check current identities and evidence."
        )
        return 1
    print(
        "Review preflight passed. No review was requested. Follow the manual command in the summary."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
