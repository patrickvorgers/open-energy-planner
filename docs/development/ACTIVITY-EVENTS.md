# Activity and module events

For everyday operation, see [Read background activities](../users/activity.md).
This reference defines producer ownership, outcomes, durability and subscriptions.

## Outcomes and availability

| Outcome | Meaning |
| --- | --- |
| Running | An actual execution began without a definitive outcome yet. |
| Completed | The producer accepted a usable result. |
| Failed | Attempted work did not produce an accepted usable result. |
| Interrupted | OEP stopped or restarted without recording a definitive result. This does not prove a content error. |
| Cancelled | Work was deliberately stopped. |
| Skipped | Work was deliberately not applied, for example after inputs changed. |
| Recovered | First usable completion after a failure for the same producer and type. |

Each actual attempt has one execution ID. Intermediate changes reuse that ID.
Repeated attempts remain separate records. Empty weather and rejected prices
do not establish recovery. Already processed appliance-history uploads are
Skipped, not recovery. Model comparisons awaiting evidence are Skipped with
Unknown availability, not Failed. When the evidence-wait deadline expires and
the window is durably abandoned, it is Failed with Degraded availability.
Partial savings correction batches and expected settlement evidence waits are
also Skipped with Unknown availability. Earlier successful financial results
remain visible. Invalid evidence and missing original plans remain failures.
A comparison becomes Completed only after its capture or evaluation is committed.
Price details name the actual adapter, market and usable coverage. Original
source expiry remains authoritative. Failure details explain whether retained input or a baseline remains usable and its impact.

Availability is separate. **Unknown** means the producer has not established its
current state. **Available**, **Degraded** and **Unavailable** are source-owned
statements using existing validity rules. Past success does not guarantee current
availability. Replacing a configured price service retracts its live state and
schedule. The collector never retries domain work.

## Business ownership

Activity failures and recovery do not automatically create notifications. Existing
module-owned notification readers retain cause keys, actions, recovery evidence
and stale-source behavior. While clients are subscribed, the event adapter reads
that projection every second and publishes changes. This performs no provider
fetch or business mutation. The UI validates notified changes through the
existing authenticated notification endpoint.

| Producer | Actual executions | Known next work |
| --- | --- | --- |
| Planning | Calculation and publication disposition, including ad-hoc work | No predicted ad-hoc work |
| Prices | Adapter requests and acceptance | Existing publication, correction, refresh and retry clocks |
| Household forecast | Captured forecast/training job | Next quarter or earlier retry |
| Solar forecast | Captured forecast/calibration job | Next quarter or earlier retry when usable inputs are established |
| Temperature and solar weather | Provider reads | Hourly refresh or 30-minute retry |
| Appliance learning | History batches and measured-cycle learning operations | Event-driven |
| Model comparison | Actual numerical workers | Event-driven |
| Forecast quality | Daily baseline archive writes | Next installation-local midnight |
| Savings | Measured settlement and contract-correction calculations | Evidence-driven |

Idle polls, cache reads and page refreshes are not activities. Database maintenance
keeps its separate journal and independently reachable recovery screen while the
normal app is stopped. Activity does not replace that recovery mechanism.

## Snapshot, replay and restart

### Optional execution context

`Events.start`, `attempt`, `thread_attempt` and `scheduled` accept an optional
`context` dictionary. Producers provide safe human names through `label` and,
when meaningful, the actual work horizon through paired ISO `start` and `end`
strings. Both timestamps must have an explicit offset and end must follow start.
Archived valuations also capture `plan_generated_at` and `plan_strategy` so the
browser uses the same creation-time and strategy caption as the Planning selector.
The creation timestamp must be aware. Only `minimize cost` and `minimize consumption`
are admitted strategies, and only with a valid creation timestamp. Unknown strategy
values are omitted. The original work horizon remains separate from plan creation.
Optional `plan_id` admits one to 128 ASCII letters, digits, underscores or hyphens.
It retains the actual immutable plan reference, never an arbitrary navigation URL.
Optional paired `waiting_start`/`waiting_end` identifies the interval where a
measured batch stopped. It uses the same aware, increasing UTC normalization as
the overall batch range. `finish` and `finish_thread` can enrich context at the
terminal boundary, preserving the captured horizon. Planning supplies its saved
plan reference only after successful publication. Financial evaluation supplies
the stopping interval after examining the captured measurements.
The collector normalizes valid ranges to UTC and limits input timestamps to 40
characters. It collapses label whitespace, replaces nonprinting characters and
limits the single-line label to 160 characters. Unknown keys are discarded.
Malformed labels and ranges are omitted independently, without failing domain
work. Context contains no raw provider payloads, exceptions, URLs, credentials or
precise coordinates; producers remain responsible for safe label content.

Context is captured as detached execution evidence, retained through progress,
terminal outcomes, replay, restart and database transfer. It changes no execution
identity, outcome, retry or recovery semantics. Existing records without context
remain unchanged. Storage uses the existing payload JSON, without extra columns.

