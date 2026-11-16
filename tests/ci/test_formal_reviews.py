"""Prove that resolved findings and green providers cannot erase formal review objections."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from script.ci import review_state
from script.ci.check_ci_candidate import check as admit_candidate
from script.ci.check_commit_signatures import VerificationError
from script.ci.pr_readiness import require_clear_formal_reviews
from tests.ci.test_ci_candidate import BASE, HEAD, REPOSITORY, Evidence
from tests.ci.test_ci_candidate import candidate as candidate_evidence


@pytest.fixture
def candidate():
    """Reuse the existing independent green candidate inputs without changing their assertions."""
    return candidate_evidence.__wrapped__()


def review_page(reviews=(), *, more=False, cursor=None, total=None):
    """Supply independent frozen GraphQL evidence with an explicit complete review count."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": "OPEN",
                    "headRefOid": HEAD,
                    "baseRefOid": BASE,
                    "reviewDecision": None,
                    "latestOpinionatedReviews": {
                        "totalCount": len(reviews) if total is None else total,
                        "nodes": [
                            {
                                **review,
                                "author": {
                                    "__typename": "Bot",
                                    "id": f"actor-{review['id']}",
                                    "login": "synthetic-reviewer",
                                    **review.get("author", {}),
                                },
                            }
                            for review in reviews
                        ],
                        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                    },
                }
            }
        }
    }


class FormalEvidence(Evidence):
    """Serve review pages alongside the existing candidate fixtures and reject all mutations."""

    def __init__(self, current=None, checks=(), pages=None):
        """Record read-only queries and allow sequential pages or later objections."""
        super().__init__(current or {}, checks)
        self.pages = [review_page()] if pages is None else pages
        self.queries = []

    def post(self, path, payload):
        """Accept only GraphQL queries, never review dismissal, approval or readiness mutations."""
        assert path == "graphql"
        assert payload["query"].lstrip().startswith("query(")
        self.queries.append(deepcopy(payload))
        if "reviewDecision" in payload["query"]:
            if len(self.pages) > 1:
                return deepcopy(self.pages.pop(0))
            return deepcopy(self.pages[0])
        return super().post(path, payload)


@pytest.mark.parametrize("reviewer", ["kody-ai[bot]", "coderabbitai[bot]", "human-reviewer"])
@pytest.mark.parametrize("action", ["ready_for_review", "labeled"])
def test_formal_objection_blocks_green_candidate_despite_later_comment(candidate, reviewer, action):
    """A later clean comment and resolved threads cannot supersede a formal change request."""
    event, current, checks = candidate
    event["action"] = action
    reviews = [
        {"id": "PRR_17", "state": "CHANGES_REQUESTED", "author": {"login": reviewer}},
    ]
    response = review_page(reviews)
    response["data"]["repository"]["pullRequest"]["reviews"] = {
        "nodes": [*reviews, {"id": "PRR_18", "state": "COMMENTED", "author": {"login": reviewer}}]
    }
    client = FormalEvidence(current, checks, [response])
    with pytest.raises(VerificationError, match="CHANGES_REQUESTED review PRR_17"):
        admit_candidate(event, REPOSITORY, client)


@pytest.mark.parametrize("later_state", ["APPROVED", "COMMENTED", "DISMISSED"])
@pytest.mark.parametrize("human_objection", [False, True])
def test_native_effective_reviews_preserve_approval_comment_and_independent_actor_semantics(
    candidate, later_state, human_objection
):
    """GitHub's effective opinion accepts later approval or dismissal but preserves comments and other objections."""
    event, current, checks = candidate
    original = {"id": "PRR_17", "state": "CHANGES_REQUESTED"}
    later = {"id": "PRR_18", "state": later_state}
    effective = original if later_state == "COMMENTED" else later
    response = review_page([effective])
    pull_request = response["data"]["repository"]["pullRequest"]
    pull_request["reviews"] = {"nodes": [original, later]}
    if human_objection:
        human_review = {
            "id": "PRR_19",
            "state": "CHANGES_REQUESTED",
            "author": {"id": "human-actor", "__typename": "User", "login": "human-reviewer"},
        }
        pull_request["latestOpinionatedReviews"]["nodes"].append(human_review)
        pull_request["latestOpinionatedReviews"]["totalCount"] += 1
    client = FormalEvidence(current, checks, [response])
    if later_state == "COMMENTED" or human_objection:
        with pytest.raises(VerificationError, match="CHANGES_REQUESTED"):
            admit_candidate(event, REPOSITORY, client)
    else:
        assert admit_candidate(event, REPOSITORY, client) == HEAD


