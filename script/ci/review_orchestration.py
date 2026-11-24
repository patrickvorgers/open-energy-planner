"""Orchestrate explicit PR review requests using trusted metadata and durable PR comments.

Only an authorized ``/oep-reviewer review`` starts work. Provider callbacks and scheduled
checks resume existing requests. This controller never admits CI, resolves
findings, marks a PR Ready or executes contributor code.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from os import environ
from pathlib import Path

from script.ci.check_ci_candidate import (
    CODERABBIT_APP_ID,
    CODEX_APP_ID,
    CODEX_BOT_ID,
    GREPTILE_APP_ID,
    GREPTILE_BOT_ID,
    KODY_APP_ID,
    changed_files,
    codex_review_state,
    current_checks,
    greptile_completed_check,
    latest_check,
    provider_comment,
    require_automated_review,
    require_included_base,
    require_prerequisites,
    review_comments,
)
from script.ci.check_commit_signatures import GitHub, VerificationError, sha
from script.ci.pr_readiness import require_clear_formal_reviews, require_resolved_threads
from script.ci.review_preflight import (
    GITHUB_ACTIONS_BOT_ID,
    GITHUB_ACTIONS_BOT_LOGIN,
    preflight,
)

REVIEW_COMMAND = "/oep-reviewer review"
UPSTREAM_REPOSITORY = "patrickvorgers/open-energy-planner"
STATE_PREFIX = "<!-- oep-review-state:"
MAX_PAGES = 10
MAX_QUOTA_QUERIES = 8
MAX_CODERABBIT_REQUESTS = 2
REPLY_TIMEOUT = timedelta(minutes=15)
REVIEW_TIMEOUT = timedelta(hours=2)
REQUEST_TIMEOUT = timedelta(days=7)
TERMINAL = {"complete", "blocked", "superseded"}
PHASES = TERMINAL | {"sending", "quota", "coderabbit", "greptile", "kody", "codex", "waiting"}
PUBLIC_FAILURE_MESSAGES = {
    "requester": "The original review request or its requester authority could not be verified. Check the request comment and the requester's current PR authorship or repository write access, then request review again.",
    "publisher": "The provider command account could not be verified. Ask a repository administrator to check the OEP_REVIEW_COMMAND_TOKEN secret and its repository access before requesting review again.",
    "blocked": "Review prerequisites could not be verified. Inspect the current checks and resolved findings before requesting review again.",
    "sending": "Command publication could not be confirmed. Inspect the PR request markers before any retry.",
    "quota": "CodeRabbit availability could not be verified. Inspect the quota reply and workflow before any retry.",
    "coderabbit": "CodeRabbit completion could not be verified. Inspect its check and findings before any retry.",
    "kody": "Kody completion could not be verified. Inspect its check and findings before any retry.",
    "greptile": "Greptile completion could not be verified. Inspect its check, published summary and findings before any retry.",
    "codex": "Codex completion could not be verified. Inspect its published code-review state and account allowance before any retry.",
    "waiting": "CodeRabbit retry evidence could not be verified. Inspect the reported retry time before any retry.",
    "retry": "CodeRabbit supplied no usable retry time. Inspect its quota reply before requesting another review.",
}
PROVIDER_BOT_LOGINS = (
    "coderabbitai[bot]",
    "greptile-apps[bot]",
    "kody-ai[bot]",
    "chatgpt-codex-connector[bot]",
)
GREPTILE_CREDIT_DENIAL = (
    r"Your organization has run out of review credits\. "
    r"\[Add credits\]\(https://app\.greptile\.com/-/switch-tenant\?"
    r"tenant=[a-zA-Z0-9_-]+&next=%2Fsettings%2Fbilling\) to resume code reviews\.\n\n"
    r"<!-- greptile-credit-pause:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12} -->\n?"
)
PROVIDER_COMMAND = (
    r"(?m)^@(?:coderabbitai|kody|codex|greptileai|greptile)"
    r"(?:\s+(?:review(?: this draft| --force)?|start-review|rate limit))?\s*$"
)
ADD_COMMENT = """
mutation($subject: ID!, $body: String!, $key: String!) {
  addComment(input: {subjectId: $subject, body: $body, clientMutationId: $key}) {
    commentEdge { node { id databaseId body createdAt } }
  }
}
"""
UPDATE_COMMENT = """
mutation($id: ID!, $body: String!) {
  updateIssueComment(input: {id: $id, body: $body}) { issueComment { id body } }
}
"""


class CommandAccountError(VerificationError):
    """Reject an unavailable or unauthorized user credential without exposing its response."""


class RequesterAuthorityError(VerificationError):
    """Identify a missing or unverifiable original request without exposing API details."""


def require_command_account(repository, state, client, publisher):
    """Verify a separate user credential and keep one account across a durable request."""
    try:
        if (
            publisher is None
            or publisher is client
            or (state["sequence"] > 0 and "publisher_id" not in state)
        ):
            raise VerificationError("A separate command credential is required")
        user = publisher.get("user")
        if (
            user.get("type") != "User"
            or type(user.get("id")) is not int
            or user["id"] <= 0
            or not repository_writer(repository, user, client)
            or state.get("publisher_id", user["id"]) != user["id"]
        ):
            raise VerificationError("The command account is not authorized")
    except VerificationError:
        raise CommandAccountError("Provider command account could not be verified") from None
    return user["id"]


def instant(value):
    """Parse a timezone-aware wire timestamp and normalize it to UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise VerificationError("Review evidence requires aware timestamps")
    return parsed.astimezone(UTC)


def is_bot(comment, login):
    """Trust provider identities from GitHub metadata rather than comment content."""
    user = comment.get("user", {})
    return isinstance(user, dict) and user.get("login") == login and user.get("type") == "Bot"


