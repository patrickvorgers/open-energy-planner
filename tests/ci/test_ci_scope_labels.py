"""Scope labels remain current and informational without executing pull-request source."""

import json
import subprocess
from copy import deepcopy
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from script.ci import label_ci_scope as labels

REPOSITORY = "owner/planner"
BASE, HEAD = "1" * 40, "2" * 40


@pytest.fixture
def event():
    """Represent an immutable new revision on the owning repository."""
    return {
        "number": 12,
        "repository": {"full_name": REPOSITORY},
        "action": "synchronize",
        "pull_request": {"base": {"sha": BASE}, "head": {"sha": HEAD}},
    }


class LabelBoundary:
    """Apply independent label mutations and allow the PR to change between API boundaries."""

    def __init__(self):
        """Keep user/request labels alongside one outdated scope."""
        self.current = {
            "state": "open",
            "base": {"sha": BASE},
            "head": {"sha": HEAD},
            "labels": [{"name": name} for name in ("bug", "ci: run", "ci-scope: full")],
        }
        self.reads, self.writes, self.fetched = 0, [], []
        self.change_at = None
        self.existing = False
        self.existing_label_name = "ci-scope: frontend"
        self.catalogue = {}

    def get(self, path):
        """Change only the current head when testing a stale job, never its event snapshot."""
        if "/pulls/" in path:
            assert path == "repos/owner/planner/pulls/12"
            self.reads += 1
            if self.reads == self.change_at:
                self.current["head"]["sha"] = "4" * 40
            return deepcopy(self.current)
        assert path.startswith("repos/owner/planner/labels/")
        from urllib.parse import unquote

        name = unquote(path.rsplit("/", 1)[1])
        return deepcopy(self.catalogue.get(name, {"name": self.existing_label_name}))

    def fetch_pr_history(self, repository, number):
        """Fetch object identities only. No checkout, import or PR script execution is permitted."""
        self.fetched.append((repository, number))

    def write(self, method, path, body=None):
        """Retain repository metadata separately from issue labels and preserve unrelated labels."""
        self.writes.append((method, path, deepcopy(body)))
        if path == "repos/owner/planner/labels":
            if self.existing or body["name"] in self.catalogue:
                raise labels.LabelWriteError(422)
            self.catalogue[body["name"]] = deepcopy(body)
        elif method == "PATCH":
            from urllib.parse import unquote

            self.catalogue[unquote(path.rsplit("/", 1)[1])].update(body)
        elif method == "POST":
            self.current["labels"].extend({"name": name} for name in body["labels"])
        else:
            assert method == "DELETE"
            from urllib.parse import unquote

            removed = unquote(path.rsplit("/", 1)[1])
            self.current["labels"] = [
                item for item in self.current["labels"] if item["name"] != removed
            ]


@pytest.mark.parametrize(
    "profile", ["documentation", "ci", "native", "frontend", "ha", "storage", "integration", "full"]
)
def test_scope_replaces_only_its_own_labels_and_repeats_without_writes(event, monkeypatch, profile):
    """Every policy result receives its literal display label while request/user labels survive."""
    client = LabelBoundary()
    monkeypatch.setattr(labels, "merged_scope", lambda base, head: profile)
    assert labels.update(event, REPOSITORY, client) == profile
    assert {item["name"] for item in client.current["labels"]} == {
        "bug",
        "ci: run",
        "ci-scope: " + profile,
    }
    assert client.fetched == [(REPOSITORY, 12)]
    client.writes.clear()
    assert labels.update(event, REPOSITORY, client) == profile
    assert not client.writes


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("selected:browser,ci,unit-frontend", {"ci-scope: browser", "ci-scope: frontend"}),
        (
            "selected:ci,supervisor,unit-supervisor",
            {"ci-scope: supervisor", "ci-scope: unit-supervisor"},
        ),
        ("selected:ci,unit-app-price", {"ci-scope: unit-app-price"}),
        ("selected:ci", {"ci-scope: ci"}),
        ("selected:ci,performance-queries", {"ci-scope: performance-queries"}),
    ],
)
def test_independent_suites_publish_planned_group_labels(event, monkeypatch, profile, expected):
    """Display each planned group, omit standard checks and preserve user/request labels."""
    client = LabelBoundary()
    client.current["labels"].append({"name": "ci-scope: focused"})
    monkeypatch.setattr(labels, "merged_scope", lambda *_: profile)
    assert labels.update(event, REPOSITORY, client) == profile
    assert {item["name"] for item in client.current["labels"]} == {"bug", "ci: run"} | expected
    client.writes.clear()
    labels.update(event, REPOSITORY, client)
    assert not client.writes


