# Trusted PR signature verification

The `Verified PR commits (trusted base)` workflow uses `pull_request_target`. GitHub loads the workflow from the base repository's default branch, and checkout explicitly selects the event's immutable base SHA. Python runs in isolated mode. No source, workflow, Python module or test from the PR is executed by this privileged workflow.

The workflow also handles `edited` events, but starts its job only when the base reference changed. Retargeting rechecks the new base/head range even when the head SHA stays unchanged. Description/title edits do not repeat verification.
Closed PR edits are ignored: after merging, that head may already belong to the base and must not replace the completed verification with an empty-range failure.

The trusted `script/ci/check_commit_signatures.py` fetches PR Git objects without checking them out. Git authentication exists only in the fetch child's environment. Credentials are not written to the repository or printed. It enumerates the complete base..head history, rejects shallow/incomplete enumeration, checks GitHub's independent commit count and requires every actual introduced commit to have verified=true and reason=valid, including merges. GitHub's temporary checkout test merge is not an authored PR commit.

The token can only read contents and PR metadata. The Actions job named `GitHub Verified PR commits` supplies the result. There is no separate legacy commit-status publication and no status-write permission. Provider errors, invalid metadata, unsigned commits and changed PR inputs fail the job. Unit tests cover these failure paths and trusted orchestration. No unsigned project commits are created by tests.

GitHub performs the signature verification and displays the individual commit's Verified badge. Our checker consumes that result and applies the repository policy to every commit introduced by the PR. It does not implement another cryptographic verifier.

## Trusted-base activation

Workflow and checker changes become active only after merging onto the trusted default branch. An unmerged target-workflow edit cannot change the installed gate or validate itself. Verify the active workflow against the intended base and current head before relying on its result.

## Accepted enforcement limitation

This repository uses signature verification as a CI quality gate. Do not assume
that a failing or missing check technically prevents a writer from merging.
Contributors and the merge coordinator must verify the required results. Making
the repository public does not itself configure server-enforced rules. Inspect
the actual GitHub rules before claiming that they enforce this policy.

This check verifies signatures but is not an adversarial security boundary against same-repository writers. Loading trusted base code prevents this verifier from executing PR code, but does not make a CI-only policy server-enforced. Server-enforced signed-commit rules or an independently trusted publisher would be needed for stronger enforcement.

Commit metadata comes from GitHub's Git commit endpoint, which excludes file diffs. Large diffs therefore do not consume the response-size budget for signature verification.
