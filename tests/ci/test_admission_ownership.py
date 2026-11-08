"""Keep the reusable admission workflow within its reviewed metadata capabilities."""

import re
from pathlib import Path

import pytest

ADMISSION_PROTOCOL = """on:
  workflow_call:
    outputs:
      reuse:
        value: ${{ jobs.candidate.outputs.reuse }}
jobs:
  candidate:
    if: >-
      github.event_name != 'pull_request' ||
      github.event.action == 'ready_for_review' || github.event.label.name == 'ci: run'
    runs-on: ubuntu-24.04
    outputs:
      reuse: ${{ steps.reuse.outputs.reuse }}
    permissions:
      contents: read
      actions: read
      checks: read
      pull-requests: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
          fetch-depth: 0
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: python3 -m script.ci.check_ci_candidate
      - uses: ./.github/actions/quick-checks
        id: reuse
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: python3 -m script.ci.check_main_ci_reuse"""


def admission_violations(source, expected=ADMISSION_PROTOCOL):
    """Keep executable YAML fixed while allowing plain labels, comments and the job's time budget."""
    protocol = []
    for original in source.splitlines():
        line = re.split(r"[ \t]+#", original, maxsplit=1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.fullmatch(
            r"[ \t]*(?:-[ \t]+)?(?:name|description):[ \t]+[^ \t|>].*", line
        ) or re.fullmatch(r"[ \t]+timeout-minutes:[ \t]+[1-9][0-9]*", line):
            continue
        protocol.append(line)
    return "\n".join(protocol) != expected


REVIEW_PREFLIGHT_PROTOCOL = """on:
  workflow_dispatch:
    inputs:
      pull_request:
        required: true
        type: string
      expected_head:
        required: true
        type: string
      expected_base:
        required: true
        type: string
      findings_dispositioned:
        required: true
        default: false
        type: boolean
permissions:
  contents: read
  checks: read
  pull-requests: read
concurrency:
  group: manual-review-preflight
  cancel-in-progress: false
jobs:
  preflight:
    if: github.ref == 'refs/heads/main' && github.event.repository.default_branch == 'main'
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          ref: ${{ github.sha }}
          persist-credentials: false
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: python3 -m script.ci.review_preflight"""


def test_review_preflight_owns_only_trusted_main_metadata_reads():
    """The declared preflight has no automatic trigger, contributor execution or write permissions."""
    path = Path(__file__).resolve().parents[2] / ".github/workflows/review-preflight.yaml"
    assert not admission_violations(path.read_text(), REVIEW_PREFLIGHT_PROTOCOL)


REVIEW_ORCHESTRATION_PROTOCOL = """on:
  issue_comment:
    types: [created]
  check_run:
    types: [completed]
  schedule:
    - cron: '7,22,37,52 * * * *'
  workflow_dispatch:
permissions:
  contents: read
  checks: read
  pull-requests: write
  issues: write
concurrency:
  group: oep-review-orchestration
  cancel-in-progress: false
jobs:
  review:
    if: >-
      github.ref == 'refs/heads/main' &&
      github.repository == 'patrickvorgers/open-energy-planner' &&
      github.event.repository.default_branch == 'main' &&
      (github.event_name != 'check_run' ||
       github.event.check_run.app.id == 347564 || github.event.check_run.app.id == 413034) &&
      (github.event_name != 'issue_comment' ||
       (github.event.issue.pull_request &&
        (contains(github.event.comment.body, '/oep-reviewer review') ||
         github.event.comment.user.login == 'coderabbitai[bot]' ||
         github.event.comment.user.login == 'kody-ai[bot]')))
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          ref: refs/heads/main
          persist-credentials: false
        env:
          GITHUB_TOKEN: ${{ github.token }}
          OEP_REVIEW_COMMAND_TOKEN: ${{ secrets.OEP_REVIEW_COMMAND_TOKEN }}
        run: python3 -m script.ci.review_orchestration"""


def test_review_orchestration_has_only_trusted_metadata_and_comment_capabilities():
    """The orchestration workflow cannot gain contributor checkout, test execution or release writes."""
    path = Path(__file__).resolve().parents[2] / ".github/workflows/review-orchestration.yaml"
    assert not admission_violations(path.read_text(), REVIEW_ORCHESTRATION_PROTOCOL)


@pytest.mark.parametrize(
    "before,after",
    [
        ("ref: refs/heads/main", "ref: ${{ github.event.issue.head.sha }}"),
        ("persist-credentials: false", "persist-credentials: true"),
        ("contents: read", "contents: write"),
        ("checks: read", "checks: write"),
        ("steps:", "steps:\n      - run: docker compose up"),
        ("cancel-in-progress: false", "cancel-in-progress: true"),
        ("run: python3 -m script.ci.review_orchestration", "run: bash arbitrary.sh"),
    ],
)
def test_review_orchestration_rejects_unreviewed_execution_capabilities(before, after):
    """The fixed workflow protocol rejects privilege growth and PR-controlled execution."""
    assert admission_violations(
        REVIEW_ORCHESTRATION_PROTOCOL.replace(before, after, 1), REVIEW_ORCHESTRATION_PROTOCOL
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ("workflow_dispatch:", "pull_request:"),
        ("contents: read", "contents: write"),
        ("pull-requests: read", "pull-requests: write"),
        ("ref: ${{ github.sha }}", "ref: ${{ inputs.expected_head }}"),
        ("persist-credentials: false", "persist-credentials: true"),
        ("runs-on: ubuntu-24.04", "runs-on: self-hosted"),
        ("steps:", "steps:\n      - run: docker compose up"),
        ("steps:", "services:\n      db:\n        image: postgres\n    steps:"),
        ("run: python3 -m script.ci.review_preflight", "run: bash arbitrary.sh"),
        (
            "run: python3 -m script.ci.review_preflight",
            "shell: bash -c 'docker compose up; bash {0}'\n        run: python3 -m script.ci.review_preflight",
        ),
        ("env:\n", "env:\n          BASH_ENV: arbitrary.sh\n"),
        ("cancel-in-progress: false", "cancel-in-progress: true"),
    ],
)
def test_review_preflight_rejects_execution_and_privilege_expansion(before, after):
    """A metadata workflow cannot silently gain workloads, mutable source or privileged execution."""
    assert before in REVIEW_PREFLIGHT_PROTOCOL
    assert admission_violations(
        REVIEW_PREFLIGHT_PROTOCOL.replace(before, after, 1), REVIEW_PREFLIGHT_PROTOCOL
    )


