"""Read current formal review, discussion and provider gates before Ready and again before merge.

This trusted-main workflow never dismisses a review or grants approval. Finding
disposition remains the existing operator attestation, including outside-diff
findings. A successful snapshot does not replace the expected-head merge guard.
"""

import json
from os import environ
from pathlib import Path

from script.ci.check_ci_candidate import (
    current_checks,
    require_automated_review,
    require_included_base,
    require_prerequisites,
    require_repowise_disposition,
)
from script.ci.check_commit_signatures import GitHub, VerificationError, sha
from script.ci.pr_readiness import require_clear_formal_reviews, require_resolved_threads


def check(repository, number, head, base, client):
    """Recheck a frozen open PR without changing readiness, reviews, comments or CI.

    The formal-review reader checks live head/base on every page. Repeat it after
    the other evidence reads so a new formal objection or revision fails closed.
    Merge callers must still verify candidate proof and recheck immediately
    before the guarded merge. No GitHub API offers an atomic review/base lock.
    """
    if type(number) is not int or number <= 0:
        raise VerificationError("Invalid PR number")
    head, base = sha(head), sha(base)
    require_clear_formal_reviews(repository, number, head, base, client)
    current = client.get(f"repos/{repository}/pulls/{number}")
    if (
        current.get("state") != "open"
        or current.get("head", {}).get("sha") != head
        or current.get("base", {}).get("sha") != base
        or current.get("base", {}).get("ref") != "main"
        or current.get("base", {}).get("repo", {}).get("full_name") != repository
    ):
        raise VerificationError("Review-state check requires the frozen open PR against main")
    require_included_base(repository, head, base, client)
    checks = current_checks(repository, head, client)
    require_prerequisites(checks)
    if current.get("user", {}).get("login") != "dependabot[bot]":
        require_automated_review(checks, repository, number, head, client)
        require_repowise_disposition(checks, repository, number, current, client)
    require_resolved_threads(repository, number, client)
    require_clear_formal_reviews(repository, number, head, base, client)


def main():
    """Accept only an explicit trusted-main dispatch, using a read-only workflow token."""
    try:
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        repository = environ["GITHUB_REPOSITORY"]
        if (
            environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environ.get("GITHUB_REF") != "refs/heads/main"
            or repository != "patrickvorgers/open-energy-planner"
            or event["repository"]["full_name"] != repository
            or event["repository"]["default_branch"] != "main"
        ):
            raise VerificationError("Review-state checks require an upstream trusted-main dispatch")
        sha(environ["GITHUB_SHA"])
        inputs = event["inputs"]
        number = int(inputs["pull_request"])
        head, base = sha(inputs["expected_head"]), sha(inputs["expected_base"])
        check(repository, number, head, base, GitHub(environ["GITHUB_TOKEN"]))
        Path(environ["GITHUB_STEP_SUMMARY"]).write_text(
            f"## Review-state snapshot\n\nPR: {number}\n\nHead: `{head}`\n\nBase: `{base}`\n\n"
            "No outstanding formal change requests or unresolved threads were found. "
            "Current-head prerequisites, provider completion and applicable RepoWise disposition passed. "
            "The operator must still evaluate every finding, including outside-diff findings. "
            "No review was dismissed and no approval was created. "
            "Recheck immediately before Ready and before merge. "
            "This snapshot is not candidate or merge evidence.\n",
            encoding="utf-8",
        )
    except VerificationError as error:
        print(f"Review-state check failed: {error}. No review or PR state was changed.")
        return 1
    except (KeyError, TypeError, ValueError, OSError):
        print("Review-state evidence unavailable or invalid. No review or PR state was changed.")
        return 1
    print("Review-state snapshot passed. No review or PR state was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