def comments_for(repository, number, client):
    """Read complete bounded comment history, rejecting duplicate or incomplete metadata."""
    return review_comments(repository, number, client)


def read_state(comments):
    """Load the latest controller-owned state without trusting contributor-authored markers."""
    states = []
    for comment in comments:
        if not comment["body"].startswith(STATE_PREFIX):
            continue
        if (
            not is_bot(comment, GITHUB_ACTIONS_BOT_LOGIN)
            or comment["user"].get("id") != GITHUB_ACTIONS_BOT_ID
        ):
            continue
        raw, separator, _ = comment["body"][len(STATE_PREFIX) :].partition(" -->\n")
        if not separator or len(raw) > 4000:
            raise VerificationError("Invalid review state envelope")
        state = json.loads(raw)
        if (
            not isinstance(state, dict)
            or state.get("phase") not in PHASES
            or state.get("target") not in {"quota", "coderabbit", "greptile", "kody", "codex"}
            or any(
                type(state.get(field)) is not int or state[field] < 0
                for field in (
                    "request",
                    "consumed",
                    "sequence",
                    "command",
                    "checks_after",
                    "queries",
                    "reviews",
                )
            )
            or type(state.get("kody_used")) is not bool
            or any(
                type(state.get(field, False)) is not bool
                for field in ("greptile_used", "codex_used")
            )
            or state["queries"] > MAX_QUOTA_QUERIES
            or state["reviews"] > MAX_CODERABBIT_REQUESTS
            or state["consumed"] < state["request"]
            or (
                "feedback_digest" in state
                and (
                    not isinstance(state["feedback_digest"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", state["feedback_digest"]) is None
                )
            )
            or (
                "publisher_id" in state
                and (type(state["publisher_id"]) is not int or state["publisher_id"] <= 0)
            )
        ):
            raise VerificationError("Invalid durable review state")
        state.setdefault("greptile_used", False)
        state.setdefault("codex_used", False)
        if "greptile_credit" in state:
            evidence = state["greptile_credit"]
            if (
                state["target"] != "codex"
                or not isinstance(evidence, list)
                or len(evidence) != 3
                or any(type(value) is not int or value <= 0 for value in evidence[:2])
                or evidence[0] <= evidence[1]
                or not isinstance(evidence[2], str)
                or instant(evidence[2]) > instant(state["sent"])
            ):
                raise VerificationError("Invalid durable Greptile credit evidence")
        sha(state["head"])
        sha(state["base"])
        for field in ("started", "sent"):
            instant(state[field])
        if state["resume"] is not None:
            instant(state["resume"])
        state["node_id"] = comment["node_id"]
        states.append((comment["id"], state))
    return max(states, key=lambda item: item[0])[1] if states else None


def save_state(state, subject, client):
    """Persist an intent before sending and retain one readable status comment per request."""
    stored = {key: value for key, value in state.items() if key != "node_id"}
    body = (
        f"{STATE_PREFIX}{json.dumps(stored, sort_keys=True)} -->\n"
        '## <img src="https://raw.githubusercontent.com/patrickvorgers/open-energy-planner/'
        "39f6ee5f559df8a5b493884fd3b5a4ce93394188/"
        'custom_components/open_energy_planner/brand/icon.png" '
        'alt="OEP logo" width="24" height="24"> OEP review\n\n'
        f"Revision: `{state['head']}`\n\n"
        f"Status: {state['phase'].capitalize()}\n\n{state['message']}\n\n"
        "This request does not mark the PR Ready or start candidate CI. "
        "All review findings still require evaluation and disposition."
    )
    if state.get("node_id"):
        response = client.post(
            "graphql",
            {"query": UPDATE_COMMENT, "variables": {"id": state["node_id"], "body": body}},
        )
        if response.get("errors"):
            raise VerificationError("Review status update was not confirmed")
        try:
            result = response["data"]["updateIssueComment"]["issueComment"]
        except (KeyError, TypeError):
            raise VerificationError("Review status update was not confirmed") from None
        if (
            not isinstance(result, dict)
            or result.get("id") != state["node_id"]
            or result.get("body") != body
        ):
            raise VerificationError("Review status update was not confirmed")
    else:
        result = add_comment(subject, body, f"oep-review-state-{state['request']}", client)
        state["node_id"] = result["id"]


def block_request(state, subject, client, category=None):
    """Publish only an owned bounded failure category, never an exception's arbitrary value."""
    message = PUBLIC_FAILURE_MESSAGES.get(
        category or state["phase"], PUBLIC_FAILURE_MESSAGES["blocked"]
    )
    state.update(phase="blocked", message=message)
    save_state(state, subject, client)
    return False


def record_publication(state, identifier, created, subject, client):
    """Keep the in-memory outbox pending until its durable confirmation is acknowledged."""
    confirmed = {
        **state,
        "phase": state["target"],
        "command": identifier,
        "sent": created,
        "message": f"Waiting for {state['target']}. Request comment: {identifier}.",
    }
    save_state(confirmed, subject, client)
    state.update(confirmed)


def add_comment(subject, body, key, client):
    """Create a fixed-purpose PR comment and verify the mutation without automatic retries."""
    response = client.post(
        "graphql",
        {"query": ADD_COMMENT, "variables": {"subject": subject, "body": body, "key": key}},
    )
    if response.get("errors"):
        raise VerificationError("Review command publication was not confirmed")
    try:
        result = response["data"]["addComment"]["commentEdge"]["node"]
    except (KeyError, TypeError):
        raise VerificationError("Review command publication was not confirmed") from None
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("id"), str)
        or type(result.get("databaseId")) is not int
        or result.get("body") != body
        or not isinstance(result.get("createdAt"), str)
    ):
        raise VerificationError("Review command publication was not confirmed")
    try:
        instant(result["createdAt"])
    except (ValueError, TypeError):
        raise VerificationError("Review command publication was not confirmed") from None
    return result


