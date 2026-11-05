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
Unknown availability, not Failed. A comparison becomes Completed only after
its capture or evaluation is committed. Price details name the actual adapter,
market and usable coverage. Original source expiry remains authoritative. Failure details
explain whether retained input or a baseline remains usable and its impact.

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

`GET /v1/events` uses existing bearer or trusted-ingress authorization. Credentials
never enter URLs. Its first response contains an authoritative snapshot and a
process-epoch cursor. `?cursor=<cursor>` returns later changes or waits up to
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
days. Start time determines retention. Original plans and financial archives are
unchanged.

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