def test_dependabot_exception_does_not_erase_formal_objections(candidate):
    """The provider exception cannot override a formal change request from any reviewer."""
    event, current, checks = candidate
    current["user"]["login"] = "dependabot[bot]"
    client = FormalEvidence(
        current,
        checks[:2] + checks[3:],
        [review_page([{"id": "PRR_17", "state": "CHANGES_REQUESTED"}])],
    )
    with pytest.raises(VerificationError, match="CHANGES_REQUESTED"):
        admit_candidate(event, REPOSITORY, client)


def test_individually_dismissed_history_is_read_only_and_repeatable(candidate):
    """An actual dismissal permits admission without creating an approval or another mutation."""
    event, current, checks = candidate
    client = FormalEvidence(
        current,
        checks,
        [
            review_page(
                [{"id": "PRR_17", "state": "DISMISSED"}, {"id": "PRR_18", "state": "COMMENTED"}]
            )
        ],
    )
    assert admit_candidate(event, REPOSITORY, client) == HEAD
    assert admit_candidate(event, REPOSITORY, client) == HEAD


def test_formal_objection_on_later_page_cannot_hide_behind_approval():
    """Read every page even when initial reviews are approved or dismissed."""
    client = FormalEvidence(
        pages=[
            review_page([{"id": "PRR_17", "state": "APPROVED"}], more=True, cursor="next", total=2),
            review_page([{"id": "PRR_18", "state": "CHANGES_REQUESTED"}], total=2),
        ]
    )
    with pytest.raises(VerificationError, match="review PRR_18"):
        require_clear_formal_reviews(REPOSITORY, 12, HEAD, BASE, client)
    assert [query["variables"]["cursor"] for query in client.queries] == [None, "next"]


def test_complete_paginated_history_allows_resolved_formal_states():
    """A bounded complete review inventory accepts COMMENTED, DISMISSED, APPROVED and PENDING."""
    client = FormalEvidence(
        pages=[
            review_page(
                [{"id": "PRR_17", "state": "DISMISSED"}], more=True, cursor="next", total=4
            ),
            review_page(
                [
                    {"id": f"PRR_{index}", "state": state}
                    for index, state in [(18, "COMMENTED"), (19, "APPROVED"), (20, "PENDING")]
                ],
                total=4,
            ),
        ]
    )
    require_clear_formal_reviews(REPOSITORY, 12, HEAD, BASE, client)
    assert len(client.queries) == 2


@pytest.mark.parametrize(
    "fault",
    [
        "head",
        "base",
        "closed",
        "decision",
        "errors",
        "missing",
        "count",
        "rows",
        "duplicate",
        "id",
        "state",
        "cursor",
        "author",
        "actor-type",
        "actor-id",
    ],
)
def test_unavailable_or_ambiguous_formal_evidence_blocks(fault):
    """Identity drift, incomplete pages, malformed reviews and API errors never pass admission."""
    response = review_page([{"id": "PRR_17", "state": "COMMENTED"}])
    current = response["data"]["repository"]["pullRequest"]
    reviews = current["latestOpinionatedReviews"]
    if fault in {"head", "base"}:
        current[f"{fault}RefOid"] = "3" * 40
    elif fault == "closed":
        current["state"] = "CLOSED"
    elif fault == "decision":
        current["reviewDecision"] = "CHANGES_REQUESTED"
    elif fault == "errors":
        response["errors"] = [{"message": "private provider details"}]
    elif fault == "missing":
        del current["reviewDecision"]
    elif fault == "count":
        reviews["totalCount"] = 2
    elif fault == "rows":
        reviews["nodes"] = None
    elif fault == "duplicate":
        reviews["nodes"] *= 2
        reviews["totalCount"] = 2
    elif fault == "id":
        reviews["nodes"][0]["id"] = True
    elif fault == "state":
        reviews["nodes"][0]["state"] = "unknown"
    elif fault == "author":
        reviews["nodes"][0]["author"] = None
    elif fault == "actor-type":
        reviews["nodes"][0]["author"]["__typename"] = "Unknown"
    elif fault == "actor-id":
        reviews["nodes"][0]["author"]["id"] = ""
    else:
        reviews["pageInfo"] = {"hasNextPage": True, "endCursor": None}
        reviews["totalCount"] = 2
    with pytest.raises(VerificationError):
        require_clear_formal_reviews(REPOSITORY, 12, HEAD, BASE, FormalEvidence(pages=[response]))


