# Database migration from Data storage

## Implementation plan and acceptance conditions

1. Reuse the four-backend archive transfer, including exact verification of every
   registered OEP table. Add bounded row-count progress, not a second copy engine.
2. Keep a database-independent maintenance HTTP shell alive. A managed app runtime
   serves normal traffic on loopback. The shell closes admission and drains that
   runtime completely before copying. No database hot swapping or dual writes.
3. Add authenticated connection preflight and explicit target-overwrite consent.
   Never return passwords, connection URLs or driver exceptions to the browser.
4. Keep a private, atomically replaced migration journal outside both databases.
   It holds source/candidate connection settings, phase and recovery boundary.
   An interrupted, unreleased migration starts in maintenance mode, never on an
   empty target. Retrying rebuilds the destination only after renewed consent.
5. Verify schema, state, all copied rows, a rolled-back write and application
   construction before release. Persist release before any background startup.
   Restart the managed application automatically. The maintenance shell stays up.
6. Preserve the source. Before release, offer restore/retry. After release, refuse
   old-source restoration: new data may exist, so returning needs a new migration.
7. Extend the existing Courtyard Data storage screen with an explicit Change
   database flow, progress, reconnect handling and downloadable recovery steps.
8. Prove admission, drain ordering, restart boundaries, credential redaction,
   four-backend copy/progress and desktop/mobile behavior. Keep CI and deployment
   outcomes separate from local verification.

## Durable connection ownership

After a UI migration, `database-selection.json` is authoritative over the original
Supervisor `database_url` / `OEP_DATABASE_URL`. App updates retain this bootstrap
file. `archive-backend.json` still records the active archive identity. These files
and `database-migration.json` contain credentials, use private permissions and must
be protected with the data directory. No new application-data tables are required.

The journal is authoritative across an interrupted activation: released means the
target is selected even if connection-file replacement or runtime startup failed.
Unreleased means maintenance only. Never infer permission to revert from an HTTP
timeout or absence of new rows. A failed post-release startup remains maintenance
on the target. Retry startup, not the destructive copy.

## User experience and recovery

The screen answers: which database holds my OEP data, and can I safely move it?
Keep the Courtyard type, colors, icon family and left navigation. Connection data
is read-only until Change database. The flow is Destination, Check connection,
explicit overwrite consent, then Migrate database. Only actual copy/verification
counts produce percentages, with one decimal. Loading, failure and interrupted
states retain the recovery instructions. Closing a tab never cancels migration.

The open browser receives recovery instructions before starting. It distinguishes
loss of connection from migration failure. On reload, authenticated status comes
from the journal. A process/machine restart briefly interrupts HTTP. Restarting
only the managed runtime does not. No separate web-service dependency is added.

If the maintenance server itself cannot start, stop OEP and start its usual module
command with `--recover-storage` in addition to the normal mode/data arguments.
This restores the retained source only for an unreleased migration. Alternatively,
restore a backup of the complete bootstrap directory only if the migration was
not released. After release, use a bootstrap backup consistent with the released
target. Never restore pre-migration connection settings that reactivate the source
and hide newer target writes. If necessary, recover the target first and perform
a reverse migration. Do not delete a destination that may hold post-release data.
Do not publish journals or URLs in support logs.

For a Home Assistant app, enable optional `recover_storage` in the app's
configuration and start the app. This applies only to an unreleased migration.
After Data storage confirms the previous connection is working, disable the
option. A retained option is idempotent for that operation and never restores a
released source. Standalone operators may set `OEP_RECOVER_STORAGE=1` for the same
bounded rescue behavior. The explicit CLI flag refuses inapplicable recovery.

Authenticated confirmation identifies the backend, server and database. Usernames,
passwords, full URLs and TLS/query options never enter status or downloads.
Failure diagnostics retain stack locations without raw driver messages or locals.
The container healthcheck recognizes authenticated maintenance status as a deliberate
pause. It does not treat unavailable planning endpoints during migration as a crash.
Normal live startup still requires fresh Home Assistant evidence before publishing
commands. Historical plans remain unchanged.

## Scope

Only the OEP archive is migrated. Home Assistant Recorder, external provider data,
physical device control and other applications' tables are not migrated. The
destination database must already exist. TLS options use the installed SQLAlchemy
drivers' URL parameters. One OEP instance must own both archives during transfer.

## Maintenance API and admission

