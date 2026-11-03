# Contributing

Read the [security policy](SECURITY.md) before reporting a vulnerability or
attaching logs, screenshots or example configurations. Use synthetic inputs
instead of personal measurements or credentials.

AI-assisted development also follows the repository [AI development agreements](AGENTS.md), including evidence-based handling of all Codex and CodeRabbit review findings.

CodeRabbit is the only mandatory automated reviewer. Before merging, its review must be complete for the latest PR head commit, and every finding must be evaluated and addressed. Any new commit, including documentation-only changes, requires CodeRabbit review of the new head. Passing CI, an older review or an unavailable CodeRabbit service does not meet this requirement. Recheck the head SHA immediately before merging.

Codex Cloud review is optional and runs only on an explicit human request for that service and PR. Keep automatic Codex reviews disabled for this repository. Do not routinely request or repeat Codex Cloud reviews after commits. A general request to check a PR does not authorize a cloud review. Existing findings from any reviewer still require evaluation and resolution.

Temporary exception: PRs authored by `dependabot[bot]` do not require CodeRabbit review, and manual CodeRabbit reviews must not be requested for them. Codex Cloud review is also optional and requires an explicit user request. Passing CI, verified commits, dependency compatibility and regression-coverage assessment remain required. Any existing findings must still be evaluated and addressed.

All documentation, directory/file names, code, comments, UI text and tests use English. Original revisions remain accessible through Git. Maintained specifications are in `design/`. Product and site documentation describe current behavior and help future maintenance. Keep delivery history, PR/review references and temporary test receipts in PRs or CI artifacts. Update existing guides instead of creating a report for each change. See the [maintained-guide policy](docs/development/CODE-DOCUMENTATION.md#maintained-guides).

Code must follow the [code documentation standard](docs/development/CODE-DOCUMENTATION.md), based on PEP 257 and Home Assistant's Google-style docstrings. Document every authored production module, class, function and method. Expand non-obvious contracts and explain decisions near the code. Keep documentation accurate in the same PR as the behaviour change. Reviewers assess usefulness and completeness, not a blanket coverage percentage.

Ruff rejects missing/empty public docstrings in production code, scripts, tests and browser tests. Every test function needs a short description of its situation and expected behaviour. Planner scenarios also explain the independent reason for their expected outcome. Document assumptions in shared fixtures/helpers and keep numerical expectations independent of solver output. Existing code is brought into compliance when introducing a control. Do not introduce a baseline or legacy exemptions to defer violations. Internal helper documentation and semantic accuracy remain review requirements.

Read the [release scope](design/FIRST-RELEASE-SCOPE.md) and [development guide](docs/development/README.md). Open a short-lived branch and submit a pull request. Keep changes focused and identify the requirement they implement. Do not change independent scenario expectations merely to match a new solver result.

Use the [real Supervisor developer environment](docs/development/DEVELOPER-ENVIRONMENT.md)
for local app installation and integration work. Run `python -m script.supervisor.dev start`
from a checkout. Personal HA OS machines are not required development or merge gates.

During development, run `python script/ci/check_native.py --quick` and affected
regression tests before each push. Group related review corrections into one
tested push. Build and review in Draft. While the repository has fewer than 10
GitHub stars, request CodeRabbit manually with `@coderabbitai review` after every
new PR head. Retain the Dependabot exception above. Always verify that a review
actually completed on the current head instead of assuming automatic execution.
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
and desktop/mobile browser checks when required by the matrix. CodeRabbit
supplements executable checks and requires the GitHub App to have repository access.

GitHub-hosted execution is the default and requires no developer-owned runner.
Only the personal repository owner can apply `ci-runner: prefer-local` before
choosing Ready to use their Linux x64 runner in Docker
when it is online and idle. Other contributors always use GitHub, even with
that preference label. The runner label is the generic `oep-ci-local`. Missing, offline,
busy or unreadable capacity automatically falls back to GitHub. Both routes run
the same workflow, suite selection, reports and installation packaging. See
[personal runner setup and limits](docs/development/CI-STAGES.md#personal-runner-execution)
for the one-time registration, read-only availability credential and location labels.

Contributors need no CI labels for the normal GitHub-hosted route. Ready attests
that all findings, including outside-diff comments, were evaluated and addressed.
The admission gate verifies current-head Quick, signatures, CodeRabbit and no
unresolved discussions. Use `ci: run` only for a deliberate retry on a frozen,
reviewed candidate. It cannot bypass those gates. No periodic checks or automatic
retries of failed tests are scheduled.

Candidate CI follows the [change-based matrix](docs/development/CI-STAGES.md#change-based-candidate-matrix). The immutable base policy selects suites from the whole PR combined with its intended main base. Unknown impact selects full validation. Review, signing and merge requirements apply to every route. Manual and release runs remain full. Main may reuse an identical successful full or independently verified focused product candidate through the [checked reuse gate](docs/development/CI-STAGES.md#reuse-an-identical-tested-merge-on-main). Strict owned Python test-only candidates additionally permit [profile-bound report reuse](docs/development/CI-STAGES.md#strict-test-only-candidates-and-main-merges), without an installation bundle or deployment identity. Mixed known changes retain their complete required suite union. Unknown impact, differing trees and missing proof retain full main validation. Focused product reuse requires both original bundle and report artifacts and never proves full validation or authorizes deployment. Local `--profile` flags do not establish PR eligibility.

Query volume checks have a separate, additive `performance-queries` group. The original matrix, including `full`, excludes it. The independent query-impact decision adds it when needed and publishes a magenta `ci-scope: performance-queries` label alongside the ordinary scope labels. SQL/storage, schema, database dependencies and performance-fixture changes retain it before merge. Small correctness cases stay in `databases`. See [query performance ownership](docs/development/CI-STAGES.md#query-performance-ownership) for collection, execution evidence and exact-tree main reuse. There is no nightly run.

Sign every commit with a signing key registered to your GitHub account. Verify the resulting commit shows **Verified** on GitHub. Do not disable signing to bypass failures. Use the host signing setup or a forwarded signing agent. Never put a private signing key into an image or repository. Windows host-specific signing paths may not work inside Linux. In that case commit on the host until agent forwarding has been configured and verified.

Dependency updates require compatibility review and an assessment of meaningful regression coverage. Versions ship only as a jointly tested release. AMD64 is primary. No ARM artifact is released until the required native checks pass. Historic snapshots and test reports are evidence, not additional supported production implementations.

Contributions are offered under AGPL-3.0-only, the project licence. Contributors retain their copyright. Third-party components retain their own terms and notices. Do not submit credentials, production HA data or copies of third-party implementation code without checking its licence.
