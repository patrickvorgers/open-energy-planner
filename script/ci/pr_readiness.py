"""Keep changed PRs in Draft and verify resolved discussions before candidate admission."""

import json
from os import environ
from pathlib import Path

from script.ci.check_commit_signatures import GitHub, VerificationError, identity

MAX_THREAD_PAGES = 10
FORMAL_REVIEW_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state headRefOid baseRefOid reviewDecision
      latestOpinionatedReviews(first: 100, after: $cursor, writersOnly: false) {
        totalCount
        nodes { id state author { __typename login ... on Node { id } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
THREAD_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes { id isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
DRAFT_MUTATION = """
mutation($id: ID!) {
  convertPullRequestToDraft(input: {pullRequestId: $id}) {
    pullRequest { id isDraft headRefOid }
  }
}
"""


def require_clear_formal_reviews(repository, number, head, base, client):
    """Reject effective formal objections, independent of thread and provider completion.

    Paginate GitHub's own latest opinionated review per actor on the frozen open
    revision, including non-writers. A later APPROVED can supersede that actor's
    change request. COMMENTED, resolved threads and successful checks cannot.
    Missing or duplicate actor identities fail closed. No review is mutated.
    """
    owner, name = repository.split("/")
    cursor = None
    cursors = set()
    seen = set()
    reviewers = set()
    expected_count = None
    states = {"PENDING", "COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
    for _ in range(MAX_THREAD_PAGES):
        response = client.post(
            "graphql",
            {
                "query": FORMAL_REVIEW_QUERY,
                "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
            },
        )
        try:
            if response.get("errors"):
                raise VerificationError("Formal review evidence unavailable")
            current = response["data"]["repository"]["pullRequest"]
            if (
                current["state"] != "OPEN"
                or current["headRefOid"] != head
                or current["baseRefOid"] != base
            ):
                raise VerificationError("PR changed during formal review verification")
            decision = current["reviewDecision"]
            if decision not in {None, "APPROVED", "REVIEW_REQUIRED", "CHANGES_REQUESTED"}:
                raise VerificationError("Invalid formal review decision")
            connection = current["latestOpinionatedReviews"]
            rows, page, total = (
                connection["nodes"],
                connection["pageInfo"],
                connection["totalCount"],
            )
            if (
                type(total) is not int
                or total < 0
                or (expected_count is not None and total != expected_count)
            ):
                raise VerificationError("Formal review count changed or is invalid")
            expected_count = total
            if not isinstance(rows, list) or len(rows) > 100:
                raise VerificationError("Invalid formal review evidence")
            for review in rows:
                if (
                    not isinstance(review, dict)
                    or not isinstance(review.get("id"), str)
                    or not review["id"]
                    or review["id"] in seen
                    or review.get("state") not in states
                ):
                    raise VerificationError("Invalid or duplicate formal review evidence")
                seen.add(review["id"])
                author = review.get("author")
                if (
                    not isinstance(author, dict)
                    or author.get("__typename") not in {"User", "Bot"}
                    or not isinstance(author.get("id"), str)
                    or not author["id"]
                    or author["id"] in reviewers
                    or not isinstance(author.get("login"), str)
                    or not author["login"]
                ):
                    raise VerificationError("Missing or ambiguous formal reviewer identity")
                reviewers.add(author["id"])
                if review["state"] == "CHANGES_REQUESTED":
                    raise VerificationError(
                        "Outstanding CHANGES_REQUESTED review. "
                        "Keep Draft and follow the individual review-disposition procedure"
                    )
            if decision == "CHANGES_REQUESTED":
                raise VerificationError("GitHub still reports CHANGES_REQUESTED")
            if page["hasNextPage"] is False:
                if len(seen) != total:
                    raise VerificationError("Incomplete formal review history")
                return
            cursor = page["endCursor"]
            if (
                page["hasNextPage"] is not True
                or not rows
                or len(seen) >= total
                or not isinstance(cursor, str)
                or not cursor
                or cursor in cursors
            ):
                raise VerificationError("Incomplete formal review history")
            cursors.add(cursor)
        except (KeyError, TypeError) as error:
            raise VerificationError("Invalid formal review evidence") from error
    raise VerificationError("Formal reviews exceed the page limit")


def require_resolved_threads(repository, number, client):
    """Reject unresolved discussions, including outdated ones, without interpreting comment text.

    Ready for review is the operator's attestation that all findings, including
    outside-diff findings without a GitHub resolution flag, were evaluated.
    Resolved discussions prove acknowledgement, not semantic correctness.
    """
    owner, name = repository.split("/")
    cursor = None
    seen = set()
    for _ in range(MAX_THREAD_PAGES):
        response = client.post(
            "graphql",
            {
                "query": THREAD_QUERY,
                "variables": {
                    "owner": owner,
                    "name": name,
                    "number": number,
                    "cursor": cursor,
                },
            },
        )
        try:
            if response.get("errors"):
                raise VerificationError("Review threads unavailable")
            connection = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            rows = connection["nodes"]
            page = connection["pageInfo"]
            if not isinstance(rows, list) or any(
                not isinstance(row, dict)
                or row.get("isResolved") is not True
                or not isinstance(row.get("id"), str)
                or row["id"] in seen
                for row in rows
            ):
                raise VerificationError("Open or invalid review threads remain")
            identifiers = {row["id"] for row in rows}
            if len(identifiers) != len(rows):
                raise VerificationError("Duplicate review thread evidence")
            seen.update(identifiers)
            if page["hasNextPage"] is False:
                return
            if (
                page["hasNextPage"] is not True
                or not rows
                or not isinstance(page["endCursor"], str)
                or not page["endCursor"]
                or page["endCursor"] == cursor
            ):
                raise VerificationError("Incomplete review thread evidence")
            cursor = page["endCursor"]
        except (KeyError, TypeError) as error:
            raise VerificationError("Invalid review thread evidence") from error
    raise VerificationError("Review threads exceed the page limit")


def reset_changed_pr(event, repository, client):
    """Return a new/revised open PR to Draft using only trusted metadata and exact event identities."""
    number, base, head = identity(event, repository)
    action = event.get("action")
    if action not in {"opened", "synchronize", "reopened", "edited"}:
        return False
    if action == "edited" and not event.get("changes", {}).get("base", {}).get("ref"):
        return False
    current = client.get(f"repos/{repository}/pulls/{number}")
    if current.get("state") not in {"open", "closed"} or not all(
        isinstance(current.get(side), dict) for side in ("base", "head")
    ):
        raise VerificationError("Invalid PR readiness metadata")
    if current.get("state") != "open":
        return False
    if current.get("head", {}).get("sha") != head or current.get("base", {}).get("sha") != base:
        return False
    node_id = current.get("node_id")
    if type(current.get("draft")) is not bool or not isinstance(node_id, str) or not node_id:
        raise VerificationError("Invalid PR readiness metadata")
    # The existing dependency-review exception has no automated Draft review phase.
    if current.get("user", {}).get("login") == "dependabot[bot]":
        print(
            f"PR {number} retains its Dependabot readiness state. Assess compatibility and "
            "regression coverage, then explicitly request candidate CI with ci: run."
        )
        return False
    changed = not current["draft"]
    if changed:
        response = client.post("graphql", {"query": DRAFT_MUTATION, "variables": {"id": node_id}})
        try:
            result = response["data"]["convertPullRequestToDraft"]["pullRequest"]
            if (
                response.get("errors")
                or not isinstance(result, dict)
                or result.get("id") != node_id
                or result.get("isDraft") is not True
            ):
                raise VerificationError("Draft transition was not confirmed")
        except (KeyError, TypeError) as error:
            raise VerificationError("Draft transition unavailable") from error
        print(
            f"PR {number} returned to Draft. Finish current-head review before choosing Ready for review."
        )
    return changed


def main():
    """Execute only immutable base code from the trusted PR metadata workflow."""
    try:
        if environ.get("GITHUB_EVENT_NAME") != "pull_request_target":
            raise VerificationError("Readiness updates require trusted pull_request_target code")
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        reset_changed_pr(event, environ["GITHUB_REPOSITORY"], GitHub(environ["GITHUB_TOKEN"]))
    except VerificationError as error:
        print(f"PR readiness failed: {error}. No candidate was admitted by this workflow.")
        return 1
    except (KeyError, TypeError, ValueError, OSError):
        print("PR readiness metadata unavailable. No candidate was admitted by this workflow.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