def quota_reply(body, created):
    """Recognize observed CodeRabbit availability sentences, never inferring quota from errors."""
    available = (
        "Reviews are available now." in body
        and "Your [plan](https://docs.coderabbit.ai/management/plans) includes PR reviews" in body
    )
    limited = "You're currently rate limited" in body or "Rate limit exceeded" in body
    wait = re.search(r"(?:available in|Please wait)\s+(\d+)\s+(seconds?|minutes?|hours?)", body)
    if available and not limited:
        return "available", None
    if limited and not available:
        if not wait:
            return "limited", None
        amount = int(wait[1])
        seconds = amount * {"second": 1, "minute": 60, "hour": 3600}[wait[2].rstrip("s")]
        if not 0 < seconds <= REQUEST_TIMEOUT.total_seconds():
            raise VerificationError("Invalid provider retry interval")
        return "limited", created + timedelta(seconds=seconds)
    if (
        available
        or limited
        or re.search(r"\b(?:quota|rate.limit|allowance|availability)\b", body, re.IGNORECASE)
    ):
        return "unknown", None
    return "unrelated", None


def provider_active(checks):
    """Recognize current-head in-flight provider work before any additional command."""
    return any(
        row.get("app", {}).get("id")
        in {CODERABBIT_APP_ID, GREPTILE_APP_ID, KODY_APP_ID, CODEX_APP_ID}
        and row.get("status") != "completed"
        for row in checks
    )


def greptile_fallback_target():
    """Select the next provider from the trusted workflow's temporary repository setting."""
    return "codex" if environ.get("OEP_GREPTILE_PAUSED") == "true" else "greptile"


def require_paused_greptile_fallback(checks, checks_after, availability_reply):
    """Keep a paused fallback tied to a CodeRabbit refusal or timeout and no Greptile work."""
    if any(row.get("app", {}).get("id") == GREPTILE_APP_ID for row in checks):
        raise VerificationError("Greptile already has check evidence for this revision")
    if availability_reply is None:
        check = latest_check(checks, "CodeRabbit", CODERABBIT_APP_ID)
        if (
            not check
            or check["id"] <= checks_after
            or check.get("status") != "completed"
            or check.get("conclusion") != "timed_out"
        ):
            raise VerificationError("CodeRabbit timeout evidence changed before fallback")


def greptile_credit_reply(row, command, sent):
    """Recognize only the observed unedited refusal from the real Greptile app."""
    return (
        row["id"] > command
        and provider_comment(row, "greptile-apps[bot]", GREPTILE_BOT_ID, GREPTILE_APP_ID)
        and re.fullmatch(GREPTILE_CREDIT_DENIAL, row["body"]) is not None
        and isinstance(row.get("updated_at"), str)
        and instant(row["created_at"]) >= instant(sent)
        and instant(row["created_at"]) == instant(row.get("updated_at"))
    )


def require_credit_evidence(evidence, comments, *, published_id=None):
    """Revalidate the exact refusal recorded before sending or recovering a Codex command."""
    identifier, command, sent = evidence
    matches = [row for row in comments if greptile_credit_reply(row, command, sent)]
    if len(matches) != 1 or matches[0]["id"] != identifier:
        raise VerificationError("Greptile credit evidence changed before Codex confirmation")
    if any(
        row["id"] > command and row["id"] != published_id and provider_command_comment(row)
        for row in comments
    ):
        raise VerificationError("Another provider command makes this request ambiguous")


def provider_command_comment(row):
    """Detect a provider command outside this controller's current publication."""
    return not is_bot(row, GITHUB_ACTIONS_BOT_LOGIN) and re.search(PROVIDER_COMMAND, row["body"])


def require_recovery_feedback(state, comments, published):
    """Allow a new authenticated same-head progress summary after an already published command."""
    if feedback_digest(comments) == state["feedback_digest"]:
        return
    codex = codex_review_state(comments, state["head"])
    if (
        codex
        and codex.get("status") == "in_progress"
        and codex.get("has_any_findings", False) is False
        and codex.get("has_unresolved_findings", False) is False
    ):
        progress = [
            row
            for row in comments
            if row["id"] > published["id"]
            and provider_comment(row, "chatgpt-codex-connector[bot]", CODEX_BOT_ID, CODEX_APP_ID)
            and row["body"].startswith("<!-- codex-pull-request-review-summary -->\n")
            and instant(row["created_at"]) >= instant(published["created_at"])
        ]
        if (
            len(progress) == 1
            and feedback_digest([row for row in comments if row["id"] != progress[0]["id"]])
            == state["feedback_digest"]
        ):
            return
    raise VerificationError("Provider feedback changed during Codex recovery")


def feedback_digest(comments):
    """Fingerprint bounded provider publications without storing their bodies in durable state.

    Include edit metadata and content so an older comment edited in place cannot
    bypass the guard, even within GitHub's timestamp resolution. Missing or
    inconsistent publication timestamps require inspection rather than fallback.
    """
    digest = sha256()
    for comment in sorted(comments, key=lambda row: row["id"]):
        if not any(is_bot(comment, login) for login in PROVIDER_BOT_LOGINS):
            continue
        try:
            created = instant(comment["created_at"])
            updated = instant(comment["updated_at"])
        except (KeyError, TypeError, ValueError):
            raise VerificationError("Provider comment edit evidence is unavailable") from None
        if updated < created:
            raise VerificationError("Provider comment edit evidence is inconsistent")
        digest.update(
            json.dumps(
                [comment["id"], comment["body"], created.isoformat(), updated.isoformat()]
            ).encode("utf-8")
        )
    return digest.hexdigest()