def test_new_revision_replaces_all_old_groups_without_starting_tests(event, monkeypatch):
    """A later commit replaces old planned groups while preserving the explicit run request."""
    client = LabelBoundary()
    monkeypatch.setattr(labels, "merged_scope", lambda *_: "selected:browser,ci,unit-frontend")
    labels.update(event, REPOSITORY, client)
    event["pull_request"]["head"]["sha"] = client.current["head"]["sha"] = "4" * 40
    monkeypatch.setattr(labels, "merged_scope", lambda *_: "selected:ci,databases,unit-app-price")
    assert labels.update(event, REPOSITORY, client) == "selected:ci,databases,unit-app-price"
    assert {item["name"] for item in client.current["labels"]} == {
        "bug",
        "ci: run",
        "ci-scope: databases",
        "ci-scope: unit-app-price",
    }


@pytest.mark.parametrize("profile", ["full+performance-queries", "storage+performance-queries"])
@pytest.mark.parametrize("existing", [False, True])
def test_performance_label_is_additional_distinct_and_repairs_existing_color(
    event, monkeypatch, profile, existing
):
    """An additive volume decision retains the matrix label and creates or repairs one magenta label."""
    client = LabelBoundary()
    performance = "ci-scope: performance-queries"
    if existing:
        client.catalogue[performance] = {"name": performance, "color": "1d76db"}
        client.current["labels"].append({"name": performance})
    monkeypatch.setattr(labels, "merged_scope", lambda *_: profile)
    labels.update(event, REPOSITORY, client)
    assert {item["name"] for item in client.current["labels"]} == {
        "bug",
        "ci: run",
        "ci-scope: " + profile.split("+")[0],
        performance,
    }
    assert client.catalogue[performance]["color"] == "d500f9"
    client.writes.clear()
    labels.update(event, REPOSITORY, client)
    assert not client.writes
    monkeypatch.setattr(labels, "merged_scope", lambda *_: profile.split("+")[0])
    labels.update(event, REPOSITORY, client)
    assert performance not in {item["name"] for item in client.current["labels"]}


def test_partially_present_selection_adds_only_missing_groups(event, monkeypatch):
    """A retained correct group is neither duplicated nor removed when its peer is missing."""
    client = LabelBoundary()
    client.current["labels"].append({"name": "ci-scope: frontend"})
    monkeypatch.setattr(labels, "merged_scope", lambda *_: "selected:browser,ci,unit-frontend")
    labels.update(event, REPOSITORY, client)
    additions = [
        body["labels"]
        for method, path, body in client.writes
        if method == "POST" and "/issues/" in path
    ]
    assert additions == [["ci-scope: browser"]]
    assert [item["name"] for item in client.current["labels"]].count("ci-scope: frontend") == 1


@pytest.mark.parametrize("profile", ["frontend", "selected:browser,ci,unit-frontend"])
@pytest.mark.parametrize("change_at", [1, 2, 3])
def test_stale_head_never_receives_a_new_scope(event, monkeypatch, change_at, profile):
    """Head changes before classification, after fetch or before the PR write prevent publication."""
    client = LabelBoundary()
    client.change_at = change_at
    monkeypatch.setattr(labels, "merged_scope", lambda *_: profile)
    assert labels.update(event, REPOSITORY, client) is None
    assert not any("/issues/" in path for _, path, _ in client.writes)


def test_second_group_creation_failure_preserves_existing_pr_labels(event, monkeypatch):
    """A failed group creation cannot attach a partial selection or remove the previous labels."""
    client = LabelBoundary()
    previous = deepcopy(client.current["labels"])
    original = client.write

    def write(method, path, body=None):
        """Fail the second repository label creation before any PR label publication."""
        if path.endswith("/labels") and body.get("name") == "ci-scope: frontend":
            raise labels.LabelWriteError(403)
        return original(method, path, body)

    client.write = write
    monkeypatch.setattr(labels, "merged_scope", lambda *_: "selected:browser,ci,unit-frontend")
    with pytest.raises(labels.LabelWriteError):
        labels.update(event, REPOSITORY, client)
    assert client.current["labels"] == previous