def test_reusable_admission_owns_only_reviewed_proof_operations():
    """The actual admission workflow can read proof and run Quick, but cannot launch workloads."""
    root = Path(__file__).resolve().parents[2]
    assert not admission_violations((root / ".github/workflows/ci-admission.yaml").read_text())


def test_every_ci_job_uses_fixed_github_hosted_capacity():
    """Every declared runner is a literal hosted label rather than a PR-controlled expression."""
    root = Path(__file__).resolve().parents[2]
    directory = root / ".github/workflows"
    workflows = sorted((*directory.glob("*.yaml"), *directory.glob("*.yml")))
    assert workflows
    for path in workflows:
        labels = re.findall(r"^\s+runs-on:\s*(.+)$", path.read_text(), re.MULTILINE)
        assert labels and all(label == "ubuntu-24.04" for label in labels), path


@pytest.mark.parametrize("extension", ["yaml", "yml"])
def test_hosted_capacity_guard_rejects_both_workflow_extensions(tmp_path, monkeypatch, extension):
    """Neither supported workflow extension can hide a personal runner from the actual guard."""
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "hosted.yaml").write_text("jobs:\n  check:\n    runs-on: ubuntu-24.04\n")
    (workflows / f"personal.{extension}").write_text("jobs:\n  check:\n    runs-on: self-hosted\n")
    monkeypatch.setitem(
        globals(), "__file__", str(tmp_path / "tests/ci/test_admission_ownership.py")
    )
    with pytest.raises(AssertionError):
        test_every_ci_job_uses_fixed_github_hosted_capacity()