def require_no_unclassified_feedback(repository, number, state, comments, client):
    """Stop technical fallback when new provider feedback needs operator evaluation.

    A terminal check is not proof that no partial review or outside-diff finding
    was published. Do not infer disposition from comment text or resolved flags.
    """
    if state.get("feedback_digest") != feedback_digest(comments):
        raise VerificationError("Provider feedback needs evaluation before any fallback")
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        rows = client.get_list(
            f"repos/{repository}/pulls/{number}/reviews?per_page=100&page={page}"
        )
        if not isinstance(rows, list) or len(rows) > 100:
            raise VerificationError("Invalid provider review history")
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] in seen:
                raise VerificationError("Incomplete or duplicate provider review history")
            seen.add(row["id"])
            if row.get("commit_id") == state["head"] and any(
                is_bot(row, login) for login in PROVIDER_BOT_LOGINS
            ):
                try:
                    submitted = instant(row["submitted_at"])
                except (KeyError, ValueError, TypeError):
                    raise VerificationError("Provider review publication is uncertain") from None
                if submitted >= instant(state["sent"]):
                    raise VerificationError(
                        "Published provider review needs evaluation before fallback"
                    )
        if len(rows) < 100:
            return
    raise VerificationError("Provider review history exceeds the page limit")


def kody_request_command(checks):
    """Select a Kody command from validated current-head provider checks.

    Returns:
        The default ``@kody start-review`` command when Kody has no evidence or
        only automatic-disabled skips. Exact merge-only skips select
        ``@kody review --force``.

    Raises:
        VerificationError: Kody has active, completed review or unrecognized
            skip evidence that requires inspection before another request.
    """
    command = "@kody start-review"
    for check in checks:
        if check.get("app", {}).get("id") != KODY_APP_ID:
            continue
        output = check.get("output") or {}
        if (
            check.get("name") != "Kody Code Review"
            or check.get("status") != "completed"
            or check.get("conclusion") != "skipped"
            or output.get("title") != "Code Review Skipped"
        ):
            raise VerificationError("Kody already has evidence for this revision. Inspect it first")
        summary = output.get("summary")
        if summary == "Only Merge Commits (All new commits identified as merge commits)":
            command = "@kody review --force"
        elif (
            summary
            != "Automated Review is disabled — Enable 'Automated Code Review' in General Settings"
        ):
            raise VerificationError("Kody already has evidence for this revision. Inspect it first")
    return command


def require_revision(repository, number, state, client):
    """Recheck the live Draft, current main, ancestry and cheap gates before each provider command."""
    current = client.get(f"repos/{repository}/pulls/{number}")
    if (
        current.get("state") != "open"
        or current.get("draft") is not True
        or current.get("head", {}).get("sha") != state["head"]
        or current.get("base", {}).get("sha") != state["base"]
        or current.get("base", {}).get("ref") != "main"
        or current.get("base", {}).get("repo", {}).get("full_name") != repository
        or current.get("user", {}).get("login") == "dependabot[bot]"
        or client.get(f"repos/{repository}/git/ref/heads/main").get("object", {}).get("sha")
        != state["base"]
    ):
        raise VerificationError("Frozen Draft or main revision changed")
    require_included_base(repository, state["head"], state["base"], client)
    changed_files(repository, number, current, client)
    checks = current_checks(repository, state["head"], client)
    require_prerequisites(checks)
    return current, checks


def command_marker(state):
    """Identify one durable provider action across worker restarts and uncertain HTTP responses."""
    return f"<!-- oep-review-command:{state['request']}:{state['sequence']} -->"


def require_previous_feedback(state, comments, target, availability_reply):
    """Validate the preceding command's evidence before acknowledging one known quota reply.

    Only that exact, newly published reply may differ from the previous digest.
    Never absorb concurrent partial feedback into the next command's baseline.
    """
    previous_comments = comments
    if availability_reply is not None:
        if (
            state["phase"] not in {"quota", "coderabbit"}
            or not is_bot(availability_reply, "coderabbitai[bot]")
            or availability_reply not in comments
            or availability_reply["id"] <= state["command"]
            or instant(availability_reply["created_at"]) < instant(state["sent"])
            or "<!-- This is an auto-generated reply by CodeRabbit -->"
            not in availability_reply["body"]
        ):
            raise VerificationError("Validated CodeRabbit availability reply changed")
        availability, _ = quota_reply(
            availability_reply["body"], instant(availability_reply["created_at"])
        )
        if (
            availability not in {"available", "limited"}
            or {
                "quota": availability,
                "coderabbit": "available",
                "greptile": "limited",
                "codex": "limited" if greptile_fallback_target() == "codex" else None,
            }.get(target)
            != availability
        ):
            raise VerificationError("CodeRabbit availability does not authorize this command")
        previous_comments = [row for row in comments if row["id"] != availability_reply["id"]]
    if state["sequence"] > 0 and state.get("feedback_digest") != feedback_digest(previous_comments):
        raise VerificationError("Provider feedback changed since the preceding command")