@pytest.mark.parametrize("invalid", ["closed", "base", "head_shape"])
def test_closed_retargeted_or_malformed_pr_is_not_mutated(event, monkeypatch, invalid):
    """Old events cannot alter labels on a closed PR or a different base/head range."""
    client = LabelBoundary()
    if invalid == "closed":
        client.current["state"] = "closed"
    elif invalid == "base":
        client.current["base"]["sha"] = "4" * 40
    else:
        client.current["head"] = None
    assert labels.update(event, REPOSITORY, client) is None
    assert not client.writes and not client.fetched


def test_existing_repository_label_is_verified_before_reuse(event, monkeypatch):
    """A concurrent label creation is accepted only when GitHub returns the intended name."""
    client = LabelBoundary()
    client.existing = True
    monkeypatch.setattr(labels, "merged_scope", lambda *_: "frontend")
    assert labels.update(event, REPOSITORY, client) == "frontend"


def test_existing_repository_label_with_another_name_is_rejected(event, monkeypatch):
    """A 422 followed by an unexpected label name fails without writing a PR label."""
    client = LabelBoundary()
    client.existing = True
    client.existing_label_name = "ci-scope: full"
    monkeypatch.setattr(labels, "merged_scope", lambda *_: "frontend")
    with pytest.raises(labels.LabelWriteError):
        labels.update(event, REPOSITORY, client)
    assert not any("/issues/" in path for _, path, _ in client.writes)


@pytest.mark.parametrize(
    ("previous_profile", "next_profile", "expected"),
    [
        ("frontend", "ha", {"ci-scope: ha"}),
        (
            "selected:browser,ci,unit-frontend",
            "selected:ci,unit-app-price",
            {"ci-scope: unit-app-price"},
        ),
    ],
)
def test_a_new_head_stops_old_removals_and_its_next_job_cleans_up(
    event, monkeypatch, previous_profile, next_profile, expected
):
    """A head arriving after publication prevents further old-job writes and receives its own next scope."""
    client = LabelBoundary()
    client.change_at = 4
    monkeypatch.setattr(labels, "merged_scope", lambda *_: previous_profile)
    assert labels.update(event, REPOSITORY, client) is None
    assert not any(method == "DELETE" for method, _, _ in client.writes)
    event["pull_request"]["head"]["sha"] = "4" * 40
    monkeypatch.setattr(labels, "merged_scope", lambda *_: next_profile)
    assert labels.update(event, REPOSITORY, client) == next_profile
    assert {item["name"] for item in client.current["labels"]} == {"bug", "ci: run"} | expected


@pytest.mark.parametrize("status", [404, 403])
def test_concurrent_removal_is_idempotent_but_permission_failure_is_not(event, monkeypatch, status):
    """A missing old scope is already removed. Denied writes must remain a failed publication."""
    client = LabelBoundary()
    original = client.write

    def write(method, path, body=None):
        """Inject an independently specified failure only at the old-label removal boundary."""
        if method == "DELETE":
            raise labels.LabelWriteError(status)
        return original(method, path, body)

    client.write = write
    monkeypatch.setattr(labels, "merged_scope", lambda *_: "frontend")
    if status == 404:
        assert labels.update(event, REPOSITORY, client) == "frontend"
    else:
        with pytest.raises(labels.LabelWriteError):
            labels.update(event, REPOSITORY, client)


@pytest.mark.parametrize("failure", [None, "conflict", "unreadable", "timeout"])
def test_scope_uses_merged_tree_and_falls_back_to_full(monkeypatch, failure):
    """The base policy receives the intended merge tree, never the PR's policy or head-only diff."""
    policy_calls = []

    def merge(command, **options):
        """Model the native Git boundary without installing Git in application test images."""
        assert command == ["git", "merge-tree", "--write-tree", BASE, HEAD]
        assert options["timeout"] == 60
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        if failure == "conflict":
            raise subprocess.CalledProcessError(1, command)
        return "unreadable" if failure else "3" * 40

    def classify(base, tree, root):
        """Reject head-only selection so intervening main changes cannot distort the displayed scope."""
        policy_calls.append((base, tree))
        assert (base, tree, root) == (BASE, "3" * 40, labels.ROOT)
        return "frontend"

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", merge)
    monkeypatch.setattr(labels, "select_profile", classify)
    assert labels.merged_scope(BASE, HEAD) == ("full" if failure else "frontend")
    assert len(policy_calls) == (0 if failure else 1)


