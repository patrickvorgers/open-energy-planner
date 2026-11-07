# Review and CI stages

PR development uses Draft for building and reviewing, followed by a deliberate
Ready for review transition for one selected candidate run. This reduces repeated heavy runs while keeping the final merge evidence.
It does not remove tests or change app behaviour.

## Find the right procedure

| Task | Start here |
|---|---|
| Prepare a revision | [During development](#during-development) |
| Coordinate multiple PRs | [Follow the intended merge order](#follow-the-intended-merge-order) |
| Request independent review | [Choose the reviewer](#choose-the-reviewer-before-requesting-a-review) |
| Understand selected tests | [Change-based candidate matrix](#change-based-candidate-matrix) |

Local checks, review, candidate CI, merge and main reuse establish different evidence. Follow the complete applicable procedure before claiming readiness.

## During development

Each PR opening, new revision or reopening runs
`Quick checks`. It runs the
centrally pinned Ruff lint and formatting checks, followed by CI policy tests.
It installs the central development dependencies, Ruff and pytest, without
building Docker images or installing app and HA dependencies. Trusted signature
verification remains a separate automatic check.

Automatic and incremental CodeRabbit reviews are disabled. Positive label and
description-keyword opt-ins are also empty because they could otherwise trigger
reviews even with automatic review disabled. This policy applies to every star
count and paid plan. Keep the PR in Draft while assessing
findings and batching corrections. The trusted base workflow returns newly
opened, reopened, revised or retargeted PRs to Draft. This includes PRs opened
with GitHub's normal Ready option. Title and description edits do not reset
readiness. The transition does not change source, labels or review history.

Run `python script/ci/check_native.py --quick` and the affected regression tests
locally before pushing. Review findings remain evidence to assess. Fix valid
findings and group related corrections into one tested push. Do not push each
comment fix separately. Large independent corrections can still form separate
reviewable commits in that push. Do not rewrite already reviewed published
history merely to reduce the commit count.

Local validation provides focused development feedback. Include affected
desktop/mobile browser flows and targeted HA, database or Supervisor checks when
the changed behavior needs that evidence. The complete suite set selected by the
immutable base policy runs once through the GitHub candidate workflow. Do not automatically repeat
that entire set locally as a prerequisite for requesting candidate CI. A full
local `python script/ci/ci.py` run is reserved for a concrete diagnostic reason or
an explicit request for that full local run. Record its reason and actual result in the PR.

Given identical source trees and profiles, local and hosted runs use the same
test orchestration and pinned container dependencies. Host capacity, networking
and timing can differ. The candidate workflow additionally establishes admission,
package identity and artifact evidence on either execution route. An informal
local success does not replace those merge or reuse gates.

Wait for the active review to complete before collecting its full batch of inline
and outside-diff findings. Evaluate every finding, apply valid corrections and run
affected checks before publishing one tested push. Request one incremental review
of the new head, without duplicate requests while it is pending. Further findings
start another batch. A full PR review is a deliberate reassessment of the complete
PR diff, not the default after each correction. Latest-head review and disposition
of every finding remain mandatory. Once candidate CI starts, keep that candidate
frozen unless a new finding or failure requires a correction.

## Follow the intended merge order

Develop Drafts and run builder checks in parallel. Agree on one
merge queue before requesting final automated reviews. Review availability is
not a reason to review a later PR before its predecessor has merged.

For a queue of PR A followed by PR B, finish A's review, required candidate CI,
and guarded merge first. Then include that actual main
in B, resolve conflicts and run Quick, affected regression tests and CodeQL on
the updated head. Inspect material changes and batch corrections. Only
then freeze B's head and base and request its final automated review. Complete
B's selected candidate CI before merging B. Automated review may run while A's
main CI is pending. A's required main checks must succeed before B becomes Ready,
requests candidate CI or merges. Repeat this sequence for the next PR.

Do not request a second review while the first is running or push speculative
base updates during a final review. If main advances before merge, synchronize
and validate again. A review of an older head is not review evidence for the new
head. The coordinator records the actual queue in PR discussions or the active
coordination chat, not in maintained product documentation.

The preflight verifies that the selected current main commit is an ancestor of
the frozen head and rechecks the current base and head before emitting a command.
It cannot prove the human queue order. The operator must still confirm that all
preceding PRs have merged before requesting review, and verify their successful
main checks before Ready, candidate CI or merge.

## Choose the reviewer before requesting a review

After the current-head cheap checks pass and the coordinated review slot is
available, a repository writer or coding agent deliberately requests review by
posting the following comment. A coding agent needs no additional human
confirmation once those prerequisites and the coordinated slot are ready:

```text
@oep review
```

This exact standalone comment is the manual trigger and the operator's
attestation that existing findings have been dispositioned. It authorizes review
of the live Draft revision accepted by the workflow. Do not push while the
request is being accepted. Pushes, Ready transitions, labels and check completion
do not independently request reviews. The workflow does not mark the PR Ready,
start candidate CI, resolve findings or merge.

The trusted-main **OEP review orchestration** workflow reuses the existing
preflight, checks the requester's current write permission, and records the head,
base and progress in an **OEP review** PR comment. It applies this order:

1. Post `@coderabbitai rate limit` on the PR and wait for CodeRabbit's fresh reply.
   This checks included review availability without starting a review, as
   documented in [CodeRabbit's rate-limit guide](https://docs.coderabbit.ai/management/rate-limits).
   Use the actual response for this request. Do not infer capacity from a fixed
   plan allowance, an earlier review or a stale reply from another PR.
2. If included capacity is available, request one CodeRabbit review of the frozen head.
3. If CodeRabbit explicitly reports no included capacity, request Kody-ai once
   using `@kody start-review` with `<!-- oep-kody-head:<current-head-sha> -->`.
   A positive availability check does not reserve capacity. If the subsequent
   CodeRabbit request is explicitly rate-limited, switch to the same Kody route
   without immediately retrying CodeRabbit.
4. If Kody completes its review, stop regardless of whether it found issues.
   A substantive review is not a promise that the code is correct. The coding
   agent still evaluates and addresses every finding before Ready for review.
5. If Kody explicitly fails technically, wait until CodeRabbit's reported retry
   time and query its capacity again. A completed `timed_out` check or a failed
   check titled `Code Review Failed` is recognized as technical failure.
   Skipped, ambiguous and incomplete results are not technical-failure evidence.
   Only a fresh positive quota response permits another CodeRabbit review.

Provider comments and completed provider checks resume requests. A short scheduled
check also runs at minutes 7, 22, 37 and 52 of each hour, so delayed retries do not
hold a runner open and do not depend on a coding agent staying online. GitHub may
delay scheduled runs. Fifteen minutes is the intended polling interval, not a
guaranteed retry deadline. A manual workflow dispatch resumes the same durable
requests and does not independently authorize a review. The PR comment is also
the durable inbox if Actions replaces a queued trigger. Repeated commands for
active work do not duplicate it. Discovery checks each candidate's current
permission. A known authorization denial is skipped without consuming the command
or stopping active work. An unavailable permission check and an explicitly handled
unauthorized command still fail closed. Each acknowledged command is durably consumed
independently of the original request identity. A later push cannot revive an
older duplicate as a new request.

Each request permits at most eight quota queries, two actual CodeRabbit requests
and one Kody request. It expires after seven days. Missing quota replies block
after fifteen minutes. A genuine but stale quota reply triggers a fresh bounded
quota query only when no provider is active. Its old availability never authorizes
a review. Generic review acknowledgements are not quota evidence and do not
expire an active review. Reviews without an unambiguous completion block after
two hours without starting an overlapping provider. Scheduled execution determines
when these limits are observed. A head/base change, a newer main, closure or a
Ready transition supersedes pending work. Synchronize and validate an updated
Draft, then explicitly request review of that revision.
If a confirmed quota limit supplies no usable retry time, Kody may still review.
If Kody then fails technically, the request blocks instead of inventing a
CodeRabbit retry deadline.

The controller writes a durable intent before each provider command. If a worker
loses the publication response, it recovers a uniquely marked command from the PR
instead of sending it again. An uncertain outcome without that command blocks.
Losing the subsequent status-confirmation response also preserves that outbox.
Global workflow serialization protects these operations. A direct manual
provider command can still race with the controller. Do not mix both routes.

Nullable or malformed GraphQL mutation payloads use the same canonical failure
path and retain an unresolved publication intent for reconciliation.
Public failure messages use fixed categories with an inspection action. They
never copy exception values or provider payloads. A malformed, over-limit or
unavailable PR does not prevent the scheduled sweep from checking other PRs.
The workflow still reports failure if a PR cannot be processed or becomes blocked
during that sweep.
An unavailable or invalid global PR inventory also fails the workflow.

For a **Blocked** status, inspect its explanation, the Actions run, actual provider
checks and all findings. A new explicit request can reconsider failed prerequisites
after they are fixed. It cannot bypass an existing review command or unresolved
provider work. Preserve request markers. Use the manual recovery procedure below
only after establishing what actually ran. Unknown response formats require a
tested parser update rather than guessing availability or retrying blindly.

The workflow uses only the built-in workflow token with metadata reads and PR
comment writes. It checks out trusted `main`, never contributor code, and does
not require a personal token, a local runner or a new dependency. Provider support
for commands posted by `github-actions[bot]` must be verified on the activated
workflow. A missing reply blocks visibly and does not enable another identity,
paid credits or a billing change.

Record the availability reply and chosen reviewer in the PR. Recheck the frozen
head and base before sending either review command. Never launch both reviews
in parallel, repeat an uncertain request or enable usage billing or paid credits.
An unavailable status reply, provider outage or generic failure is not evidence
of quota exhaustion. Stop and report that condition instead of guessing or
silently switching providers. The bounded delayed retry above is permitted only
after Kody's explicit technical failure and fresh CodeRabbit quota evidence.
The existing Dependabot exception remains.

This is the order agents use when requesting reviews. It does not change the
candidate-admission rules. A completed substantive review of the current head
from either CodeRabbit or Kody-ai remains valid for Ready, CI and merge. All
findings from every reviewer still require evaluation, replies and resolution.

## Prepare one final CodeRabbit review

This direct-provider route remains available for deliberate manual recovery or
when diagnosing the orchestrator. Prefer `@oep review` for ordinary requests.
Do not run this route alongside active orchestrated work.

Keep every PR within **150 changed files**. All files count, including binary,
removed and renamed files. Split a 151-file PR. Do not hide files with provider
path filters to fit the limit.

This is the repository's PR ceiling, not a guarantee that every provider plan
processes 150 files. CodeRabbit documents 150 files for Essentials and 100–300
for open-source repositories depending on popularity. Inspect the actual review's
processed and excluded files against the PR inventory. Split smaller when the
provider's applicable limit would leave a partial review.

First obtain successful current-head Quick checks, GitHub Verified PR commits and
CodeQL. Evaluate genuine findings and document any evidence-based disposition.
Inspect your own changes, batch corrections, run affected checks and reassess
material scope changes before consuming an external review. CodeRabbit provides
the independent PR review. Do not add a separate local Codex reviewer, reviewer
subagent or Codex Cloud review stage. Existing findings from any reviewer still
require evidence-based disposition.

After the preceding queued merge is complete, include and test the actual current
main before freezing the final head and base. For this deliberate direct-provider recovery route, run **Manual CodeRabbit review preflight** from
`main` with the PR number and expected head/base. Affirm that existing findings
are dispositioned. No Codex review SHA or independent-Codex attestation is needed.
The preflight uses only
trusted main code and read-only metadata. It checks the file inventory, current
Quick/signature/CodeQL results, resolved discussions, existing CodeRabbit activity
and trusted same-head CodeRabbit or Kody request markers, including commands
already published by the Actions controller.

The successful job summary contains the manual comment to post after rechecking
the frozen head and the shared queue:

```text
@coderabbitai review
<!-- oep-coderabbit-head:<current-head-sha> -->
```

The summary does not request a review, reserve quota or durably lock a request.
Two successful preflights can still lead two people to post the command. The
workflow concurrency group serializes preflights, not provider review capacity.
All chats must coordinate one queue and use actual provider availability, not a
hardcoded hourly allowance. Keep this conservative procedure during paid months
too. Never remove a request marker, push an empty commit or retry CodeRabbit
automatically to evade the duplicate guard. The orchestrator's bounded delayed
recovery is the documented exception, with a separate durable action marker.
A confirmed rate limit selects the
Kody-ai fallback described above after the frozen identities and prerequisites
are rechecked.

For that fallback, inspect the actual availability reply or rate-limit result
and confirm that no substantive review ran. Recheck the exact current head and
base, Quick/signature/CodeQL results, finding disposition and the absence of
running or completed provider work. Request Kody-ai once with its own head
marker. Do not repeat the existing CodeRabbit request or wait for its quota to
refill when Kody-ai can review the frozen revision.

Native automatic, incremental, positive-label and description-keyword triggers
are disabled. GitHub cannot prevent a person with access from directly posting a
CodeRabbit command. The operator attestation is not machine proof of finding
correctness. No preflight receipt is a merge credential.
Candidate admission separately enforces the file limit and actual current-head
check evidence. It accepts either a substantive CodeRabbit result with completed scope,
workspace, analysis, feedback and publication phases, or a substantive Kody-ai result. A green skipped or
rate-limited provider check does not meet that requirement. Unknown provider
output fails closed and requires investigation instead of weakening the gate.
Completed phases alone do not prove substantive coverage of every changed file;
the operator must also check the actual review scope and exclusions.

Dependabot retains its documented CodeRabbit exception. Do not request manual
CodeRabbit reviews for it. Its file limit, signatures, CodeQL, compatibility and
required CI evidence still apply.

## Kody-ai alternative

Kody-ai can satisfy the same latest-head automated-review prerequisite as CodeRabbit.
The candidate workflow validates `Kody Code Review` from GitHub App `413034`
(`kody-ai`). It requires a completed successful check with the provider's
`Code Review Complete` title and successful review-publication summary. Starting,
skipped, warning, failed, unknown and older-head results do not qualify. The
latest substantive Kody attempt supersedes earlier Kody evidence on that head.
An existing automatic-disabled Kody skip does not prevent an explicit orchestrated
request. It remains metadata only and never supplies substantive review evidence.
A completed `Code Review Skipped` check whose summary is exactly
`No New Commits (No changes detected since last review)` is metadata from the
unchanged revision. It preserves an earlier substantive success on that same
head, but cannot supply missing review evidence or hide a later pending or
failed review. Other skipped or unknown results still fail admission. Either provider
may satisfy admission independently of the other provider's availability.
The CodeRabbit-specific preflight and manual command above remain specific to
CodeRabbit. They are not prerequisites for choosing Kody-ai.

The same independent local review, coordinated merge order, current-head cheap
checks and finding-disposition requirements apply to either reviewer. Inspect
actual scope and exclusions. Address findings from both providers when both ran.
A successful check does not prove that all findings have been addressed.

## Request candidate CI

Ready for review starts candidate CI, not another CodeRabbit review. Native
automatic review routes stay disabled, while the explicit manual command remains
available. Admission reuses substantive review evidence for the exact same head,
even if a later automatic metadata check is skipped. It reads the complete check
history and rejects later pending or failed attempts from the selected provider. A new head must obtain
its own completed review before admission. A Ready transition must not be used to
spend another review on an already reviewed, unchanged commit.

First complete a CodeRabbit or Kody-ai review of the current head and address all findings,
including comments outside the diff. Resolve evaluated discussions, including
outdated threads. Then select **Ready for review**. That transition automatically
requests one candidate run without a CI label. For this repository Ready means
ready for final candidate validation, rather than the start of bot review.
The candidate gate requires a complete changed-file inventory within the 150-file
limit, passing Quick checks, GitHub Verified PR commits, CodeQL and a substantive
completed CodeRabbit or Kody-ai check from its actual GitHub App on that head.
The established Dependabot review exception remains in effect. An API outage,
missing evidence, pending review, wrong app or changed PR fails before image builds.
The gate checks the current base and head before and after reading the evidence.
Every reviewer route must also prove that the frozen head includes the intended
base through the GitHub comparison API. Missing, divergent or unavailable ancestry
evidence fails admission.

Admission also reads every review-thread page and rejects unresolved threads.
The review check proves review completion, and thread resolution proves
acknowledgement. Neither proves semantic correctness. Choosing Ready attests that
the operator evaluated and addressed all findings, including outside-diff
comments without a resolution flag. Inspect them again before merging. Historical
resolved comments are allowed. A completed current-head review is always required, including when it produces no comments.

Keep the candidate frozen. A new commit cancels the obsolete candidate through
the trusted readiness workflow, returns the PR to Draft and runs cheap
checks again. Follow the final-review procedure above rather than requesting
CodeRabbit on every push. After completing corrections and current-head
review, choose Ready again. An early Ready transition with missing evidence fails
before image builds. Complete the missing work and make a fresh Ready transition.
There is no candidate-CI polling workflow, periodic candidate run, special
disposition comment or automatic retry after a failed test. The separate review
resumption schedule only handles existing explicit review requests and never
starts candidate CI. The ordinary matrix still selects suites.
Ready does not force the full suite or add query performance tests by itself.

Use `ci: run` only for a deliberate retry on an already reviewed, non-Draft
candidate. It uses exactly the same admission gates as Ready. Wait until Quick
checks and signature verification finish. Request another run by removing and reapplying it:

```console
gh pr edit NUMBER --remove-label "ci: run"
gh pr edit NUMBER --add-label "ci: run"
```

Keeping the label on a PR does not automatically run heavy tests after another
push. A new PR revision starts cheap checks and cancels the obsolete candidate
through the trusted readiness workflow, including merge-conflict cases. Quick
checks use their own concurrency group and continue when the PR becomes Draft.
Unrelated label edits do
not cancel the candidate. Title/description edits do not cancel it or rerun quick
checks. Retargeting a PR cancels the old candidate and reruns quick checks. It
returns the PR to Draft and requires a fresh Ready transition. The admission log records the checked base
and head so the operator can compare both before merging.

## GitHub-hosted execution

All candidate, main, manual and release jobs use GitHub-hosted Ubuntu runners.
Contributors need no personal runner, runner preference label or availability
credential. PR authors and labels cannot select personal hardware. The repository
does not ship runner registration, workspace repair or host cleanup tooling.

During development, run Quick and affected regression tests locally. Execute the
complete selected suite set once through the reviewed candidate workflow.
An identical tested merge can reuse the original candidate proof through the
checked main-reuse gate. Do not repeat the full suite locally as a prerequisite.

## Change-based candidate matrix

### Metadata proof ownership

Changes confined to `label_ci_scope.py`, `check_ci_candidate.py`,
`check_commit_signatures.py`, `check_main_ci_reuse.py`,
`check_suite_completion.py`, `review_preflight.py` and `scope_report.py` under
`script/ci/` select
only the `ci` suite. These modules classify changes, verify admission and
signatures, inspect original evidence or parse completion reports. They do not
execute app, HA, database, migration or volume workloads. Mixed changes retain
the union of every affected owner, including both sides of moves.

The remaining CI responsibilities have separate reviewed owners:

| Owner | Required coverage after this policy reaches main |
| --- | --- |
| `ci_scope.py`, `ci_groups.py` | CI regressions for selection and pure suite membership |
| `ha_suites.py`, `ha_shard.py` | `selected:ci,ha` for HA arguments and worker partitioning |
| `.github/workflows/ci-admission.yaml` | CI regressions for admission, Quick and evidence reuse |
| `.coderabbit.yaml`, `.github/workflows/review-preflight.yaml`, `.github/workflows/review-orchestration.yaml` | CI regressions for review configuration, the fixed trusted-main metadata preflight and bounded comment-based review orchestration |
| `proof_git.py`, `pytest_suites.py`, `ci.py`, `check_images.py`, `checks.yaml` | Full functional and query-performance coverage for actual shared execution |

`proof_git.py` owns fixed Git processes and immutable policy loading. Workflows
and reuse checks extract the complete `script` snapshot from the same immutable
base revision. They run its selector with isolated Python and site initialization
disabled. Every imported helper and package initializer must come from that
snapshot. Candidate files, `PYTHONPATH` and installed packages cannot supply policy
definitions. Legacy standalone selectors remain supported. Unsafe archives,
missing helpers and failed policy execution cannot authorize focused evidence.
Workflow outputs are staged and published only after successful selection.

The selector records authoritative summary rows in `selection.json`.
`scope_report.py` only renders those captured decisions. It never recomputes
selection or writes Actions outputs. Selection rules belong in `ci_scope.py`.
Pure membership and profile encoding belong in `ci_groups.py`.

The focused `ha` parent invokes the real worker runner, early HA contracts and
provisioning once. It does not select database transfers, query performance,
browser or Supervisor acceptance. Its JUnit requirements follow the configured
worker count. HA subgroups retain their independent feature routes. Legacy `ha`
and `full` profiles retain their original coverage. Mixed changes retain the
complete union and parent suites remove only redundant child execution.

Keep HA directories and worker arguments in `ha_suites.py` and its pure model.
Their transitive guard forbids process, Git, database and workload imports.
HA paths must remain within the real HA integration roots. Keep actual process
launch in the existing runners. The reusable admission workflow has a fixed
read-only command/action contract. Adding execution, services, environment hooks
or fabricated reuse outputs fails its architecture regressions. Unknown owners
and shared execution remain conservative. These guards complement review and do
not establish an adversarial sandbox.

The manual review preflight has a separate fixed workflow contract. It may run
only from trusted main through an explicit dispatch, with read permissions and
the declared proof entrypoint. Extra commands, services, mutable contributor
checkouts, environment hooks or write permissions fail its structural checks.
Its Python imports remain inside the same transitive proof ownership guard.
Declared entrypoints are checked whenever present, including newly added files.
Unknown preflight names retain conservative coverage.

This boundary does not classify arbitrary CI changes as metadata. Changes to
shared execution, Docker setup, pytest selection or immutable Git process
adapters retain their existing functional and query-performance coverage.
Removing an old runner or provenance call does not itself grant a cheaper route.

The existing ownership guard follows proof imports through helpers and ancestor
package initializers. It allows explicit metadata capabilities and reviewed
standard-library imports. Workload libraries, arbitrary process launch, dynamic
imports and reflective execution access fail the guard. A new dependency or
capability requires an ownership decision and regression coverage. Keep process
execution in its reviewed owner rather than widening the proof boundary. This
static architecture guard complements review. It is not an adversarial Python
security sandbox.

Quick checks already run CI regressions without app dependencies or Docker.
The candidate's native CI service has no network, database services or Docker
socket. Do not add them to make a metadata proof module pass. Changes to this
execution environment retain conservative coverage. Signature verification,
current-head review, suite completion and exact-tree reuse gates still apply.

### Request and informational labels

Ready for review requests a candidate. `ci: run` requests a deliberate retry.
Neither requires every suite. Advisory
`ci-scope: *` labels show the planned test selection for the current PR revision,
never execution status or success. Independent selections receive one label per
selected group. For example, `selected:browser,ci,unit-frontend` produces
`ci-scope: browser` and `ci-scope: frontend`. The frontend label represents
`unit-frontend`. Other independent groups retain their exact suite names, such as
`ci-scope: databases` and `ci-scope: unit-app-price`. Standard `ci` checks are omitted
when other groups are selected. A selection containing only `ci` displays
`ci-scope: ci`. Outdated group labels and the former `ci-scope: focused` label are
removed during the next publication.

Documentation-only and full selections retain `ci-scope: documentation` and
`ci-scope: full`. Legacy profile names retain their original coverage for older
immutable base policies. Exact selected and omitted groups appear in Actions.
Scope labels never request a run. The former `ci: full` request label is no
longer accepted.

The readable names replace `ci:run` and `ci:scope:*`. Before merging this naming
change, coordinate a short freeze on PR creation, head pushes and base-branch
retargeting with active owners. Record open PR identities, then temporarily
disable only the scope-label publication workflow and let its queued or running
jobs finish. Verify that no publisher remains active.
Then merge and rename the existing repository labels in place, retaining their
IDs and PR associations. Verify the label inventory and re-enable publication
only after all renames succeed. Compare the remaining open PR identities before
releasing the freeze. If a PR was created or changed despite the freeze, reconcile
its current scope with the trusted publisher after re-enabling publication and
before releasing the queue. Do not apply the request label for reconciliation.
If a rename fails, retain the publication pause
while reconciling the existing IDs. This prevents a publisher from creating a
new-name label before the old label can be renamed. Do not interrupt another
PR's test candidate or disable review/signature checks during this maintenance.

Do not add the renamed request label to a PR merely to refresh its appearance.
A new explicit request still requires removing and reapplying `ci: run` after
the current-head review and checks pass.

A separate trusted-base workflow refreshes the scope asynchronously when a PR
opens, receives a commit, reopens, becomes ready, changes base branch or requests
CI. It fetches Git objects without checking out or executing PR source. The
base policy classifies the intended merge tree, including intervening changes
on main. Conflicts or unreadable Git evidence conservatively display full.
Only this label job has `issues: write` for creating repository labels and
`pull-requests: write` for applying labels to PRs. Review, signature and test jobs
retain their existing permissions. Write failures report only their HTTP status,
without provider bodies or credentials. Auto-generated label events do not cancel or start
candidate tests or recursively restart the label publisher. Title and description
edits use independent ignored concurrency groups and cannot cancel publication.
Events captured for closed PRs also use ignored groups, including delayed events
that arrive after a PR reopens.

The publisher rejects outdated or closed PR ranges before writes. It preserves
user labels and the request label while replacing old scope labels. Publication
is asynchronous and labels can also be edited manually. They are never merge
evidence and never select tests. The unprivileged candidate independently
recomputes its scope from the same immutable base policy. Check its admission
and scope logs for the actual head/base and executed suites.

The trusted workflow activates after this change reaches main. Local regression
tests cover classification, stale events, label replacement and its privilege
boundary before activation.

Candidate admission, latest-head CodeRabbit or Kody-ai review, evaluation and replies for
all findings, GitHub Verified commits and the final head/base identity checks
are mandatory for every route. The matrix selects executable tests. It never
weakens review or merge requirements. The Dependabot exception remains unchanged.

### Early packaged-image checks

After building the selected images, Actions runs a separate packaged-image check
before any long suite. Native, database, HA and browser collection use
`script/ci/pytest_suites.py`, the same argument owner used by actual execution.
Collection replaces the JUnit report option with `--collect-only`, preserving
test paths, filters and plugins. Both HA workers use the real partition plugin.
Provisioning modules are imported without invoking their guarded entry points.

Checks run inside the built images without source bind mounts or dependent
services. Database collection retains its configured four-backend selection but
does not connect to the servers. A load failure stops the job before the long
tests. Cleanup and report preservation still run. Actions log groups identify
each suite and image. The job summary records success or failure, duration and
command, with failure output and the log path within the artifact. The test reports
artifact includes each complete `preflight-*.log`, including import tracebacks.

Collection proves that tests and imports load. Fixtures, database connections,
browser startup, numerical results and runtime concurrency remain the responsibility
of the complete selected suites. Actual app health and Supervisor acceptance remain.
`python script/ci/ci.py` runs both stages locally. Actions uses `--stage preflight`
followed by `--stage tests` so a failed first step prevents execution.

A local full-profile measurement took 23.32 seconds including container startup:
native 4.73, databases 3.44, HA workers 6.16 and 6.23, provisioning imports 1.23
and browser 1.53 seconds. The original HA collection retained 121 and 117 cases, exactly 238
unique cases without overlap. These are local measurements, not hosted timing guarantees.

### Independent selection and compatibility

The executable policy is the complete `script` snapshot from the immutable PR
base, with `script/ci/ci_scope.py` as its selector. Actions runs the selector
with isolated Python and site initialization disabled. It reads the
complete base-to-intended-merge diff without rename detection. Both sides of a
move contribute impact. New files inherit known directories. Most-specific
prefixes win independently of declaration order.

Files under `docs/` with the supported Git mode `100644` select `documentation`,
regardless of extension.
This includes screenshots, diagrams, data examples and developer Pages assets.
They do not add runtime suites to a mixed PR or reduce the checks required by
other changed owners. Root Markdown eligibility remains explicit. Unsupported
file modes and unreadable comparisons retain their existing full fallback.
The separate developer Pages publishing workflow retains its own browser checks.

New focused selections use a canonical `selected:` value with sorted suite
names. They union exact groups instead of choosing a cumulative superset.
`ci` is always included for lint, formatting, documentation and policy tests.
Malformed selections, unsupported file modes, unclassified domains,
unclassified composition, dependencies and unclear impact retain full validation. Manual,
release and ordinary non-reused main runs remain full.

The original `native`, `frontend`, `ha`, `storage`, `integration` and `full`
profiles retain their functional matrix. None implicitly selects query-volume
tests. `full` runs all functional native, browser, HA, database and Supervisor
checks. There is no separate `functional` profile.

Query performance is an independent addition after matrix selection. For a named
profile it is recorded as, for example, `full+performance-queries`. A focused
selection adds `performance-queries` to its sorted suite union. This preserves
one exact selection identity for execution, completion and evidence without
changing the original matrix definitions. Unknown or unreadable impact falls back to the original `full` matrix without
query performance. An older immutable policy without additive selection support
also retains `full`. Explicit recognized query owners add the volume group.
Validate the optional group explicitly when introducing or changing this policy.

Trusted signature and scope-label workflows execute the immutable base's
`script/ci/` entrypoints, with legacy-path fallback for older bases. The retained
`script/check_commit_signatures.py` and `script/label_ci_scope.py` entrypoints
delegate to grouped base code so older main workflows also work with stacked
bases. Both layouts preserve failed exit statuses under isolated Python.

### Source responsibility and contract map

| Changed owner | Required groups beyond `ci` | Expensive boundaries |
| --- | --- | --- |
| `core/` and every established domain's `calculation/` under the app | `unit-app` | Reviewed calculations do not own transport, ORM or platform lifecycle. HA, browser, four-backend contracts and Supervisor can be omitted. |
| Files under `docs/` with Git mode `100644` | Documentation | Screenshots and other documentation assets do not add runtime groups. The developer Pages publishing workflow retains its own checks. |
| App `frontend/`, `web/` | `unit-frontend`, `browser` | No HA or Supervisor startup. Public pages, assets, headers and ingress rendering stay covered. |
| App `notifications/` | `unit-app`, `unit-frontend`, `browser` | Read-only source contracts, collection and the authenticated status adapter affect app consumers and generic rendering. The import-boundary test excludes domain, database, solver and HA dependencies. |
| App `activities/` | `unit-app`, `unit-frontend`, `browser`, `database-contracts` | The event projection, cursor subscription and restart bookkeeping affect app consumers and retained activity evidence. SQL remains in `storage/adapters/`. Changes to that adapter or the shared schema independently add their database, migration, performance and lifecycle contracts. |
| Existing root `planning.py` | `unit-app`, `unit-ha-contract`, `browser`, `databases`, `ha-contracts`, `ha-controls`, `ha-history`, `ha-thermal` | Shared plan construction, validation and publication retain all affected peer, device and archive contracts. This owner does not install Supervisor or provision HA. App composition and process lifecycle retain those requirements separately. New or removed root modules remain full. |
| Guarded app/domain `storage/adapters/queries/` | `unit-app`, `browser`, `database-contracts` | Read-only SQL projections retain independent query performance. Writers, schema and transactions stay outside this owner. |
| App `storage/`, a known domain's nested `storage/` or conventional `storage.py` | `unit-app`, `browser`, `databases` | Add query performance independently. All four real backends remain mandatory. Ordinary queries do not imply Supervisor or full HA. |
| App `storage/adapters/lifecycle/` | `unit-app`, `unit-supervisor`, `databases`, `ha-contracts`, `ha-connection`, `provisioning`, `supervisor` | Add query performance independently. Schema, transfer and retained startup preserve both backend and actual install/restart/update/retention proof. |
| App `home_assistant/` | `unit-ha-contract`, `ha-contracts`, `ha-controls`, `ha-history` | App-side catalogue, observation and history contracts retain genuine HA. |
| Ordinary files in established app domains | `unit-app`, `unit-ha-contract`, `browser`, plus the domain-specific groups in `DOMAIN_HA_SUITES` | Ordinary native files do not select every real HA group. App-side HA adapters, public wire, lifecycle and shared-composition changes retain their dedicated broader contracts. |
| App `supervisor/` and `script/supervisor/` | `unit-supervisor`, `supervisor` | Actual install, pairing, ingress, restart, update and retention remain. No unrelated planner, browser, four-backend or full HA matrix is inferred. |
| Integration `supervisor/` | `unit-component`, `unit-supervisor`, `ha-connection`, `supervisor` | HA's administrator WebSocket/bootstrap boundary and actual supervised installation both remain. |
| App `runtime/diagnostics/` | `unit-app`, `unit-supervisor`, `browser` | Diagnostic providers supply notification payloads consumed by browser flows. Process diagnostics do not own platform startup. Transitive imports remain checked. |
| App `runtime/` | `unit-app`, `unit-supervisor`, `ha-connection`, `provisioning`, `supervisor` | Process startup and installation contracts remain independent of unrelated browser/DB groups. |
| `script/runtime/` | `unit-supervisor`, `provisioning`, `supervisor` | Privileged startup and retained installation behavior remain covered. |
| `script/provisioning/` | `unit-provisioning`, `unit-supervisor`, `provisioning` | Ordinary definitions retain Core first/repeat/interrupted/rejected provisioning. Supervised bootstrap assertions consume these definitions natively. |
| `script/home_assistant/bootstrap/` | `unit-provisioning`, `unit-supervisor`, `ha-connection`, `provisioning`, `supervisor` | Mixed onboarding/sidebar/bootstrap requires actual lifecycle acceptance, not unrelated full calculations. |
| Other `script/home_assistant/` | `unit-provisioning`, `provisioning` | Core provisioning smoke is independently selectable. |
| App `optimization/adapters/highs/` and `optimization/adapters/process/` | `unit-app`, `ha-solver` | Native correctness and the real app-to-HA solver handoff remain. Unrelated history, thermal, database and Supervisor tests can be omitted. |
| `test_environment/oep_test_fixture/calculation/` | `unit-test-environment` | Detached energy arithmetic can run without Home Assistant. |
| `test_environment/oep_test_fixture/calculation/recorder/` | `unit-test-environment`, `ha-test-environment`, `provisioning` | Recorder document construction still owns an HA contract, even without framework imports. |
| `test_environment/oep_test_fixture/` | `unit-test-environment`, `ha-test-environment`, `provisioning` | Real simulator, Recorder/preferences and provisioning contracts remain. HA manifest changes retain full installation acceptance. |
| Other integration code | `unit-component`, `unit-ha-contract`, `ha-contracts`, `ha-connection`, `ha-controls`, `ha-history`, `ha-thermal` | HA framework behavior retains genuine framework coverage. Installation manifests, package initialization and release identity stay full. The fixed HA `config_flow.py` contract selects connection, provisioning and Supervisor acceptance. |
| CI implementation, release tooling, undeclared app-root ownership and dependencies | Full | These owners still affect the system or execution policy broadly. |

The directories above are below `src/open_energy_planner/` unless a different
root is shown. `config_flow.py`, HA manifests and the public module entry point
are framework contracts, not arbitrary filename exceptions. Pure calculations
and persistence use directory ownership instead of enumerated product files.

### Focused native test ownership

Tests below each reviewed `tests/app/<area>/` inherit a `unit-app-<area>` group.
The declared areas are appliance, contract, demand, ev, forecast, history,
installation, notifications, planning, price, runtime, savings, solar, solver_regression and thermal.
Shared test helpers add every importing consumer from both immutable trees.
An undeclared new test domain fails closed to full until its ownership is reviewed.
The existing `unit-app` group keeps collecting every app test for legacy bases and
shared production calculations. When parent and child groups are both required,
the parent executes once and the summary marks the child as included.
Likewise, `ha-solver` is the existing remote solver group inside `ha-controls`.
Selecting the complete controls group includes the solver group without duplicate runs.
Independently selected children require their own successful execution reports.
When a selected parent includes a child, completion requires the selected parent
report instead. Missing or unsuccessful required reports fail the completion gate.

### Application composition and root ownership

`app.py` retains authentication, shared mutation ordering, lifecycle composition
and the aggregate snapshot. Contract, demand and price HTTP handlers belong to
`contracts/api/`, `demand/api.py` and `prices/api.py`. Setup editing belongs to
`installation/api/`, actual HA discovery to `home_assistant/setup_catalogue.py`,
and the public setup session to `frontend/session.py`.
Contract wire/history/basis helpers live in `contracts/`, historical interval
models in `history/`, location validation in `installation/`, timing arithmetic
in `core/`, price-provider composition in `prices/bootstrap.py`, and observation ingestion in `home_assistant/`.
The backend binding file writer is in `storage/adapters/lifecycle/`: changing
its atomic retained-startup behavior requires provisioning and Supervisor coverage.
No archived document, route or database schema changes with these moves.
Appliance implementation ownership includes `appliances/cycles/`,
`appliances/ev/`, `appliances/thermal/`, individual `appliances/forecasting.py`
and shared `appliances/sources.py`. These inherit
the existing appliance-domain route. A whole-PR move retains both old and new
paths. Changes to shared app composition retain native app and peer contracts, browser, four-backend, provisioning, connection and Supervisor checks. They no longer add unrelated Recorder, thermal and control groups by location alone. Test areas
remain `tests/app/appliance`, `tests/app/ev`, `tests/app/thermal` and
`tests/app/demand`, together with their existing backend and HA contracts.
The retained root modules compose the whole planner or its installation boundary:
`app.py`, `planning.py`, `live_inputs.py`, `discovery.py`, `pairing.py` and `ingress.py`.
Existing `app.py`, `planning.py`, `live_inputs.py` and `discovery.py` have reviewed root ownership. App composition retains app and peer unit checks, browser, four-backend contracts, early HA contracts, connection, provisioning and actual Supervisor acceptance. The planning coordinator retains shared plan construction/publication, peer, device and archive contracts without independently selecting installation or simulator lifecycle. Live input coordination retains app and peer checks plus early HA and control contracts. Discovery retains native thermal API, peer and component checks, real connection and Supervisor acceptance. New or removed flat modules still require full validation. Central composition alone does not add query performance. Release/build identity, the public
entry point and shared planning policy also stay conservative. Repository-root
build and tool configuration retain the platform-required names. Moving them solely
to obtain a cheaper selection would hide their actual consumers.

The calculation selector and transitive import guard share `CALCULATION_DIRECTORIES`,
derived from the reviewed domain set. New calculation files cannot introduce
storage, transport, process or filesystem dependencies without failing the guard.
The native solver selector and import guard also share `NATIVE_SOLVER_DIRECTORIES`.
Solver adapters may use HiGHS and import only `os.environ` for child environment settings, but
must not acquire transitive database, app-side HA transport or frontend dependencies.
A future helper introducing one of these dependencies fails CI before the narrower
solver selection can be trusted.
This static check cannot prove dynamic dispatch, external protocols or semantic
coupling. Recorder documents, HA measurement/device identity bindings, installation contracts and solver handoff therefore
retain explicit acceptance rules. Ordinary domain APIs remain conservative until
an independent contract boundary is justified. Merely relocating files is not evidence
that their expensive tests are unnecessary.

Changes to `Dockerfile.native-test` select every declared native unit group,
plus the four-backend contracts and Core provisioning that share that image.
The `unit-` environment convention includes future native groups automatically.
Browser, real integration HA and actual Supervisor use separate images and are
not added by this image change alone. The native image supplies the app source path to direct helper commands and child
processes. The app smoke scenarios execute before unit groups when `unit-app` or any
`unit-app-<area>` group is selected. Unknown image/build ownership retains full validation.

### Native domains and real HA boundaries

Native domain changes retain the complete app behavior suite, app-side peer
contracts and browser behavior. They do not automatically select every real HA
framework group. `DOMAIN_HA_SUITES` records the real execution owners. Appliance
changes retain controls, thermal and history. EV changes retain controls. Thermal
changes retain thermal acceptance. Installation changes retain early contracts,
controls, thermal and history. Optimization changes retain controls. Forecast-quality identity and evidence binding changes retain early contracts and Recorder history. Changes to
app-side HA adapters and the custom component retain their established broader
contracts. Shared composition, direct protocol changes and lifecycle do not
inherit a cheap native-only route.

The architecture guard rejects direct and transitive dependencies from ordinary
domains to app composition, live input coordination, Home Assistant itself or
the custom component. This does not prove arbitrary runtime payload semantics.
Review must inspect contract changes and preserve their real consumer tests.
Mixed PRs union all required groups. A native domain edit cannot suppress an
explicit Recorder or device-control change elsewhere in the same PR.

Forecast freshness belongs to `installation/forecast_policy.py`. Both installation
and standalone solar admission use that unchanged policy. Its native-only route
is protected by a dependency guard and existing expiry, recovery and publication
regressions. Planning horizons and resolution remain in the shared root policy.
Names captured at publication belong to the existing HA naming projection.
Measurement explanations belong to the observation notification owner. The price
API registers its existing peer input endpoint alongside its other price routes.
Authentication, locking, retained bytes and public route names remain unchanged.

Root extensions require a PR explanation of central ownership, why a domain
module cannot own the change and the resulting test impact. Architecture and
selection regressions block known dependency violations and accidental broadening.
Semantic responsibility remains a review requirement rather than a line-count
limit or a generic automatic dependency engine.

### Test groups and environments

| Suite | Collected directory | Environment |
| --- | --- | --- |
| `unit-app` | `tests/app/` | Native app behavior, calculations and SQLite-backed app scenarios |
| `unit-app-<area>` | `tests/app/<area>/` from the declared area set | One native app domain without collecting unrelated domains |
| `unit-ha-contract` | `tests/ha_contract/` | App-side HA message contracts without starting HA |
| `unit-component` | `tests/custom_component/unit/` | Framework-independent integration models, measurements and schedule |
| `unit-frontend` | `tests/frontend/unit/` | Native public-routing, security, packaging and asset contracts |
| `unit-supervisor` | `tests/supervisor/unit/` | Supervisor tooling, privileged startup and mixed bootstrap unit boundaries |
| `unit-provisioning` | `tests/provisioning/unit/` | Ordinary example definitions, identities and acceptance assertions |
| `unit-test-environment` | `tests/test_environment/unit/` | Offline fixture sources and readiness configuration |
| `unit-release` | `tests/release/` | Matched packaging and deployment identity |
| `browser` | `tests/frontend/browser/` | Real desktop/mobile Playwright behavior |
| `databases` | `tests/database/contracts/` and `tests/database/migrations/` | Compatibility parent. Real SQLite, PostgreSQL, MariaDB and MySQL. |
| `database-contracts` | `tests/database/contracts/` | Ordinary correctness on the same required real four-backend matrix. |
| `database-migrations` | `tests/database/migrations/` | Transfer and recovery on every original supported backend route. |
| `performance-queries` | `tests/database/performance/` | Twelve retained-history volume variants on the same four real backends. Selected for SQL/storage, schema, backend and owned performance-harness changes. |
| `ha-contracts` | `tests/custom_component/integration/contracts/` | Early real catalogue, handoff and command invalidation contracts |
| `ha-connection` | `tests/custom_component/integration/connection/` | Real integration lifecycle, authentication and reconnect |
| `ha-controls` | `tests/custom_component/integration/controls/` | Real entity/control/automation behavior and serialized remote-solver cases |
| `ha-solver` | `tests/custom_component/integration/controls/remote/` | Real remote native-solver plan, rejection and pause/resume handoff |
| `ha-history` | `tests/custom_component/integration/history/` | Genuine Recorder/history and native units |
| `ha-thermal` | `tests/custom_component/integration/thermal/` | Real HA thermal integration |
| `ha-test-environment` | `tests/test_environment/integration/` | Genuine simulator, thermal fixtures and balance/preference retention |
| `provisioning` | `script/home_assistant/provision_smoke.py` | Core first, repeated, interrupted and rejected provisioning |
| `supervisor` | `script/supervisor/check_supervisor.py` | Disposable real Supervisor cold/warm installation, ingress, outer restart, update and retained data |

A test-only change selects its own group and any importing scenario groups.
New tests inherit collection from directories without filename or test-name
allowlists. Unknown new test areas fail the declaration guard. Test grouping
follows responsibility and environment rather than mirroring every source file.
Consumer and fixture ownership follow both default pytest module patterns,
`test_*.py` and `*_test.py`. Changing pytest collection configuration requires
reviewing the selector's corresponding collection contract.

### Temporary Recorder fixture scale

Recorder-enabled `running_chain` scenarios keep every generated hourly row and
the latest two complete days of five-minute fixture data, aligned to the current
UTC hour. An entirely older dataset retains its final fine point so native
sum-origin alignment and hourly import still occur. Inputs are not mutated.
The canonical Recorder importer and its validation are unchanged. Only the
temporary simulator-generator bindings are replaced and restored by the test
helper. `compact_history=False` explicitly exercises the original full imports.
The two-day bound is a functional-test optimization, not a model of Home
Assistant retention. Hourly statistics coexist with recent fine statistics.

This policy does not change persistent developer-lab history or CI selection.
The disposable real Supervisor retains the full 32 to 35-day scenario, learned
models and lifecycle checks. Its retention snapshot also compares an original
hour and twelve older five-minute points through the native Recorder API after
warm provisioning, restart and update. The fine sample is on day 28 of the
32-day seed, outside the compact window and within HA's normal fine retention.
The full seed is deliberately synthetic bulk and lifecycle evidence. It does
not represent the normal fine-history retention period or prove automatic
purging at a configured retention boundary.
Changes to this boundary require native full/compact Recorder regression and
real Supervisor acceptance. Do not shorten the default simulator to optimize CI.

### Strict test-only candidates and main merges

The existing immutable-base selector additionally marks a candidate `test_only`
only when every changed path is an owned Python file under `tests/` and the
selection is focused. This is computed from the complete base-to-merge diff,
including additions, deletions and both sides of moves. A label cannot grant it.
Every changed helper needs statically known scenario consumers in the original or proposed
tree. One ordinary test cannot conceal a second orphan helper. Imports, package
initializers and conftest consumers retain all required groups from both trees.
Unknown dynamic imports can add conservative suite coverage, but cannot serve
as the static ownership proof that admits a helper to a focused route.
Direct and qualified `import_module` and `__import__` calls retain their users,
including imported aliases. Nonliteral or relative targets and built-in import
calls with extra arguments retain conservative coverage instead of inferring a
static target from `fromlist`, `level` or other call context.
Import functions passed as values or stored in aliases retain conservative
coverage. Reused alias names preserve every import binding, including bindings
in separate scopes, so a built-in import cannot be mistaken for a simpler call.
Assignments or import aliases binding `pytest_plugins` require full validation
until their plugin ownership is explicitly supported. None are currently used.
Both literal and unresolved plugin declarations are rejected rather than assuming
ordinary imports cover pytest's separate loading mechanism. Wildcard imports
also retain full validation because their exported bindings are not classified.
Static import analysis is conservative coverage evidence, not a proof that a
changed expectation is correct. Latest-head review remains mandatory.

| Complete change | Candidate and main policy |
| --- | --- |
| Owned Python test cases only | Execute the existing selected groups. An identical verified merge may reuse their original report evidence. |
| Python test fixtures or helpers with known consumers | Execute the owning groups and all importing scenario groups. The same exact-tree main reuse is eligible. |
| Database or real HA test cases | Retain the selected real four-backend or HA groups. Test-only never replaces them with unit tests. |
| Test cases plus documentation, policy, runtime code, dependencies, workflows or packaging | Ordinary whole-change selection. No test-only exception on main. |
| Non-Python fixtures, unknown areas, orphan helpers, special Git modes or unreadable impact evidence | No test-only exception. Preserve the normal conservative selection and full fallback. |
| Manual or release event | Full validation and the normal release artifact rules. |

Strict test-only candidates still run the selected suite-completion gate. They
publish `Test-only CI source tree: <tree SHA> (<profile>)` only after successful
suite completion and a clean checkout check. They preserve the original
`test-reports-amd64` artifact, including selection, completion and executed-case
evidence. They do not create an installation bundle or deployment build identity.
The app and integration packaging owners exclude `tests/`. Their definitions
cannot change in a test-only diff.

On main, the existing reuse gate verifies the signed two-parent merge, unique
same-repository merged PR, exact head/base and newest successful candidate attempt.
It runs the first parent's complete `script` policy snapshot under isolated
Python with site initialization disabled against the complete actual merged diff. That policy must independently confirm the exact
focused profile recorded in the candidate's ordered suite, completion and tree
steps. The original reports must be unexpired and belong to the same head and
successful attempt. A missing older policy capability, mismatched tree/profile,
later failed or pending attempt, unavailable API or missing report retains full
main CI. Successful reuse repeats quick checks but does not rerun the candidate's
groups or authorize an installation update.

### Support ownership and import evidence

| Helper owner | Responsibility |
| --- | --- |
| `tests/database/support/` | Backend configuration, independent retained datasets and persisted-state assertions |
| `tests/app/forecast/support/` | Installation basis and running forecast HTTP fixtures |
| `tests/app/installation/support/` | Independent physical installation problems |
| `tests/app/ev/support/` | EV identities, catalogue settings and observation documents without backend configuration |
| `tests/ha_contract/support/` | Catalogue metadata, command wire documents and local pairing listeners |
| `tests/custom_component/support/` | Real HA/app lifecycle, Recorder and peer gateways |
| `tests/test_environment/support/` | Offline price, weather and temperature inputs |
| `tests/supervisor/support/` | Mixed bootstrap/sidebar API doubles and invocation harness |
| `tests/frontend/browser/` | Browser actions, viewport assertions and persisted browser setup |

The former `tests/support/` directory has no domain-neutral helpers left.
A helper's owner follows its contents. Multiple consumers do not turn database,
forecast or installation setup into generic shared infrastructure. The EV fixtures
were extracted from a backend test module so real HA controls no longer import
backend parametrization just to obtain device identities.

For every changed Python test module, including helpers inside scenario files,
the trusted selector reads Python blobs from both immutable Git trees without
importing, executing, checking out or extracting candidate code. Git archive
attributes cannot hide files. Static absolute, package-relative and transitive
imports add every consuming scenario area. Package initialization also contributes
impact. Ancestor conftest dependencies apply to collection in that subtree, not
to an ordinary helper imported outside it. A newly added consumer inherits these
rules automatically. Removed consumers remain visible through the old tree.

Known literal dynamic imports are resolved. Unknown dynamic import users count
as potential consumers. Unreadable evidence and a changed helper with no known
scenario consumer require full coverage. Static checks also reject direct and
transitive Supervisor dependencies in ordinary domains and transport, database
or lifecycle dependencies in calculation owners. Every direct ORM import must
belong to an area selecting the real four-backend suite.

This is static import evidence, not a complete dynamic call graph. File loading,
external commands, HTTP/WebSocket payloads, entity semantics, Recorder units and
timestamps, authentication, installation ordering and retained state remain
explicit reviewed contracts. Unknown behavior stays conservative.

### Functional database migration selection

Read-only query owners and their transitive helpers may import only SQLAlchemy's
`select` constructor. Whole-module, dialect, ORM and DDL imports are rejected.
Execution must directly consume an expression built by that imported constructor.
Raw driver access, borrowed statement parameters and saved execute aliases are
rejected, including inside transitive helpers.

`database-contracts` collects ordinary correctness under `tests/database/contracts/`.
`database-migrations` collects cross-backend transfer and verification under
`tests/database/migrations/`, including settlement transfer cases. Ordinary
restart, active-archive damage and local backup restoration remain correctness
contracts. Mixed settlement scenarios retain independent small correctness tests
as well as their original transfer assertions. The shared matrix retains SQLite,
PostgreSQL, MariaDB and MySQL and every original ordered transfer route.

`databases` remains a compatibility parent that executes both functional
directories and owns the original combined `databases.xml`. All legacy profiles,
including `full`, retain that parent and their complete original functional
meaning. A focused child owns its own JUnit report and exact-tree completion
entry. Both children use the existing disposable real-backend services,
sequentially on one database worker. They never write concurrently to shared
backend tables. Collection and execution use the same directory arguments.

Storage writers, schema, migration and shared database fixtures retain the
compatibility parent. Native domain wire and persisted model owners also retain
it. Query-only ownership exists under storage `queries/` directories within SQL
adapters. The existing latest-decision read moved to
`prices/storage/adapters/queries/selection.py`. It accepts an existing connection
and tables and does not create engines, initialize schemas, serialize writes or
change transactions. Changes there select ordinary four-backend correctness and
the independent query-performance policy, without every transfer.

The read-only guard rejects SQL mutation, schema operations, raw execution and
transitive dependencies on storage writers, runtime or framework adapters. New
queries must follow the same boundary. An uncertain or mixed writer/query change
retains migration coverage. The guard is static evidence, not a complete proof
of arbitrary dynamic SQL or serialized payload semantics. Review must assess
archive compatibility and add relevant transfer contracts.

Shared history inputs live in `tests/app/history/support/bonus.py`, retail
settings in `tests/app/contract/support/settings.py` and physical savings inputs
in `tests/app/savings/support/scenarios.py`. A change to a scenario no longer
changes importing migration or volume scenarios merely because those inputs
previously lived in the same file. Changes to the actual support inputs
still retain all transitive consumers. Scope labels remain informational.

### Execution, completion and reuse proof

Packaged collection fails early before services or long tests start. Execution
runs cheap policy/unit groups first, then selected browser and early HA contracts.
Real database/HA groups follow. Actual Supervisor acceptance runs last after fast
services have been cleaned up. Every group has its own JUnit report. Empty
selection, collection failure, cancellation or process failure cannot count as a
completed suite.

Full and legacy HA runs execute early contracts once, then exclude their directory
from the remaining two-worker HA run. When the four-backend suite runs separately,
native execution excludes database contracts instead of repeating them on SQLite.
A legacy native-only selection still includes its original SQLite coverage.

The controller records successful suite commands and validates the exact selected
set before writing `results/ci/suite-completion.json`. That record binds the profile,
source tree and completed groups. Selected pytest reports are cleared before execution.
Fresh JUnit evidence must contain executed cases without failures, errors or skips.
An empty HA worker is permitted only when its peer executes the group. The report
records actual executed counts and verifies them again before packaging.
Missing or extra suites, stale trees and malformed reports fail. Only a full
candidate can publish the full-tree marker required by full-candidate main reuse. Focused proof
cannot substitute for a full candidate. It can authorize exact focused-product
main reuse only through independent first-parent selection and both original
artifact kinds. Strict test-only proof has its own
profile-bound tree marker and report-only reuse rule above, including an unexpired
`test-reports-amd64` artifact. Full-candidate reuse requires the newest successful,
complete attempt, ordered full-suite/completion/tree steps and its original
unexpired matched installation bundle. A newer queued, running, failed or cancelled candidate
blocks reuse of an older success.

Actions shows selected and omitted groups with complete-PR path and consumer
reasons. Groups executing inside legacy parents are labeled as included, not
separately executed. Planned `ci-scope: *` labels are advisory. They never replace those logs.
`results/ci/selection.json` retains the trusted selection and its raw reasons.
`results/ci/phase-times.json` records actual phase starts, durations and outcomes,
including overlapping work and the final Supervisor phase. Preflight has a separate
`phase-times-preflight.json` record. Hosted image-build timing remains in workflow
step metadata. These records are uploaded with
completion and JUnit evidence.

### How an omission is substantiated

An exclusion requires an explicit contract boundary, an enforced dependency guard,
regressions covering new files/domains/moves/consumers, actual completion evidence
and a full comparison candidate. A successful full run on this CI-infrastructure
PR checks every omitted group too. A focused comparison on the same tree and
runtime can then show which phases disappeared without pretending that selection
reasons alone establish correctness.

Historical PR replay is a counterfactual eligibility analysis. Use complete
original base/merge ranges. Normalize mechanical moves only, retain mixed-file
and unknown fallbacks and report excluded groups. Do not infer saved minutes from
parallel phase durations or compare local and hosted wall times as equivalent.
Time claims need comparable complete candidates with recorded runtime, resources,
cache state, source tree and actual critical-path timing. Keep measurements and
historical replay results in PR evidence rather than a permanent timing promise.

### HA process isolation

The HA test container runs two pytest processes within its existing two-CPU and
2 GiB limit. Every collected case belongs to exactly one process. Recorder-heavy
cases start first and alternate between workers. Each process owns its temporary
HA directories, Recorder databases, event loops and local app ports. Tests named
`test_remote_*` stay together in worker zero because they mutate the shared
external solver app. New tests using that app must retain this naming contract.
Both processes must finish and either failure fails the HA gate. Their independent
`ha-0.xml` and `ha-1.xml` reports are uploaded with the remaining JUnit reports.
The native Energy reload test creates a fresh HA and Recorder store for each of
the flat and nested power configurations. Initial metadata, unit and history
checks run once per configuration. The hourly readback requires at least 720 rows
per returned source within its 31-day query window. Both missing and
restored-unavailable fixture sensors are then tested, using fresh counter/event
baselines before each real reload. Each condition checks retained hourly history,
numeric counter progress and Energy preferences. Each terminal reload drains
Recorder and compares its hourly history with the original readback. External-unit
rejection and fixture store-marker removal run once per power configuration after
both sensor conditions. This removes two repeated initial imports while keeping
the independent from-empty household and schema-repair scenarios.
No Recorder implementation or database write API is patched. Database contracts
may still run concurrently in their separate service.

Keep timing comparisons in CI artifacts or PR evidence. Measure representative current runner builds and suite scheduling before estimating full-job savings.

### Local commands and candidate evidence

`python script/ci/ci.py` runs full validation by default. To run selected suites locally,
use a canonical selection such as `--profile selected:ci,supervisor,unit-supervisor`
or `--documentation-only`. Legacy profile names remain supported with their original coverage. A local profile flag does not establish PR eligibility.
Actions obtains eligibility from the base policy and records the selected route.

The quality job builds only the images needed by its selected suites. Non-document
candidates produce the matched installation bundle after selected tests pass,
except strict test-only candidates, which publish reports without an installation bundle.
Only an actual full-suite success can publish the `Full CI source tree` marker.
A focused candidate cannot masquerade as complete Supervisor/database validation.

### Query performance ownership

`performance-queries` is a separate four-backend suite under
`tests/database/performance/`. It preserves the original eight five-year price
variants and four three-year settlement variants, including their independent
numerical assertions, query-plan checks and existing coarse timeout guard.
The focused functional `database-contracts` suite collects only `tests/database/contracts/`. The legacy `databases` parent also includes `tests/database/migrations/`.
Small price cases separately verify independent latest buckets, exclusion of old
errors, MAEs and reopened evidence. A sparse three-year settlement case verifies
year boundaries, aggregation, 96-detail truncation and daily/monthly chart keys
using 99 quarters. Index repair, rollback, migrations, persistence, DST and input
validation remain functional contracts. A slow test is not automatically a
performance test. The ordinary native runner excludes the volume directory.

The immutable base selects performance checks for storage and SQL owners,
including nested domain storage, schema/lifecycle code and moves out of those
areas. Changes anywhere in the shared `tests/database/support/` directory or the
database conftest retain both database groups, without a fixture filename list.
Changes to performance cases, the native image, `requirements.lock`, database
dependencies and the shared database environment retain the group. CI controllers,
workflows and selection metadata alone do not add query measurements. Shared
execution and unknown CI owners still retain their full functional coverage.
Both sides of a move are classified, so a simultaneous query or fixture change
cannot lose its volume checks because the PR also changes CI.
Shared Python fixture consumers are retained from both immutable revisions.
An architectural regression rejects ORM imports outside SQL adapters or an
adapter whose policy omits either functional database or performance coverage.
New queries need volume assertions for their own access pattern. Running the
existing price and settlement cases alone is not proof for an unrelated query.

An update confined to the one SHA-pinned `actions/setup-python` reference in
`checks.yaml` selects CI regressions. The selector compares immutable Git blobs
and requires every other workflow byte to remain identical. It does not read the
working copy or trust a PR label. Changing the Python version, runner, permissions,
commands or another action, adding or deleting the workflow, an ambiguous setup
reference or unavailable comparison evidence retains the normal full route.
The reference must belong to a block-style workflow step. Matching text inside
scripts or other scalar values never qualifies. Ambiguous YAML syntax before the
reference conservatively retains full validation.
Mixed changes retain the union of every affected owner's required suites.

Central Python composition changes keep the original `full` matrix without
inferring query impact from that breadth. SQLAlchemy ownership remains enforced
by the architectural regression above. Storage/query owners, shared database
support add performance separately. Unknown impact retains `full` without it. A main push retains its
full functional matrix, while its complete immutable before/after diff decides
the performance addition. Manual/release events and unavailable policy evidence
retain `full` without an automatic performance addition. There is no scheduled or nightly workflow.

The advisory label `ci-scope: performance-queries` appears alongside the ordinary
matrix/group labels only when the addition is selected. It uses magenta `d500f9`,
distinct from ordinary blue `1d76db`. Label publication repairs an existing
performance label's color and removes the PR label when no longer selected.
Labels do not authorize or select execution. The trusted base policy does.

A local targeted invocation is:

```sh
python script/ci/ci.py --profile selected:ci,performance-queries
```

Both database suites use the same disposable four-backend services and run
sequentially within their database worker. That worker can run in parallel with
HA. Performance-only selection does not start unrelated HA, browser or Supervisor
services. The performance group owns `results/ci/performance-queries.xml` and
must complete successfully before its tree-bound completion report is published.
Collection regression checks retain all twelve original volume variants and prove
that index repairs and the new small functional counterparts stay in the
functional group. Missing, empty, failed or skipped selected execution cannot
publish successful completion. Performance completion requires at least twelve
executed cases, including when verifying a previously recorded completion report.
Timing reports are observations. No new
hardware-dependent millisecond threshold is introduced by this split.

A complete full candidate publishes `Full CI source tree: <tree> (<selection>)`
only after successful suite completion, bundle creation and clean-tree
verification. Selection is `full` or `full+performance-queries`. Main reuse
preserves signature, head/base, latest attempt, ordered step, original bundle
and exact tested-tree checks. It independently executes the first parent's
performance policy on the actual merged diff. Explicitly required performance
cannot reuse a new functional-only `full` marker. An unavailable performance
policy follows the original `full` fallback without automatically adding volume tests. Legacy unsuffixed full markers
retain their original all-tests meaning. Focused test-only report reuse keeps
its existing independent eligibility and exact-profile evidence.

### Retained history test inputs

`tests/database/support/history_datasets.py` generates five-year price inputs and three-year
settlement inputs independently of application models, schema and serializers.
The price fixture retains 262944 forecast pairs, 43824 hourly actuals and 5475
decision rows per area. Each settlement fixture retains 105216 quarters in each
of its two tables. Inputs are streamed in batches of 1000 rows into each test's
fresh database. SQLite, PostgreSQL, MariaDB and MySQL retain their real insertion,
index maintenance and query paths. Independent numerical assertions, installation,
ingestion and migration tests remain unchanged.

Settlement generation reuses a standard JSON encoder and immutable interval and
row fields. It retains finite JSON validation and the original payload bytes.
There is no persistent raw-data cache or database snapshot to refresh or invalidate.
Selected performance runs still use newly generated inputs and fresh stores.

Record benchmark conditions, native query plans and separate generation/insertion/query timings in CI artifacts. Local fixture timings do not establish full-CI savings. The workflow generates fresh stores and does not restore physical database snapshots.

## Merge and release evidence

Require an actual successful `Quality and integration (AMD64)` result for the
latest requested PR candidate, its base-selected route and intended base,
together with latest-head review and verified commits. A missing, skipped, pending
or cancelled result does not satisfy the merge policy. A subsequent source change requires renewed review and
another candidate run. Test failures may require another correction cycle.

## Reuse an identical tested merge on main

Main always runs quick checks. An ordinary two-parent merge may reuse its
successful full PR candidate instead of building and testing the same files
again. Admission verifies the main merge signature through GitHub, its unique
merged PR association, both parents and the current-attempt Checks run from this
repository. The full quality job and actual full-suite step must have succeeded.
Selection includes unsuccessful and pending runs. A newer failure cannot hide
behind an older successful candidate. The newest run is checked again after
reading the job and artifact evidence.

The quality job captures its checked-out Git tree before testing. After the
matched installation bundle is uploaded, it verifies the checkout still matches
the workflow commit and has no tracked differences. Its successful
`Full CI source tree: <tree SHA>` step records the exact tested tree in GitHub
Actions job metadata. Main requires exactly that tree, not merely a green status
on a PR head. A merge of two independently tested PRs runs full CI whenever the
combined tree differs from the candidate. Incomplete or unavailable metadata,
old/skipped/failed attempts, missing tree proof and expired or missing bundles
all select the full pipeline. Older candidates without the new proof cannot qualify.

The gate records the original successful run, artifact and build in the Actions
summary. Reuse creates no new installation bundle or build identity. Download
the original candidate artifact through that run and verify its manifest and
checksums as usual. Retention stays 14 days. The bundle must have been created
during the successful attempt and must still be unexpired. No source overlays,
restamping or copied replacement artifacts are allowed.

Focused main reuse requires the complete coverage independently selected by
the first parent's immutable policy for the actual merged diff. A focused
product candidate can reuse exact selected coverage through the separate
`Selected CI source tree: <tree> (<profile>)` marker. This never qualifies as
a full-candidate marker. The gate independently computes
`candidate_reuse_profile` from the first parent, requires the exact same
canonical profile and the ordered successful selected-suite/completion/tree
steps, and validates both original installation bundle and report artifacts.
GitHub also returns skipped conditional tree-marker steps. Those steps are
not proof and do not make a single successful marker ambiguous. Only completed
successful tree markers participate in proof selection. Multiple successful
markers remain ambiguous and fail closed.
Legacy unsuffixed full-tree markers retain their original full-candidate proof
contract without invoking focused first-parent selection.
An older policy without that function cannot authorize focused product reuse.
Strict test-only merges retain their separate report-only proof. Direct main
pushes, force pushes, differing trees, widened required profiles and missing
proof run the full pipeline. Manual workflows and published releases
always retain full checks. The quick stage still fails before expensive image
builds. Main reuse never replaces the required pre-merge candidate result or manual review of findings. Release
publication remains unchanged. This check verifies an already merged result.
It does not add server-enforced merge protection or authorize deploying an
untested combination before its full run succeeds.

The repository's private-plan merge gates are owner-reviewed policy, not paid
server-enforced branch protection. This change adds no paid upgrade and does not
claim to defend against a malicious same-repository writer who can edit workflows.

Workflow changes activate for other PRs as they incorporate the updated base.
Validate the initial workflow PR itself with affected local checks, GitHub Quick
checks, latest-head review and one candidate containing every required suite
before merging. Trusted base changes take effect only after merge. Do not repeat
the full candidate locally or require hosted execution when the personal route
has the same exact-source and artifact evidence.