def send_command(
    repository,
    number,
    state,
    target,
    subject,
    client,
    now,
    publisher=None,
    *,
    availability_reply=None,
    credit_reply=None,
):
    """Record an outbox intent, recheck identities and publish at most one command for that intent."""
    _, checks = require_revision(repository, number, state, client)
    require_resolved_threads(repository, number, client)
    if provider_active(checks):
        raise VerificationError("A provider is still working on this revision")
    comments = comments_for(repository, number, client)
    previous_sent, previous_command, previous_checks = (
        state["sent"],
        state["command"],
        state["checks_after"],
    )
    paused_codex = target == "codex" and state["phase"] in {"quota", "coderabbit"}
    if paused_codex and (
        greptile_fallback_target() != "codex"
        or state["greptile_used"]
        or any(
            f"<!-- oep-greptile-head:{state['head']} -->" in row["body"].splitlines()
            for row in comments
        )
    ):
        raise VerificationError("The Greptile pause does not supersede existing review work")
    credit_evidence = None
    if credit_reply is not None:
        if state["phase"] != "greptile" or target != "codex":
            raise VerificationError("Greptile credit evidence does not authorize this command")
        credit_evidence = [credit_reply["id"], state["command"], state["sent"]]
        require_credit_evidence(credit_evidence, comments)
        require_no_unclassified_feedback(
            repository, number, state, [row for row in comments if row != credit_reply], client
        )
        require_clear_formal_reviews(repository, number, state["head"], state["base"], client)
        if greptile_completed_check(checks):
            raise VerificationError("Greptile review publication is still possible")
    else:
        require_previous_feedback(state, comments, target, availability_reply)
        if paused_codex:
            require_paused_greptile_fallback(checks, previous_checks, availability_reply)
            require_no_unclassified_feedback(
                repository,
                number,
                state,
                [row for row in comments if row != availability_reply],
                client,
            )
            require_clear_formal_reviews(repository, number, state["head"], state["base"], client)
    codex = codex_review_state(comments, state["head"])
    if codex and codex.get("status") != "completed":
        raise VerificationError("Codex already has uncompleted work for this revision")
    require_original_requester(repository, number, state, client)
    publisher_id = require_command_account(repository, state, client, publisher)
    if target == "quota":
        if state["queries"] >= MAX_QUOTA_QUERIES:
            raise VerificationError("Quota checks reached the bounded request limit")
        state["queries"] += 1
        command = "@coderabbitai rate limit"
    elif target == "coderabbit":
        if state["reviews"] >= MAX_CODERABBIT_REQUESTS:
            raise VerificationError("CodeRabbit attempts reached the bounded request limit")
        state["reviews"] += 1
        command = f"@coderabbitai review\n<!-- oep-coderabbit-head:{state['head']} -->"
    elif target == "kody":
        if state["kody_used"]:
            raise VerificationError("Kody was already requested for this revision")
        kody_command = kody_request_command(checks)
        state["kody_used"] = True
        command = f"{kody_command}\n<!-- oep-kody-head:{state['head']} -->"
    elif target in {"greptile", "codex"}:
        if state[f"{target}_used"]:
            raise VerificationError("This provider was already requested for this revision")
        if any(
            f"<!-- oep-{target}-head:{state['head']} -->" in row["body"].splitlines()
            for row in comments
        ):
            raise VerificationError("This provider already has a same-head request")
        state[f"{target}_used"] = True
        provider_command = (
            "@greptileai review this draft" if target == "greptile" else "@codex review"
        )
        command = f"{provider_command}\n<!-- oep-{target}-head:{state['head']} -->"
    else:
        raise VerificationError("Unknown review provider")
    state.update(
        phase="sending",
        publisher_id=publisher_id,
        target=target,
        sequence=state["sequence"] + 1,
        checks_after=max((row["id"] for row in checks), default=0),
        command=0,
        sent=now.isoformat(),
        message=f"Publishing the {target} request.",
        feedback_digest=feedback_digest(comments),
    )
    if credit_evidence is not None:
        state["greptile_credit"] = credit_evidence
    save_state(state, subject, client)
    _, final_checks = require_revision(repository, number, state, client)
    require_resolved_threads(repository, number, client)
    if provider_active(final_checks):
        raise VerificationError("A provider started before command publication")
    final_comments = comments_for(repository, number, client)
    final_codex = codex_review_state(final_comments, state["head"])
    if final_codex and final_codex.get("status") != "completed":
        raise VerificationError("Codex started before command publication")
    if feedback_digest(final_comments) != state["feedback_digest"]:
        raise VerificationError("Provider feedback changed before command publication")
    if credit_evidence is not None:
        require_credit_evidence(credit_evidence, final_comments)
        if greptile_completed_check(final_checks):
            raise VerificationError("Greptile review publication is still possible")
    if credit_evidence is not None or paused_codex:
        if paused_codex:
            require_paused_greptile_fallback(final_checks, previous_checks, availability_reply)
            if any(
                row["id"] > previous_command and provider_command_comment(row)
                for row in final_comments
            ):
                raise VerificationError("Another provider command makes this request ambiguous")
        require_no_unclassified_feedback(
            repository, number, {**state, "sent": previous_sent}, final_comments, client
        )
        require_clear_formal_reviews(repository, number, state["head"], state["base"], client)
    if target == "kody" and kody_request_command(final_checks) != kody_command:
        raise VerificationError("Kody request evidence changed before publication")
    require_original_requester(repository, number, state, client)
    require_command_account(repository, state, client, publisher)
    result = add_comment(
        subject, command + "\n" + command_marker(state), command_marker(state), publisher
    )
    record_publication(state, result["databaseId"], result["createdAt"], subject, client)


def github_user(user):
    """Admit a concrete GitHub user identity without trusting comment association labels."""
    return (
        isinstance(user, dict)
        and user.get("type") == "User"
        and type(user.get("id")) is int
        and user["id"] > 0
        and isinstance(user.get("login"), str)
        and re.fullmatch(r"[A-Za-z0-9-]+", user["login"]) is not None
    )


