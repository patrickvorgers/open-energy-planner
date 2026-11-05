# Contributing

Every added or changed feature requires a documentation-site impact assessment.
Check the beginner route, visible labels, installation/configuration steps,
defaults, supported platforms, everyday behavior, limitations and troubleshooting.
Update affected canonical guides in the same PR, or explain why no site update
is needed. Reviewers must verify that decision. Keep one maintained source.
See [documentation-site maintenance](docs/development/DOCUMENTATION-SITE.md#maintain-one-source).

Read the [security policy](SECURITY.md) before reporting a vulnerability or
attaching logs, screenshots or example configurations. Use synthetic inputs
instead of personal measurements or credentials.

## Screenshots for interface changes

Every PR that changes the UI must show screenshots directly in its description.
Capture the actual changed interface with synthetic data at desktop and mobile
sizes. Include relevant states, such as the latest and historical selections
when those behave differently. Inspect every image for private information.

For charts, choose a representative period with enough data points and variation
to assess the changed behavior, scales, legends, date boundaries and gaps. Daily
price and energy patterns should show a representative full day or the relevant
planning horizon. Explain the selected period when it matters to the change.
A sparse test fixture with only a few points may prove a focused regression, but
it does not provide sufficient visual evidence for the PR. Keep focused tests
small and use representative synthetic data for visual review.

Upload native GitHub attachments instead of committing temporary PR screenshots
to the repository or creating screenshot branches. GitHub CLI supports this
through `--attach`. Check that `gh pr edit --help` lists that option. Preserve
the existing PR description in the body file, then attach the reviewed images:

```sh
gh pr edit PR_NUMBER --repo OWNER/REPOSITORY \
  --body-file /absolute/path/pr-description.md \
  --attach /absolute/path/desktop.png \
  --attach /absolute/path/mobile.png
```

Repeated `--attach` options upload multiple images. Matching local Markdown image
references in the body file are rewritten to GitHub attachment URLs. Other
attachments are appended as inline images. Use descriptive alt text and verify
the uploaded images load from the final PR description. A download link or CI
artifact link is not a substitute for screenshots visible in that description.
Use existing authorized credentials without logging or persisting them.

The ChatGPT Chrome extension file chooser is an alternative when its existing
file-URL permission allows uploads. Do not expand permissions or take over the
native desktop merely to upload screenshots. If neither upload route works,
report the missing inline evidence explicitly and retain synthetic captures in
CI artifacts until it can be resolved.

See [GitHub's attachment procedure](https://docs.github.com/en/github-cli/github-cli/attaching-files-with-github-cli).

## Development and review

AI-assisted development also follows the repository [AI development agreements](AGENTS.md), including evidence-based handling of all Codex, CodeRabbit and Kody-ai review findings.

At least one automated reviewer, CodeRabbit or Kody-ai, must complete a substantive review of the latest PR head before Ready for review or merge. Every new commit, including documentation-only changes, requires a completed review of that head from either provider. Evaluate and address every finding from all reviewers, including outside-diff comments, and resolve the evaluated threads. Passing CI, no comments, pending or skipped reviews, an unavailable provider and older-head reviews do not prove completion. Recheck the head SHA immediately before merging.

CodeRabbit is the preferred independent PR reviewer, with Kody-ai as the fallback after a confirmed rate limit. Do not add a separate local Codex reviewer, reviewer subagent or Codex Cloud review stage. Builders still inspect their own changes, run affected tests and evaluate every existing finding from any reviewer. Keep automatic Codex reviews disabled.

Temporary exception: PRs authored by `dependabot[bot]` do not require automated review, and manual CodeRabbit reviews must not be requested for them. Passing CI, verified commits, dependency compatibility and regression-coverage assessment remain required. Any existing findings must still be evaluated and addressed.

All documentation, directory/file names, code, comments, UI text and tests use English. Original revisions remain accessible through Git. Maintained specifications are in `design/`. Product and site documentation describe current behavior and help future maintenance. Keep delivery history, PR/review references and temporary test receipts in PRs or CI artifacts. Update existing guides instead of creating a report for each change. See the [maintained-guide policy](docs/development/CODE-DOCUMENTATION.md#maintained-guides).

Code must follow the [code documentation standard](docs/development/CODE-DOCUMENTATION.md), based on PEP 257 and Home Assistant's Google-style docstrings. Document every authored production module, class, function and method. Expand non-obvious contracts and explain decisions near the code. Keep documentation accurate in the same PR as the behaviour change. Reviewers assess usefulness and completeness, not a blanket coverage percentage.

Ruff rejects missing/empty public docstrings in production code, scripts, tests and browser tests. Every test function needs a short description of its situation and expected behaviour. Planner scenarios also explain the independent reason for their expected outcome. Document assumptions in shared fixtures/helpers and keep numerical expectations independent of solver output. Existing code is brought into compliance when introducing a control. Do not introduce a baseline or legacy exemptions to defer violations. Internal helper documentation and semantic accuracy remain review requirements.

Read the [release scope](design/FIRST-RELEASE-SCOPE.md) and [development guide](docs/development/README.md). Open a short-lived branch and submit a pull request. Keep changes focused and identify the requirement they implement. Do not change independent scenario expectations merely to match a new solver result.

Use the [real Supervisor developer environment](docs/development/DEVELOPER-ENVIRONMENT.md)
for local app installation and integration work. Run `python -m script.supervisor.dev start`
from a checkout. Personal HA OS machines are not required development or merge gates.

During development, run `python script/ci/check_native.py --quick` and affected
regression tests before each push. Group related review corrections into one
tested push. Build and review in Draft. Keep each PR within 150 changed files,
including binary, removed and renamed files. Split a larger PR before review.
First wait for current-head Quick, verified commits and CodeQL to pass and
evaluate their findings. Inspect your own changes, batch corrections and retest.
The selected automated provider supplies the independent review after those builder checks.

Before requesting an automated review, ask CodeRabbit for fresh availability
with `@coderabbitai rate limit` on the PR. This does not start a review. Use
CodeRabbit when included capacity is available and Kody-ai when CodeRabbit
explicitly reports no capacity. A confirmed rate limit after a positive check
also permits Kody fallback. Unknown status and ordinary failures do not.
Record the response and selected provider, avoid concurrent or duplicate
requests, and never enable paid credits. Both completed current-head reviewers
remain equally valid for CI. See [provider selection](docs/development/CI-STAGES.md#choose-the-reviewer-before-requesting-a-review).

Develop Drafts and run builder checks in parallel, but request
final automated reviews in the intended merge order. Wait for the preceding PR
to merge. Include that actual main in this
PR, resolve conflicts, retest affected behavior and wait for current-head Quick,
signature and CodeQL checks. Reassess material changes before final review.
Only after that work is complete, freeze the final head and base. Admission verifies
that the frozen head includes the frozen base for either reviewer. When choosing CodeRabbit, run the
**Manual CodeRabbit review preflight** workflow from `main`, with those SHAs,
and an affirmative existing-finding-disposition attestation.
Follow its job summary to request one manual CodeRabbit review. Automatic,
incremental, label and description-keyword review triggers are disabled regardless
of star count or paid plan. Retain the Dependabot exception above. All chats share
one coordinated review queue. Respect actual provider limits and never retry
automatically. The read-only preflight neither spends nor reserves review quota.
Always verify actual completed current-head analysis, not merely a green provider
status. See [review and CI stages](docs/development/CI-STAGES.md) for the procedure
and its limits. Kody-ai is an alternative with the same independent-review,
merge-order and finding-disposition requirements. The CodeRabbit-specific
preflight is not required for Kody-ai.
Automated review may run while the preceding merge's main CI is pending.
Wait for that main CI to succeed before choosing Ready, requesting candidate CI
or merging the next PR.
After latest-head review and disposition of every finding, resolve the evaluated
threads and choose **Ready for review** to start one selected candidate automatically.
A PR returns to Draft when opened, reopened, given a new commit or retargeted.
Title and description edits do not reset readiness. A new commit cancels the old candidate and
needs a fresh Ready transition after review. Missing or skipped required selected checks
cannot authorize a merge. See [review and CI stages](docs/development/CI-STAGES.md).

Use local Quick checks and affected regression tests for development feedback.
Include targeted browser, HA, database or Supervisor checks when the change needs
them. Run the complete required suite set once through the GitHub candidate workflow. Do not require
a duplicate full local run before requesting that candidate. Use
full local `python script/ci/ci.py` runs only for a concrete diagnostic reason or
an explicit request for that full run. Record the reason and actual result in
the PR. Focused `--profile` and `--documentation-only` runs remain valid local checks.
An informal local result does not replace the candidate merge gate. Candidate CI retains Ruff
and formatting and executes the suites selected by the immutable base policy.
Those suites include native/planner, database, app smoke, Home Assistant handoff
and desktop/mobile browser checks when required by the matrix. CodeRabbit or
Kody-ai supplements executable checks and requires its GitHub App to have repository access.

All candidate jobs use GitHub-hosted Ubuntu runners. No developer-owned runner,
runner preference label or availability credential is needed. See
[GitHub-hosted execution](docs/development/CI-STAGES.md#github-hosted-execution).

Contributors need no CI labels for the normal GitHub-hosted route. Ready attests
that all findings, including outside-diff comments, were evaluated and addressed.
The admission gate verifies current-head Quick, signatures, CodeQL, a substantive
CodeRabbit or Kody-ai review, inclusion of the intended base and no unresolved discussions. Use `ci: run` only for a deliberate retry on a frozen,
reviewed candidate. It cannot bypass those gates. No periodic checks or automatic
retries of failed tests are scheduled.

Candidate CI follows the [change-based matrix](docs/development/CI-STAGES.md#change-based-candidate-matrix). The immutable base policy selects suites from the whole PR combined with its intended main base. Unknown impact selects full validation. Review, signing and merge requirements apply to every route. Manual and release runs remain full. Main may reuse an identical successful full or independently verified focused product candidate through the [checked reuse gate](docs/development/CI-STAGES.md#reuse-an-identical-tested-merge-on-main). Strict owned Python test-only candidates additionally permit [profile-bound report reuse](docs/development/CI-STAGES.md#strict-test-only-candidates-and-main-merges), without an installation bundle or deployment identity. Mixed known changes retain their complete required suite union. Unknown impact, differing trees and missing proof retain full main validation. Focused product reuse requires both original bundle and report artifacts and never proves full validation or authorizes deployment. Local `--profile` flags do not establish PR eligibility.

Query volume checks have a separate, additive `performance-queries` group. The original matrix, including `full`, excludes it. The independent query-impact decision adds it when needed and publishes a magenta `ci-scope: performance-queries` label alongside the ordinary scope labels. SQL/storage, schema, database dependencies and performance-fixture changes retain it before merge. Small correctness cases stay in `databases`. See [query performance ownership](docs/development/CI-STAGES.md#query-performance-ownership) for collection, execution evidence and exact-tree main reuse. There is no nightly run.

Sign every commit with a signing key registered to your GitHub account. Verify the resulting commit shows **Verified** on GitHub. Do not disable signing to bypass failures. Use the host signing setup or a forwarded signing agent. Never put a private signing key into an image or repository. Windows host-specific signing paths may not work inside Linux. In that case commit on the host until agent forwarding has been configured and verified.

Dependency updates require compatibility review and an assessment of meaningful regression coverage. Versions ship only as a jointly tested release. AMD64 is primary. No ARM artifact is released until the required native checks pass. Historic snapshots and test reports are evidence, not additional supported production implementations.

Contributions are offered under AGPL-3.0-only, the project licence. Contributors retain their copyright. Third-party components retain their own terms and notices. Do not submit credentials, production HA data or copies of third-party implementation code without checking its licence.