@pytest.mark.parametrize(
    "change",
    [
        "docker",
        "shell",
        "custom-shell",
        "continue-on-error",
        "action",
        "service",
        "permissions",
        "flow-permissions",
        "environment",
        "output",
        "workflow-fallback",
        "job-fallback",
        "commented-output",
        "duplicate-output",
        "candidate-step-condition",
        "quick-step-condition",
        "list-first-shell",
        "list-first-continue",
        "list-first-run",
        "flow-environment",
        "quoted-environment",
        "noncomment-hash",
        "runner",
        "secret-forward",
    ],
)
def test_admission_guard_rejects_workload_and_evidence_bypasses(change):
    """An ordinary admission edit cannot gain shell/Docker services or fabricate a reuse output."""
    root = Path(__file__).resolve().parents[2]
    source = (root / ".github/workflows/ci-admission.yaml").read_text()
    changes = {
        "secret-forward": (
            "        env:\n          GITHUB_TOKEN: ${{ github.token }}\n        run: python3 -m script.ci.check_main_ci_reuse",
            "        env:\n          GITHUB_TOKEN: ${{ secrets.UNEXPECTED_CREDENTIAL }}\n        run: python3 -m script.ci.check_main_ci_reuse",
        ),
        "docker": ("run: python3 -m script.ci.check_ci_candidate", "run: docker compose up"),
        "shell": (
            "run: python3 -m script.ci.check_ci_candidate",
            "run: |\n          bash arbitrary.sh",
        ),
        "custom-shell": (
            "        run: python3 -m script.ci.check_ci_candidate",
            "        shell: bash -c 'docker compose up; bash {0}'\n        run: python3 -m script.ci.check_ci_candidate",
        ),
        "continue-on-error": (
            "        run: python3 -m script.ci.check_ci_candidate",
            "        continue-on-error: true\n        run: python3 -m script.ci.check_ci_candidate",
        ),
        "flow-permissions": (
            "    permissions:\n      contents: read\n      # Read candidate identities and original artifacts only in the admission job.\n      actions: read\n      checks: read\n      pull-requests: read",
            "    permissions: {contents: write, actions: read, checks: read, pull-requests: read}",
        ),
        "workflow-fallback": (
            "value: ${{ jobs.candidate.outputs.reuse }}",
            "value: ${{ jobs.candidate.outputs.reuse || 'true' }} # value: ${{ jobs.candidate.outputs.reuse }}",
        ),
        "job-fallback": (
            "reuse: ${{ steps.reuse.outputs.reuse }}",
            "reuse: ${{ steps.reuse.outputs.reuse || 'true' }} # reuse: ${{ steps.reuse.outputs.reuse }}",
        ),
        "commented-output": (
            "value: ${{ jobs.candidate.outputs.reuse }}",
            "# value: ${{ jobs.candidate.outputs.reuse }}",
        ),
        "duplicate-output": (
            "        value: ${{ jobs.candidate.outputs.reuse }}",
            "        value: ${{ jobs.candidate.outputs.reuse }}\n        value: 'true'",
        ),
        "candidate-step-condition": (
            "        run: python3 -m script.ci.check_ci_candidate",
            "        if: ${{ false }}\n        run: python3 -m script.ci.check_ci_candidate",
        ),
        "quick-step-condition": (
            "      - uses: ./.github/actions/quick-checks",
            "      - if: ${{ false }}\n        uses: ./.github/actions/quick-checks",
        ),
        "list-first-shell": (
            "      - name: Require current reviewed PR candidate",
            "      - shell: bash -c 'docker compose up; bash {0}'\n        name: Require current reviewed PR candidate",
        ),
        "list-first-continue": (
            "      - name: Require current reviewed PR candidate",
            "      - continue-on-error: true\n        name: Require current reviewed PR candidate",
        ),
        "list-first-run": (
            "    steps:",
            "    steps:\n      - run: docker compose up",
        ),
        "flow-environment": (
            "    steps:",
            "    env: {BASH_ENV: malicious.sh}\n    steps:",
        ),
        "quoted-environment": (
            "        env:",
            "        env:\n          'BASH_ENV': malicious.sh",
        ),
        "noncomment-hash": (
            "run: python3 -m script.ci.check_ci_candidate",
            "run: python3 -m script.ci.check_ci_candidate#suffix; docker compose up",
        ),
        "runner": ("runs-on: ubuntu-24.04", "runs-on: self-hosted"),
        "action": ("uses: ./.github/actions/quick-checks", "uses: docker://database-image"),
        "service": ("    steps:", "    services:\n      db:\n        image: postgres\n    steps:"),
        "permissions": ("contents: read", "contents: write"),
        "environment": ("env:\n", "env:\n          BASH_ENV: malicious.sh\n"),
        "output": ("value: ${{ jobs.candidate.outputs.reuse }}", "value: 'true'"),
    }
    before, after = changes[change]
    assert before in source
    assert admission_violations(source.replace(before, after, 1))


def test_admission_timeout_edit_keeps_its_metadata_contract():
    """Changing admission's own time budget does not require any workload capability."""
    root = Path(__file__).resolve().parents[2]
    source = (root / ".github/workflows/ci-admission.yaml").read_text()
    assert "timeout-minutes: 5" in source
    assert not admission_violations(source.replace("timeout-minutes: 5", "timeout-minutes: 6"))


def test_admission_effective_outputs_allow_ordinary_trailing_comments():
    """Comments may explain fixed output wiring without becoming evidence of another expression."""
    root = Path(__file__).resolve().parents[2]
    source = (root / ".github/workflows/ci-admission.yaml").read_text()
    for expression in (
        "value: ${{ jobs.candidate.outputs.reuse }}",
        "reuse: ${{ steps.reuse.outputs.reuse }}",
    ):
        source = source.replace(expression, expression + " # Explain fixed evidence wiring")
    assert not admission_violations(source)


def test_admission_allows_plain_descriptive_metadata_edits():
    """Changing visible step names or output descriptions does not change the fixed execution contract."""
    root = Path(__file__).resolve().parents[2]
    source = (root / ".github/workflows/ci-admission.yaml").read_text()
    source = source.replace("name: Candidate admission and evidence", "name: Read candidate proof")
    source = source.replace(
        "name: Require current reviewed PR candidate", "name: Check current review"
    )
    source = source.replace(
        "description: Validated reuse of an identical successful candidate.",
        "description: Reuse only checked candidate evidence.",
    )
    assert not admission_violations(source)