def repository_writer(repository, user, client):
    """Require live upstream write authority independently of PR authorship."""
    if repository != UPSTREAM_REPOSITORY or not github_user(user):
        return False
    permission = client.get(f"repos/{repository}/collaborators/{user['login']}/permission")
    if permission.get("permission") in {"admin", "maintain", "write"}:
        return True
    if permission.get("permission") in {"read", "triage", "none"}:
        return False
    raise VerificationError("Repository permission could not be verified")


def requester_authorized(repository, number, command, client):
    """Allow a user to review their own upstream PR or exercise live upstream write authority."""
    user = command.get("user")
    if (
        repository != UPSTREAM_REPOSITORY
        or type(number) is not int
        or number <= 0
        or not github_user(user)
    ):
        return False
    current = client.get(f"repos/{repository}/pulls/{number}")
    return requester_authorized_on_pr(repository, command, current, client)


def requester_authorized_on_pr(repository, command, current, client):
    """Check a concrete requester against one PR snapshot and live repository permissions."""
    user = command.get("user")
    if repository != UPSTREAM_REPOSITORY or not github_user(user):
        return False
    base = current.get("base")
    if (
        not isinstance(base, dict)
        or not isinstance(base.get("repo"), dict)
        or base["repo"].get("full_name") != UPSTREAM_REPOSITORY
        or base.get("ref") != "main"
    ):
        return False
    author = current.get("user")
    return (github_user(author) and author["id"] == user["id"]) or repository_writer(
        repository, user, client
    )


def require_requester(repository, number, command, client):
    """Require authorship of this upstream PR or current repository write authority."""
    if not requester_authorized(repository, number, command, client):
        raise VerificationError(
            "Review request requires this upstream PR's author or repository write permission"
        )


def require_original_requester(repository, number, state, client):
    """Recheck the original explicit request before another credential publishes on its behalf."""
    try:
        commands = [
            row
            for row in comments_for(repository, number, client)
            if row["id"] == state["request"] and row["body"].strip() == REVIEW_COMMAND
        ]
        if len(commands) != 1:
            raise VerificationError("The original explicit review request is unavailable")
        require_requester(repository, number, commands[0], client)
    except VerificationError:
        raise RequesterAuthorityError(
            "Original requester authority could not be verified"
        ) from None


def start_request(repository, number, command, client, now, publisher=None):
    """Authorize a fresh exact command and reuse the existing frozen-review preflight."""
    require_requester(repository, number, command, client)
    current = client.get(f"repos/{repository}/pulls/{number}")
    head, base = sha(current["head"]["sha"]), sha(current["base"]["sha"])
    state = {
        "request": command["id"],
        "consumed": command["id"],
        "head": head,
        "base": base,
        "phase": "blocked",
        "target": "quota",
        "sequence": 0,
        "command": 0,
        "checks_after": 0,
        "queries": 0,
        "reviews": 0,
        "kody_used": False,
        "greptile_used": False,
        "codex_used": False,
        "started": now.isoformat(),
        "sent": now.isoformat(),
        "resume": None,
        "message": "Checking the explicit request prerequisites.",
    }
    save_state(state, current["node_id"], client)
    try:
        preflight(repository, number, head, base, client)
        checks = current_checks(repository, head, client)
        kody_request_command(checks)
        send_command(repository, number, state, "quota", current["node_id"], client, now, publisher)
    except VerificationError as error:
        if state["phase"] != "sending":
            block_request(
                state,
                current["node_id"],
                client,
                category=(
                    "publisher"
                    if isinstance(error, CommandAccountError)
                    else "requester"
                    if isinstance(error, RequesterAuthorityError)
                    else None
                ),
            )
        raise
    return state