@pytest.mark.parametrize(
    "fault", ["head", "base", "count", "duplicate", "reviewer", "cycle", "limit"]
)
def test_formal_review_pagination_detects_races_and_nontermination(monkeypatch, fault):
    """A changed revision, changing history, duplicate review or looping cursor blocks safely."""
    first = review_page([{"id": "PRR_17", "state": "COMMENTED"}], more=True, cursor="next", total=3)
    second = review_page(
        [{"id": "PRR_18", "state": "COMMENTED"}], more=True, cursor="after", total=3
    )
    current = second["data"]["repository"]["pullRequest"]
    if fault in {"head", "base"}:
        current[f"{fault}RefOid"] = "3" * 40
    elif fault == "count":
        current["latestOpinionatedReviews"]["totalCount"] = 4
    elif fault == "duplicate":
        current["latestOpinionatedReviews"]["nodes"][0]["id"] = "PRR_17"
    elif fault == "reviewer":
        current["latestOpinionatedReviews"]["nodes"][0]["author"]["id"] = "actor-PRR_17"
    elif fault == "cycle":
        current["latestOpinionatedReviews"]["pageInfo"]["endCursor"] = "next"
    else:
        monkeypatch.setattr("script.ci.pr_readiness.MAX_THREAD_PAGES", 1)
    with pytest.raises(VerificationError):
        require_clear_formal_reviews(
            REPOSITORY, 12, HEAD, BASE, FormalEvidence(pages=[first, second])
        )


@pytest.mark.parametrize("fault", [None, "objection", "head", "base", "provider", "identity"])
def test_pre_ready_and_premerge_snapshot_rechecks_live_objections(candidate, fault):
    """The operator's read-only gate checks genuine review coverage and notices a new objection or revision."""
    _, current, checks = candidate
    current["base"].update(ref="main", repo={"full_name": REPOSITORY})
    final = review_page()
    if fault in {"head", "base"}:
        final["data"]["repository"]["pullRequest"][f"{fault}RefOid"] = "3" * 40
    elif fault == "objection":
        final = review_page([{"id": "PRR_19", "state": "CHANGES_REQUESTED"}])
    elif fault == "provider":
        checks[2]["output"]["title"] = "Review skipped"
    elif fault == "identity":
        checks[2]["app"]["id"] = 123
    client = FormalEvidence(current, checks, [review_page(), final])
    if fault:
        with pytest.raises(VerificationError):
            review_state.check(REPOSITORY, 12, HEAD, BASE, client)
    else:
        review_state.check(REPOSITORY, 12, HEAD, BASE, client)
        assert sum("reviewDecision" in query["query"] for query in client.queries) == 2