@pytest.mark.parametrize("failure", [None, "redirect", "unauthorized", "network"])
def test_label_transport_bounds_requests_and_hides_provider_secrets(failure):
    """Mutations retain redirect refusal, bounded waits and private errors without revealing tokens."""
    client = labels.LabelClient("private-test-token")

    class Opener:
        """Represent a safe response or failures containing private provider diagnostics."""

        def open(self, request, timeout):
            """Check the literal wire boundary without executing external requests."""
            assert request.full_url == "https://api.github.com/repos/owner/planner/labels"
            assert request.method == "POST" and timeout == 20
            assert json.loads(request.data) == {"name": "ci-scope: frontend"}
            assert request.get_header("Authorization") == "Bearer private-test-token"
            if failure in ("redirect", "unauthorized"):
                raise HTTPError(
                    request.full_url,
                    302 if failure == "redirect" else 401,
                    "private-test-token",
                    {},
                    None,
                )
            if failure == "network":
                raise URLError("private-test-token")
            return self

        def __enter__(self):
            """Model a successfully created repository label."""
            return SimpleNamespace(status=201)

        def __exit__(self, *_):
            """Close the bounded synthetic response."""

    client._opener = Opener()
    if failure:
        with pytest.raises(labels.VerificationError) as captured:
            client.write("POST", "repos/owner/planner/labels", {"name": "ci-scope: frontend"})
        assert "private-test-token" not in str(captured.value)
    else:
        client.write("POST", "repos/owner/planner/labels", {"name": "ci-scope: frontend"})


def test_publisher_workflow_keeps_write_authority_on_immutable_base():
    """Keep write authority on base code and isolate every ignored event from active scope jobs."""
    source = (labels.ROOT / ".github/workflows/ci-scope-labels.yaml").read_text()
    authority, job = source.split("jobs:\n", 1)
    assert "pull_request_target:\n" in authority and "synchronize" in authority
    assert "issues:" not in authority
    assert "pull-requests: write" not in authority
    assert job.count("issues: write") == 1
    assert job.count("pull-requests: write") == 1
    assert "ref: ${{ github.event.pull_request.base.sha }}" in job
    assert "pull_request.head.sha" not in job
    assert "persist-credentials: false" in job
    assert "python3 -I script/ci/label_ci_scope.py" in job
    assert "python3 -I script/label_ci_scope.py" in job
    concurrency = " ".join(
        authority.split("  group: >-\n", 1)[1].split("\n  cancel-in-progress:", 1)[0].split()
    )
    assert concurrency == (
        "${{ (github.event.action == 'labeled' && github.event.label.name != 'ci: run' "
        "|| github.event.action == 'edited' && github.event.changes.base.ref.from == null "
        "|| github.event.pull_request.state != 'open') "
        "&& format('ignored-scope-label-{0}', github.run_id) "
        "|| format('pr-scope-{0}', github.event.pull_request.number) }}"
    )


@pytest.mark.parametrize("trusted", [False, True])
def test_entrypoint_rejects_untrusted_execution_and_ignores_display_labels(
    tmp_path, monkeypatch, event, trusted
):
    """An unprivileged event cannot write labels and a scope-label event cannot recursively invoke updates."""
    event["action"] = "labeled"
    event["label"] = {"name": "ci-scope: frontend"}
    document = tmp_path / "event.json"
    document.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(document))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target" if trusted else "pull_request")

    def unexpected(*_):
        """Do not construct an authenticated client or reach the API for an ignored event."""
        pytest.fail("An ignored event reached scope mutation")

    monkeypatch.setattr(labels, "LabelClient", unexpected)
    assert labels.main() == (0 if trusted else 1)


@pytest.mark.parametrize("status", [403, 503])
def test_entrypoint_reports_safe_write_status_without_credentials(
    tmp_path, monkeypatch, event, capsys, status
):
    """A denied or unavailable write stays failed and exposes only its HTTP status."""
    document = tmp_path / "event.json"
    document.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(document))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_TOKEN", "private-test-token")

    def rejected(*_):
        """Represent a sanitized write failure without reaching GitHub."""
        raise labels.LabelWriteError(status)

    monkeypatch.setattr(labels, "update", rejected)
    assert labels.main() == 1
    output = capsys.readouterr()
    assert f"HTTP {status}" in output.out
    assert "private-test-token" not in output.out + output.err
