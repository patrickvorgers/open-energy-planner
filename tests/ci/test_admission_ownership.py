"""Keep the reusable admission workflow within its reviewed metadata capabilities."""

import re
from pathlib import Path

import pytest

ADMISSION_PROTOCOL = """on:
  workflow_call:
    secrets:
      OEP_RUNNER_READ_TOKEN:
        required: false
    outputs:
      reuse:
        value: ${{ jobs.candidate.outputs.reuse }}
      runner_labels:
        value: ${{ jobs.candidate.outputs.runner_labels }}
jobs:
  candidate:
    if: >-
      github.event_name != 'pull_request' ||
      github.event.action == 'ready_for_review' || github.event.label.name == 'ci: run'
    runs-on: ubuntu-24.04
    outputs:
      reuse: ${{ steps.reuse.outputs.reuse }}
      runner_labels: ${{ steps.admission.outputs.runner_labels }}
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
        id: admission
        env:
          GITHUB_TOKEN: ${{ github.token }}
          OEP_RUNNER_READ_TOKEN: ${{ secrets.OEP_RUNNER_READ_TOKEN }}
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
      codex_reviewed_sha:
        required: true
        type: string
      independent_codex_review_done:
        required: true
        default: false
        type: boolean
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
        "runner-output",
        "runner-job-output",
        "runner-secret-forward",
    ],
)
def test_admission_guard_rejects_workload_and_evidence_bypasses(change):
    """An ordinary admission edit cannot gain shell/Docker services or fabricate a reuse output."""
    root = Path(__file__).resolve().parents[2]
    source = (root / ".github/workflows/ci-admission.yaml").read_text()
    changes = {
        "runner-output": (
            "value: ${{ jobs.candidate.outputs.runner_labels }}",
            "value: '[\"self-hosted\"]'",
        ),
        "runner-job-output": (
            "runner_labels: ${{ steps.admission.outputs.runner_labels }}",
            "runner_labels: '[\"self-hosted\"]'",
        ),
        "runner-secret-forward": (
            "        env:\n          GITHUB_TOKEN: ${{ github.token }}\n        run: python3 -m script.ci.check_main_ci_reuse",
            "        env:\n          GITHUB_TOKEN: ${{ secrets.OEP_RUNNER_READ_TOKEN }}\n        run: python3 -m script.ci.check_main_ci_reuse",
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
