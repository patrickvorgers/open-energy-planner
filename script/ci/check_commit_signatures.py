"""Require GitHub-verified signatures on every commit introduced by a PR."""

import json
import re
import sys
from http.client import HTTPException
from os import environ
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from script.ci.proof_git import VerificationError, fetch_pr_history, introduced_commits, sha


class NoRedirects(HTTPRedirectHandler):
    """Prevent authenticated GitHub API requests from following redirects to another endpoint."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        """Refuse all redirect requests instead of forwarding installation credentials."""
        return


class GitHub:
    """Read GitHub signature metadata and fetch PR objects without executing PR-controlled code."""

    def __init__(self, token):
        """Require a nonempty single-line token and configure a redirect-refusing HTTP client."""
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise VerificationError("A read-only GitHub token is required")
        self._token = token
        self._opener = build_opener(NoRedirects())

    def get(self, path):
        """Read bounded object JSON with a deadline, hiding tokens and provider bodies on failure."""
        result = self._read(path)
        if not isinstance(result, dict):
            raise VerificationError("Invalid GitHub verification response")
        return result

    def get_list(self, path):
        """Read bounded JSON lists through the same authenticated, redirect-refusing transport."""
        result = self._read(path)
        if not isinstance(result, list):
            raise VerificationError("Invalid GitHub verification response")
        return result

    def post(self, path, payload):
        """Send bounded API JSON without redirects or exposing credentials on failure."""
        result = self._read(path, payload)
        if not isinstance(result, dict):
            raise VerificationError("Invalid GitHub API response")
        return result

    def _read(self, path, payload=None):
        """Decode one bounded JSON response without exposing credentials or provider errors."""
        request = Request(
            "https://api.github.com/" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                if response.status != 200:
                    raise VerificationError("GitHub verification request failed")
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    raise VerificationError("GitHub verification response exceeds limit")
                result = json.loads(data)
                return result
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, HTTPException) as error:
            # Never print token-bearing requests, response bodies or provider exception text.
            if isinstance(error, VerificationError):
                raise
            raise VerificationError("GitHub verification unavailable or invalid") from None

    def fetch_pr_history(self, repository, number):
        """Fetch immutable PR objects through the reviewed Git metadata owner."""
        fetch_pr_history(repository, number, self._token)


def identity(event, repository):
    """Validate repository/PR identity and return its number plus immutable base/head SHAs."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        raise VerificationError("Invalid repository identity")
    try:
        number = event["number"]
        base = sha(event["pull_request"]["base"]["sha"])
        head = sha(event["pull_request"]["head"]["sha"])
        event_repository = event["repository"]["full_name"]
    except (KeyError, TypeError):
        raise VerificationError("A pull request event is required") from None
    if type(number) is not int or number <= 0 or event_repository != repository:
        raise VerificationError("Pull request identity does not match this repository")
    return number, base, head


def check(event, repository, client, enumerate_commits=introduced_commits):
    """Require GitHub verification for every introduced commit and recheck the PR range.

    Reject incomplete histories, unavailable metadata and base/head/count changes.
    GitHub verifies the signatures, while this function applies the all-commits policy.
    """
    number, base, head = identity(event, repository)
    path = f"repos/{repository}/pulls/{number}"

    def confirm_head():
        """Require the current API base/head to match the event and return the commit count."""
        current = client.get(path)
        if (
            not isinstance(current.get("head"), dict)
            or not isinstance(current.get("base"), dict)
            or current.get("head", {}).get("sha") != head
            or current.get("base", {}).get("sha") != base
        ):
            raise VerificationError("PR changed during verification. Rerun on the current revision")
        count = current.get("commits")
        if type(count) is not int or count < 1:
            raise VerificationError("GitHub did not provide a valid introduced commit count")
        return count

    expected_count = confirm_head()
    commits = enumerate_commits(base, head)
    # Validate even injected enumerators. Never let an incomplete list pass.
    if (
        not commits
        or len(commits) != expected_count
        or commits[-1] != head
        or len(commits) != len(set(commits))
    ):
        raise VerificationError("Incomplete PR commit enumeration")
    for commit_sha in commits:
        commit_sha = sha(commit_sha)
        result = client.get(f"repos/{repository}/git/commits/{commit_sha}")
        verification = result.get("verification")
        if (
            result.get("sha") != commit_sha
            or not isinstance(verification, dict)
            or verification.get("verified") is not True
            or verification.get("reason") != "valid"
        ):
            raise VerificationError(f"Commit {commit_sha} is not GitHub Verified")
    if confirm_head() != expected_count:
        raise VerificationError("PR commit count changed during verification")
    return len(commits)


def main():
    """Verify trusted target-event commits and return a failing exit code for unavailable evidence."""
    try:
        if environ.get("GITHUB_EVENT_NAME") != "pull_request_target":
            raise VerificationError("Run this check from trusted pull_request_target code")
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        repository = environ["GITHUB_REPOSITORY"]
        number, _, _ = identity(event, repository)
        client = GitHub(environ["GITHUB_TOKEN"])
        client.fetch_pr_history(repository, number)
        count = check(event, repository, client)
    except VerificationError as error:
        print(f"PR signature verification failed: {error}")
        return 1
    except (OSError, KeyError, ValueError, TypeError):
        print("PR signature verification failed or is unavailable. Inspect PR commits and rerun.")
        return 1
    print(f"All {count} introduced PR commits are GitHub Verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