@pytest.mark.parametrize("fault", [None, "ref", "event", "repository", "head", "api"])
def test_trusted_dispatch_uses_read_only_snapshot_and_safe_failures(
    candidate, monkeypatch, tmp_path, capsys, fault
):
    """Only an upstream main dispatch can publish a summary and failures never expose provider details."""
    _, current, checks = candidate
    repository = "patrickvorgers/open-energy-planner"
    current["base"].update(ref="main", repo={"full_name": repository})
    client = FormalEvidence(current, checks)
    calls = []

    def check(selected_repository, number, head, base, selected_client):
        """Capture the dispatch boundary while simulating a safe transport failure."""
        calls.append((selected_repository, number, head, base, selected_client))
        if fault == "api":
            raise VerificationError("Formal review evidence unavailable")

    event = {
        "repository": {"full_name": repository, "default_branch": "main"},
        "inputs": {"pull_request": "12", "expected_head": HEAD, "expected_base": BASE},
    }
    if fault == "head":
        event["inputs"]["expected_head"] = "bad"
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    summary = tmp_path / "summary.md"
    for name, value in {
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_EVENT_NAME": "push" if fault == "event" else "workflow_dispatch",
        "GITHUB_REF": "refs/heads/untrusted" if fault == "ref" else "refs/heads/main",
        "GITHUB_SHA": "4" * 40,
        "GITHUB_REPOSITORY": "fork/planner" if fault == "repository" else repository,
        "GITHUB_TOKEN": "private-fixture-token",
        "GITHUB_STEP_SUMMARY": str(summary),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(review_state, "GitHub", lambda token: client)
    monkeypatch.setattr(review_state, "check", check)
    assert review_state.main() == (1 if fault else 0)
    assert "private-fixture-token" not in capsys.readouterr().out
    assert summary.exists() is (fault is None)
    if fault is None:
        assert calls == [(repository, 12, HEAD, BASE, client)]
        assert "No review was dismissed" in summary.read_text()
    elif fault != "api":
        assert calls == []


def test_review_snapshot_workflow_never_executes_public_pr_code_or_mutates_reviews():
    """The explicit review snapshot pins trusted main, has read-only permissions and no secrets."""
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/review-state.yaml"
    ).read_text()
    assert "workflow_dispatch:" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "pull-requests: read" in workflow
    assert "checks: read" in workflow
    assert "write" not in workflow
    assert "secrets." not in workflow
    assert "head.sha" not in workflow


@pytest.mark.parametrize(
    "legacy_fields",
    [
        {},
        {"databaseId": None},
        {"databaseId": None, "fullDatabaseId": 9223372036854775807},
        {"databaseId": 2147483647},
        {"databaseId": 2147483648},
    ],
    ids=["omitted", "null", "64-bit", "32-bit-boundary", "above-32-bit"],
)
def test_nonnull_review_node_id_accepts_approval_without_legacy_database_identity(legacy_fields):
    """A valid effective approval uses its required node ID despite missing or 64-bit legacy metadata."""
    response = review_page()
    response["data"]["repository"]["pullRequest"]["latestOpinionatedReviews"].update(
        totalCount=1,
        nodes=[
            {
                "id": "PRR_effective_approval",
                "state": "APPROVED",
                "author": {"__typename": "User", "id": "actor-approval", "login": "reviewer"},
                **legacy_fields,
            }
        ],
    )
    require_clear_formal_reviews(REPOSITORY, 12, HEAD, BASE, FormalEvidence(pages=[response]))


@pytest.mark.parametrize(
    "review_identity",
    [
        {},
        {"id": None},
        {"id": True},
        {"id": False},
        {"id": 17},
        {"id": 9223372036854775807},
        {"id": ""},
    ],
    ids=["missing", "null", "true", "false", "integer", "64-bit-integer", "empty"],
)
def test_missing_or_nonstring_review_node_id_blocks_even_with_valid_legacy_id(review_identity):
    """A legacy numeric ID cannot conceal an absent or malformed required review node identity."""
    response = review_page()
    response["data"]["repository"]["pullRequest"]["latestOpinionatedReviews"].update(
        totalCount=1,
        nodes=[
            {
                "databaseId": 23,
                "state": "APPROVED",
                "author": {"__typename": "User", "id": "actor-approval", "login": "reviewer"},
                **review_identity,
            }
        ],
    )
    with pytest.raises(VerificationError, match="formal review evidence"):
        require_clear_formal_reviews(REPOSITORY, 12, HEAD, BASE, FormalEvidence(pages=[response]))


def test_duplicate_review_node_id_across_distinct_actors_is_ambiguous():
    """Distinct legacy IDs and reviewers cannot make a duplicate GraphQL review identity trustworthy."""
    response = review_page()
    response["data"]["repository"]["pullRequest"]["latestOpinionatedReviews"].update(
        totalCount=2,
        nodes=[
            {
                "id": "PRR_duplicate",
                "databaseId": legacy_id,
                "state": "APPROVED",
                "author": {"__typename": "User", "id": actor, "login": actor},
            }
            for legacy_id, actor in [(23, "first-reviewer"), (24, "second-reviewer")]
        ],
    )
    with pytest.raises(VerificationError, match="formal review evidence"):
        require_clear_formal_reviews(REPOSITORY, 12, HEAD, BASE, FormalEvidence(pages=[response]))