def advance(repository, number, state, comments, client, now, publisher=None):
    """Resume one request using fresh provider metadata, without interpreting findings as failures."""
    if state["phase"] in TERMINAL:
        return
    current = client.get(f"repos/{repository}/pulls/{number}")
    subject = current["node_id"]
    if (
        current.get("state") != "open"
        or current.get("draft") is not True
        or current.get("head", {}).get("sha") != state["head"]
        or current.get("base", {}).get("sha") != state["base"]
        or client.get(f"repos/{repository}/git/ref/heads/main").get("object", {}).get("sha")
        != state["base"]
    ):
        state.update(
            phase="superseded",
            message="The frozen Draft or main changed. Request review of the updated revision explicitly.",
        )
        save_state(state, subject, client)
        return
    if now - instant(state["started"]) > REQUEST_TIMEOUT:
        raise VerificationError(
            "Review request expired. Inspect provider state before requesting again"
        )
    checks = current_checks(repository, state["head"], client)
    try:
        require_automated_review(checks, repository, number, state["head"], client)
    except VerificationError:
        pass
    else:
        message = "A substantive current-head review completed. Evaluate all findings before Ready for review."
        try:
            require_clear_formal_reviews(repository, number, state["head"], state["base"], client)
        except VerificationError:
            message = (
                "A substantive current-head review completed, but formal review clearance "
                "is blocked or unavailable. Keep Draft. Inspect formal change requests and "
                "follow the individual operator recovery procedure before Ready. "
                "Provider completion does not dismiss any review or dispose of findings."
            )
        state.update(
            phase="complete",
            message=message,
        )
        save_state(state, subject, client)
        return
    if state["phase"] == "sending":
        matches = [
            row
            for row in comments
            if (
                (github_user(row.get("user")) and row["user"].get("id") == state["publisher_id"])
                if "publisher_id" in state
                else (
                    is_bot(row, GITHUB_ACTIONS_BOT_LOGIN)
                    and row["user"].get("id") == GITHUB_ACTIONS_BOT_ID
                )
            )
            and command_marker(state) in row["body"].splitlines()
        ]
        if len(matches) != 1:
            return block_request(state, subject, client, category="sending")
        if "greptile_credit" in state:
            published = matches[0]
            try:
                require_credit_evidence(
                    state["greptile_credit"], comments, published_id=published["id"]
                )
                if published["id"] <= state["greptile_credit"][0] or published["body"] != (
                    f"@codex review\n<!-- oep-codex-head:{state['head']} -->\n"
                    f"{command_marker(state)}"
                ):
                    raise VerificationError("Codex outbox publication changed")
                require_recovery_feedback(state, comments, published)
            except VerificationError:
                return block_request(state, subject, client, category="sending")
        record_publication(state, matches[0]["id"], matches[0]["created_at"], subject, client)
    if state["phase"] == "coderabbit" and now - instant(state["sent"]) > REVIEW_TIMEOUT:
        return block_request(state, subject, client, category="coderabbit")
    fresh = [
        row
        for row in comments
        if row["id"] > state["command"] and instant(row["created_at"]) >= instant(state["sent"])
    ]
    if any(provider_command_comment(row) for row in fresh):
        raise VerificationError(
            "Another provider command makes this request ambiguous. Inspect the PR"
        )
    if state["phase"] == "waiting":
        if state["resume"] is None:
            return block_request(state, subject, client, category="retry")
        if now >= instant(state["resume"]):
            send_command(repository, number, state, "quota", subject, client, now, publisher)
        return
    if state["phase"] in {"quota", "coderabbit"}:
        replies = []
        for row in fresh:
            if (
                is_bot(row, "coderabbitai[bot]")
                and "<!-- This is an auto-generated reply by CodeRabbit -->" in row["body"]
            ):
                availability, resume = quota_reply(row["body"], instant(row["created_at"]))
                if availability != "unrelated":
                    replies.append((row, availability, resume))
        if len(replies) > 1:
            raise VerificationError("Ambiguous CodeRabbit availability replies")
        if replies:
            reply, availability, resume = replies[0]
            if availability == "unknown":
                raise VerificationError("Unknown CodeRabbit availability. No provider was selected")
            if now - instant(reply["created_at"]) > REPLY_TIMEOUT:
                if provider_active(checks):
                    return
                send_command(
                    repository,
                    number,
                    state,
                    "quota",
                    subject,
                    client,
                    now,
                    publisher,
                    availability_reply=reply,
                )
                return
            if availability == "limited":
                if provider_active(checks):
                    return
                state["resume"] = resume.isoformat() if resume is not None else None
                if not state["greptile_used"] and not state["kody_used"]:
                    send_command(
                        repository,
                        number,
                        state,
                        greptile_fallback_target(),
                        subject,
                        client,
                        now,
                        publisher,
                        availability_reply=reply,
                    )
                elif resume is not None:
                    state.update(
                        phase="waiting",
                        message=f"CodeRabbit quota is exhausted. Recheck at {state['resume']}.",
                    )
                    save_state(state, subject, client)
                else:
                    raise VerificationError("CodeRabbit supplied no usable retry interval")
                return
            if state["phase"] == "quota":
                if availability != "available":
                    raise VerificationError(
                        "Unknown CodeRabbit availability. No provider was selected"
                    )
                send_command(
                    repository,
                    number,
                    state,
                    "coderabbit",
                    subject,
                    client,
                    now,
                    publisher,
                    availability_reply=reply,
                )
                return
        if state["phase"] == "quota":
            if now - instant(state["sent"]) > REPLY_TIMEOUT:
                raise VerificationError(
                    "CodeRabbit availability reply timed out. No fallback was inferred"
                )
            return
    if state["phase"] == "codex":
        if now - instant(state["sent"]) > REVIEW_TIMEOUT:
            raise VerificationError(
                "Codex completion is uncertain. Inspect its published review state"
            )
        return
    if state["phase"] == "greptile" and now - instant(state["sent"]) <= REVIEW_TIMEOUT:
        refusals = [
            row
            for row in fresh
            if greptile_credit_reply(row, state["command"], state["sent"])
            and instant(row["created_at"]) <= now
        ]
        if refusals:
            if len(refusals) != 1:
                raise VerificationError("Ambiguous Greptile credit replies")
            if provider_active(checks):
                return
            send_command(
                repository,
                number,
                state,
                "codex",
                subject,
                client,
                now,
                publisher,
                credit_reply=refusals[0],
            )
            return
    provider_name, provider_app_id = {
        "kody": ("Kody Code Review", KODY_APP_ID),
        "greptile": ("Greptile Review", GREPTILE_APP_ID),
        "coderabbit": ("CodeRabbit", CODERABBIT_APP_ID),
    }[state["phase"]]
    provider = latest_check(checks, provider_name, provider_app_id)
    if provider is None:
        # Matching-name metadata was already excluded by latest_check.
        provider = max(
            (
                row
                for row in checks
                if row.get("app", {}).get("id") == provider_app_id
                and row.get("name") != provider_name
            ),
            key=lambda row: row["id"],
            default=None,
        )
    if (
        provider
        and provider["id"] > state["checks_after"]
        and provider.get("status") == "completed"
    ):
        output = provider.get("output") or {}
        if (
            state["phase"] in {"coderabbit", "greptile"}
            and provider.get("name") == provider_name
            and provider.get("conclusion") == "timed_out"
        ):
            require_no_unclassified_feedback(repository, number, state, comments, client)
            require_clear_formal_reviews(repository, number, state["head"], state["base"], client)
            target = greptile_fallback_target() if state["phase"] == "coderabbit" else "kody"
            send_command(repository, number, state, target, subject, client, now, publisher)
            return
        if (
            state["phase"] == "kody"
            and provider.get("name") == provider_name
            and (
                provider.get("conclusion") == "timed_out"
                or (
                    provider.get("conclusion") == "failure"
                    and output.get("title") == "Code Review Failed"
                )
            )
        ):
            require_no_unclassified_feedback(repository, number, state, comments, client)
            require_clear_formal_reviews(repository, number, state["head"], state["base"], client)
            send_command(repository, number, state, "codex", subject, client, now, publisher)
            return
        if (
            state["phase"] == "greptile"
            and greptile_completed_check(checks)
            and now - instant(state["sent"]) <= REVIEW_TIMEOUT
        ):
            return
        raise VerificationError(
            "Provider ended without substantive completion. Inspect its result and findings"
        )
    if now - instant(state["sent"]) > REVIEW_TIMEOUT:
        raise VerificationError(
            "Review completion is uncertain. No overlapping fallback was requested"
        )


