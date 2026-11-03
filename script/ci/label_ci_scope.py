"""Publish advisory PR scope labels using trusted base policy without executing PR code."""

import json
import sys
from http.client import HTTPException
from os import environ
from pathlib import Path
from subprocess import SubprocessError
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from script.ci.check_ci_candidate import REQUEST_CI_LABEL
from script.ci.check_commit_signatures import GitHub, VerificationError, identity
from script.ci.ci_scope import matrix_profile, select_profile, selected_suites
from script.ci.proof_git import merged_tree

SCOPE_PREFIX = "ci-scope: "
PERFORMANCE_SCOPE_LABEL = SCOPE_PREFIX + "performance-queries"
DEFAULT_SCOPE_COLOR = "1d76db"
PERFORMANCE_SCOPE_COLOR = "d500f9"
API_TIMEOUT_SECONDS = 20


class LabelWriteError(VerificationError):
    """Retain only a safe HTTP status when GitHub rejects a label mutation."""

    def __init__(self, status):
        """Exclude provider bodies and token-bearing requests from diagnostics."""
        super().__init__(f"Scope label update rejected with HTTP {status}")
        self.status = status


class LabelClient(GitHub):
    """Reuse bounded authenticated reads and add only the label writer's required methods."""

    def write(self, method, path, body=None):
        """Send bounded JSON mutations without printing private provider errors or credentials."""
        request = Request(
            "https://api.github.com/" + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=API_TIMEOUT_SECONDS) as response:
                if response.status not in (200, 201, 204):
                    raise VerificationError("Unexpected scope label response")
        except HTTPError as error:
            raise LabelWriteError(error.code) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise VerificationError("Scope label service unavailable") from None


def merged_scope(base, head):
    """Classify the intended merged tree rather than reverting intervening changes on main."""
    try:
        tree = merged_tree(base, head, ROOT)
    except (OSError, SubprocessError, VerificationError):
        return "full"
    return select_profile(base, tree, ROOT)


def update(event, repository, client):
    """Replace only scope labels for the current PR range, preserving request and user labels."""
    number, base, head = identity(event, repository)
    path = f"repos/{repository}/pulls/{number}"

    def current():
        """Refuse stale jobs before each PR mutation, including base retargeting and closure."""
        document = client.get(path)
        if (
            document.get("state") != "open"
            or not isinstance(document.get("base"), dict)
            or not isinstance(document.get("head"), dict)
            or document.get("base", {}).get("sha") != base
            or document.get("head", {}).get("sha") != head
        ):
            return None
        return document

    if current() is None:
        return None
    client.fetch_pr_history(repository, number)
    profile = merged_scope(base, head)
    suites = selected_suites(profile)
    functional_profile = matrix_profile(profile)
    planned_labels = (
        {
            SCOPE_PREFIX + ("frontend" if suite == "unit-frontend" else suite)
            for suite in suites - {"ci", "performance-queries"}
        }
        if functional_profile.startswith("selected:")
        else {SCOPE_PREFIX + functional_profile}
    ) or {SCOPE_PREFIX + "ci"}
    if "performance-queries" in suites:
        planned_labels.discard(SCOPE_PREFIX + "ci")
        planned_labels.add(PERFORMANCE_SCOPE_LABEL)
    document = current()
    if document is None:
        return None
    labels = {item["name"] for item in document["labels"]}
    missing_labels = sorted(planned_labels - labels)
    for label in missing_labels:
        color = PERFORMANCE_SCOPE_COLOR if label == PERFORMANCE_SCOPE_LABEL else DEFAULT_SCOPE_COLOR
        try:
            client.write(
                "POST",
                f"repos/{repository}/labels",
                {
                    "name": label,
                    "color": color,
                    "description": "Planned test selection for the current PR revision. Not a test result.",
                },
            )
        except LabelWriteError as error:
            if error.status != 422:
                raise
            existing = client.get(f"repos/{repository}/labels/{quote(label, safe='')}")
            if existing.get("name") != label:
                raise
            if label == PERFORMANCE_SCOPE_LABEL and existing.get("color") != color:
                client.write(
                    "PATCH", f"repos/{repository}/labels/{quote(label, safe='')}", {"color": color}
                )
    if PERFORMANCE_SCOPE_LABEL in planned_labels and PERFORMANCE_SCOPE_LABEL not in missing_labels:
        label_path = f"repos/{repository}/labels/{quote(PERFORMANCE_SCOPE_LABEL, safe='')}"
        existing = client.get(label_path)
        if existing.get("name") != PERFORMANCE_SCOPE_LABEL:
            raise VerificationError("Unexpected performance scope label")
        if existing.get("color") != PERFORMANCE_SCOPE_COLOR:
            client.write("PATCH", label_path, {"color": PERFORMANCE_SCOPE_COLOR})
    if missing_labels:
        if current() is None:
            return None
        client.write(
            "POST", f"repos/{repository}/issues/{number}/labels", {"labels": missing_labels}
        )
    for previous in sorted(labels - planned_labels):
        if previous.startswith(SCOPE_PREFIX):
            if current() is None:
                return None
            try:
                client.write(
                    "DELETE",
                    f"repos/{repository}/issues/{number}/labels/{quote(previous, safe='')}",
                )
            except LabelWriteError as error:
                if error.status != 404:
                    raise
    return profile


def main():
    """Run only from immutable trusted base code in the label-only privileged workflow."""
    try:
        if environ.get("GITHUB_EVENT_NAME") != "pull_request_target":
            raise VerificationError("Scope labels require trusted pull_request_target code")
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if (
            event.get("action") == "labeled"
            and event.get("label", {}).get("name") != REQUEST_CI_LABEL
        ):
            print("Unrelated label event ignored.")
            return 0
        profile = update(event, environ["GITHUB_REPOSITORY"], LabelClient(environ["GITHUB_TOKEN"]))
    except LabelWriteError as error:
        print(
            f"Scope label API write rejected with HTTP {error.status}. "
            "Labels never replace candidate validation."
        )
        return 1
    except (VerificationError, KeyError, OSError, TypeError, ValueError):
        print("Unable to publish CI scope. Labels never replace candidate validation.")
        return 1
    print(f"CI scope label: {profile}" if profile else "PR changed. No stale scope was published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