Every maintenance endpoint requires the existing installation bearer token or a
validated Supervisor ingress peer and prefix. Public HTML never grants access to
database details or operations. The private loopback app additionally requires a
process-private gateway proof. An arbitrary local connection cannot assert ingress
identity to that app.

| Endpoint | Request and effect |
| --- | --- |
| `GET /v1/storage/migration` | Return redacted phase, connection descriptions, release boundary, recovery availability and optional row progress. |
| `POST /v1/storage/migration/check` | Supply `url`, or explicit null for local SQLite. Return a private-operation `check_id`, destination description and whether OEP tables exist. |
| `POST /v1/storage/migration/start` | Supply that `check_id` and boolean `overwrite: true`. Persist intent, close normal admission and return 202. |
| `POST /v1/storage/migration/retry` | Before release, require renewed `overwrite: true` and rebuild the target. After release, retry selected-target startup without another copy. |
| `POST /v1/storage/migration/restore` | Restore and restart the retained source only for an unreleased failed/interrupted operation. |

Only one maintenance action can own the operation. Concurrent actions return 409.
Invalid settings or consent return 400. Unauthenticated requests return 401.
Corrupt private bootstrap settings block operations with 503 instead of selecting
an empty replacement. Application-owned errors never contain driver messages.

Connection preflight reuses the existing transient metadata probe to reject
aliases before target destruction. Its cleanup drains before request cancellation
can release the operation lock. A canceled browser editor independently invalidates
its response generation, so a delayed reply cannot restore old overwrite consent.

Normal request bodies and responses stream through the maintenance host. The
inner handler retains its original per-route request limit, including larger
history imports. Already-decoded request bodies do not retain gzip headers or an
obsolete content length. Ordinary authentication still belongs to the inner app.

## State and interruption contract

| State | Normal API admission | Recovery |
| --- | --- | --- |
| Idle, completed, restored | Open only after runtime startup succeeds. | A new transfer needs fresh preflight and consent. |
| Preparing, copying, verifying, checking | Closed. The UI, session metadata and authenticated maintenance status remain reachable. | Wait for the owned operation. There is no ambiguous mid-copy Cancel action. |
| Restarting | Closed until the selected runtime is reachable. | The release flag determines which archive may become authoritative. |
| Failed, interrupted before release | Closed. | Retry with renewed overwrite consent, or restore source. |
| Failed, interrupted after release | Closed. | Repair/retry the selected target. Returning to old history requires a reverse migration. |

The source copy is made only after app request handlers, planner and source
workers have drained. The private runtime disables aiohttp's shutdown deadline.
An accepted write must finish even if it takes longer than the default 60 seconds.
The Home Assistant app declares 300 seconds of Supervisor stop grace, the
platform's supported maximum. Active native work and separate HTTP shutdown
phases can exceed the platform's default ten seconds. This is available cleanup
time, not a delay on an otherwise completed stop or a change to the solver
budget. It does not guarantee that unbounded storage I/O finishes before a hard
stop. An interrupted transfer still follows the durable recovery contract below.
Copy/verification progress counts all registered rows and
is relative to the current phase, not a fictitious overall time estimate. Row
counts stay in memory. The phase and release boundary are durable. After a process
interruption the screen reports interrupted rather than inventing resumed counts.

Release intent is journaled before activation files or background startup. If
interrupted between those writes, explicit startup retry reconciles the selected
target from the released journal. Before that boundary, no normal request or
background worker may write to the candidate. Application construction and the
rolled-back probe may validate it without admitting normal operation.

An atomic bootstrap replacement can become visible before flushing its parent
directory fails. The controller retains that new journal in memory and closes
normal admission. It never rewrites a release boundary from stale state. Further
maintenance actions stay blocked until the operator repairs the data volume and
restarts OEP. The next startup reads the retained journal and offers recovery
according to its release flag. A pre-replacement intent write failure leaves the
prior runtime and journal unchanged. If failure reporting itself encounters a full
or unwritable volume, the listener retains an in-memory failure with repair steps
and the last persisted release flag. It does not terminate or open ordinary APIs.
Selection-file failures after release retain the
target journal and offer target-startup retry, never old-source restoration.

The normal UI restarts the managed app runtime, not the maintenance listener or
the container. A container or machine restart briefly interrupts HTTP and then
returns to journal-backed maintenance when an operation is incomplete. Browser
disconnection never proves that a server-side operation failed or was canceled.