def process_pr(repository, number, client, now, command_id=None, publisher=None):
    """Serialize a PR's request and record safe actionable failures without disclosing provider bodies."""
    if type(number) is not int or number <= 0:
        raise VerificationError("Invalid review PR number")
    comments = comments_for(repository, number, client)
    state = read_state(comments)
    if command_id is None:
        # A queued Actions event can be replaced. The explicit comment is the durable inbox.
        pending = [
            row
            for row in comments
            if row["body"].strip() == REVIEW_COMMAND
            and github_user(row.get("user"))
            and row["id"] > (state["consumed"] if state else 0)
        ]
        current = client.get(f"repos/{repository}/pulls/{number}") if pending else None
        requester_permissions = {}
        for candidate in sorted(pending, key=lambda row: row["id"], reverse=True):
            user = candidate["user"]
            identity = (user["id"], user["login"])
            if identity not in requester_permissions:
                requester_permissions[identity] = requester_authorized_on_pr(
                    repository, candidate, current, client
                )
            if requester_permissions[identity]:
                command_id = candidate["id"]
                break
    if command_id is not None:
        commands = [
            row
            for row in comments
            if row["id"] == command_id and row["body"].strip() == REVIEW_COMMAND
        ]
        if len(commands) != 1:
            raise VerificationError("The original explicit review command is unavailable")
        if state and command_id <= state["consumed"]:
            return True
        current = client.get(f"repos/{repository}/pulls/{number}")
        if (
            state
            and state["head"] == current.get("head", {}).get("sha")
            and state["phase"] not in {"blocked", "superseded"}
        ):
            require_requester(repository, number, commands[0], client)
            state["consumed"] = command_id
            save_state(state, current["node_id"], client)
            command_id = None
        else:
            start_request(repository, number, commands[0], client, now, publisher)
            return True
    if state:
        try:
            return advance(repository, number, state, comments, client, now, publisher) is not False
        except VerificationError as error:
            if state["phase"] == "sending":
                raise
            subject = client.get(f"repos/{repository}/pulls/{number}")["node_id"]
            return block_request(
                state,
                subject,
                client,
                category=(
                    "publisher"
                    if isinstance(error, CommandAccountError)
                    else "requester"
                    if isinstance(error, RequesterAuthorityError)
                    else None
                ),
            )
    return True


def main():
    """Run trusted default-branch metadata only, starting exclusively from explicit PR commands."""
    try:
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        repository = environ["GITHUB_REPOSITORY"]
        if (
            environ.get("GITHUB_REF") != "refs/heads/main"
            or event.get("repository", {}).get("default_branch") != "main"
            or event.get("repository", {}).get("full_name") != repository
            or repository != UPSTREAM_REPOSITORY
            or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository)
        ):
            raise VerificationError("Review orchestration requires trusted main")
        client = GitHub(environ["GITHUB_TOKEN"])
        command_token = environ.get("OEP_REVIEW_COMMAND_TOKEN")
        publisher = GitHub(command_token) if command_token else None
        now = datetime.now(UTC)
        event_name = environ["GITHUB_EVENT_NAME"]
        if event_name == "issue_comment":
            if event.get("action") not in {"created", "edited"} or not event.get("issue", {}).get(
                "pull_request"
            ):
                return 0
            comment = event["comment"]
            number = event["issue"]["number"]
            explicit = (
                event["action"] == "created" and comment.get("body", "").strip() == REVIEW_COMMAND
            )
            if (
                explicit or any(is_bot(comment, login) for login in PROVIDER_BOT_LOGINS)
            ) and process_pr(
                repository, number, client, now, comment["id"] if explicit else None, publisher
            ) is False:
                return 1
        elif event_name in {"schedule", "workflow_dispatch", "check_run"}:
            if event_name == "check_run" and event.get("check_run", {}).get("app", {}).get(
                "id"
            ) not in {CODERABBIT_APP_ID, GREPTILE_APP_ID, KODY_APP_ID, CODEX_APP_ID}:
                return 0
            failed = False
            for page in range(1, MAX_PAGES + 1):
                pulls = client.get_list(
                    f"repos/{repository}/pulls?state=open&per_page=100&page={page}"
                )
                if len(pulls) > 100:
                    raise VerificationError("Invalid open PR inventory")
                for pull in pulls:
                    try:
                        if (
                            process_pr(repository, pull["number"], client, now, publisher=publisher)
                            is False
                        ):
                            failed = True
                    except (VerificationError, KeyError, TypeError, ValueError, OSError):
                        failed = True
                        print(
                            "A PR review could not be processed. Inspect its request and workflow. Other PRs will still be checked."
                        )
                if len(pulls) < 100:
                    break
            else:
                raise VerificationError("Open PR inventory exceeds the page limit")
            if failed:
                return 1
        else:
            raise VerificationError("Unsupported review orchestration event")
    except (VerificationError, KeyError, TypeError, ValueError, OSError):
        print("Review orchestration stopped. Inspect the workflow and PR status before any retry.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