| Producer | Context choice |
| --- | --- |
| Prices | Existing shared adapter display name from the owner. Unknown adapter identities do not become a title suffix. |
| Household forecast and temperature weather | Household consumption, without location or sensor identities. |
| Solar forecast | All available human source names in the captured configuration, otherwise Solar production. |
| Solar weather | Solar production. The weather request serves the whole installation. |
| Appliance learning | The selected appliance's admitted display name, otherwise Appliance. |
| Model comparison | Household consumption or the solar display name captured with the original plan, otherwise Solar production. |
| Planning | The actual captured problem horizon. No guessed upcoming ad-hoc horizon. |
| Forecast quality archive | The pending archive's captured start/end. Scheduled work uses its installation-local day, including DST. |
| Measured savings | The captured measurement batch range being investigated, including when settlement remains pending. |
| Archived plan valuation | The same creation-time and strategy caption as Planning, plus the original problem horizon. Missing creation evidence falls back to Archived plan. Malformed horizon evidence omits the range without preventing failure recording. |

The browser uses inert text and the shared minute-precision installation-timezone
formatter. Names stay on one line with visual truncation. Native disclosure and
the title attribute retain the full context. Generic executions without meaningful
subjects keep their original title. Context never derives from detail or exception
text. Per-plan financial failures stay separate, retain Failed and explain the
effect and next action without promising an automatic retry. See the user guide
for batch semantics and troubleshooting.

Read-only navigation is a frontend-owned mapping from known producer owners to
existing data screens. A saved plan reference opens that exact archive through
the existing authenticated comparison reader. Linked selections remain pinned
through background publication polls. A missing archive never falls back to
another plan. Navigation retains HA ingress and embedded-workspace prefixes.
Other links explicitly name the general destination and promise no unimplemented
source, appliance or date selection. Unknown owners have no inferred destination.
The collector discards unknown context keys, including URLs and provider payloads.

Measured evaluation returns its actual per-attempt processed/total counts and
stopping frame separately from its outcome. Fixed admitted causes own readable
explanations and recovery advice. Unknown exceptions receive bounded generic
guidance without their values. These additions change no interval commits,
publication pointers, financial calculations, retry clocks or outcome semantics.

`GET /v1/events` uses existing bearer or trusted-ingress authorization. Credentials
never enter URLs. Its first response contains an authoritative snapshot and a
process-epoch cursor. The snapshot retains all known future deadlines, so an
unchanged deadline can enter a consumer's six-hour window later without another
producer update. The Activity page applies that window on each render.
`?cursor=<cursor>` returns later changes or waits up to
25 seconds. The browser uses a 35-second timeout and reconnects after three
seconds. Temporary live and storage-availability warnings clear on recovery.
Lost-update warnings remain because missing evidence cannot be reconstructed.
A failed history read remains visible until that read succeeds. Transport
recovery does not retry business work. Unauthorized responses require
authentication again.

Events contain a process-local sequence, topic and detached data. The latest
512 changes are replayable. Snapshot/cursor capture and wakeup registration have
no suspension point, preventing a subscription gap. Foreign, invalid, future or
expired cursors return `reset: true` with a new snapshot. Consumers replace their
live state and reload history instead of combining different process epochs.
Other modules can use this same subscription for structured health changes.
They must not parse display strings or diagnostic logs.

Restart keeps retained history and marks persisted Running records Interrupted.
Failure episodes are reconstructed from terminal evidence. Producers republish
current health and schedules. They are not restored as allegedly current facts.
Shutdown wakes long polls, drains producers and then drains activity writes before
the shared database closes.

## Storage and limits

`activities/` owns an SQL history store on the app-owned active engine. The shared
schema and four-backend transfer include its table. Day paging uses UTC start
time and ID with installation-local day bounds, including 23-hour and 25-hour
days. `GET /v1/activities` accepts a `limit` from 1 to 100, defaulting to 100.
One lookahead row determines whether a next page exists, including exact page
boundaries. The browser shows 25 rows and retains cursor boundaries for Previous
and Next instead of accumulating history. New executions enter page one only.
Visible older executions still receive live outcomes. A late history response
cannot overwrite an outcome received while it was loading. Day or type changes
reset the page, while day changes preserve the type filter. Refresh opens page
one. Navigation stays disabled during reads, and a failed navigation retains the
current page. Start time determines retention. Original plans and financial
archives are unchanged.

Producers enqueue detached updates without waiting for SQL. One writer handles
up to 512 queued updates. Individual write failures do not terminate that writer
or stop planning. A failed write
or full queue increments a process-local lost-update count. Activity shows that
history may be incomplete, including after a new subscription. Normal shutdown
drains accepted updates. Forced termination and unavailable storage cannot
guarantee queued diagnostic evidence. Activity is not proof of physical execution.

Publish only safe, module-owned explanations. Never include credentials, raw
provider failures, connection strings or precise site coordinates. The public
activity APIs and UI are read-only and expose no event-write endpoint.
