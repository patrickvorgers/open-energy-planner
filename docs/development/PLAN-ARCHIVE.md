# Durable plan archive

See the [database schema and data dictionary](DATABASE-SCHEMA.md) for every table,
column, enforced relationship, logical reference and index used by this archive.

## Implemented boundary

The app uses `SqlPlanRepository` over the active SQLAlchemy engine.
One transaction commits immutable plan content, its input references and the active
pointer. Publication succeeds only after commit. Reading history never activates
commands or recomputes an old optimization. The integration remains a thin API
consumer. It neither accesses this database nor imports SQLAlchemy.

The shared schema stores every durable OEP record in the one active database: plans,
shared inputs, contract/bonus rules, confirmed counters, installation and forecasting
configuration, normalized prices, forecasts, model coefficients and solar daily
weather/history blocks. Typed store classes retain their validation and immutable
version rules. The shared SQL document adapter owns persistence. Version plus active
pointer commits atomically, and in-memory configuration advances only after commit.
The [savings service](SAVINGS.md) supplies continuous reference/savings,
settlement ingestion and a responsive history visualization through the same engine.
Final production and backup/restore acceptance remain separate release gates.

Database connection settings, administration/pairing credentials, certificates,
Supervisor options and rotated technical logs remain outside the database. These
are bootstrap/security/operational files, not durable planning/configuration tables.

## Exact content and reuse

The database stores SHA-256-addressed finite JSON values, records and foreign-key
references. Plans share unchanged top-level fields. Input snapshots and physical
problems are additionally split into fields, so a new measurement does not copy
unchanged forecast, tariff, device-limit or time-grid blocks. Arrays remain useful
blocks. There is no table per scalar or general event-sourcing framework.

Every plan ID is immutable. Repeating the same save is idempotent. Changing content
under the same ID fails. Numeric JSON values, timestamp strings, nulls, commands,
strategy, original inputs, independently checked economics and expected flows are
preserved without rounding. Reconstruction verifies hashes and bounds record depth.
Retention follows the age policy below. Schema version 1 identifies the content-addressed graph.
An unsupported version fails startup instead of clearing or guessing at history.

## Retention and payload representation

`lifecycle/retention.py` preserves all original plans for 24 hours, one first publication per UTC quarter until age three days, one per UTC hour until age fourteen days, and one per HA local calendar day thereafter. Active state is always protected. The worker processes at most 32 plan candidates per round with a durable timestamp/ID cursor.

Publication also stores an independently hash-checked calculation context in `oep_plan_contexts` and a logical activation link in `oep_activation_contexts`. Existing plans are verified and backfilled in bounded batches before deletion is eligible. Thinning clears the old activation foreign key only after its logical identity is durable. Permanent compact control lists preserve Forecast Quality attribution. Original-plan API reads never return a calculation projection as though it were a complete original.

Unreachable shared record/leaf blocks are collected in bounded parent-before-child batches. A block referenced by any retained plan survives. Old detail payloads and captured quarters expire only behind a verified finalized daily Savings checkpoint. The context already active at that boundary, subsequent activations and active plan remain protected. See [daily settlement](SAVINGS.md).

`adapters/payloads.py` encodes profitable JSON of at least 4096 UTF-8 bytes using gzip level 1 and base64 in the existing text column, prefixed `oep:gzip:1:`. Content hashes always cover decoded canonical JSON. The bounded decoder checks the version, base64, gzip checksum/end marker, trailing data and expanded size of at most 64 MiB. Larger existing plain records remain readable and are not compressed. All new readers accept existing plain JSON. Existing records are recompressed with a durable primary-key cursor, at most 16 rows in one table per round. Incompressible records cannot starve later rows.

No background `VACUUM` or backend-wide compaction is performed. Deleted SQLite pages become reusable space. Other archive owners are not subjected to plan retention. Before rollback, restore a consistent matching backup. Old binaries do not understand the compression envelope. Restore is also the only way to recover intentionally expired originals. Cross-backend migration transfers and verifies the compressed records, compact contexts and finalized days through the complete shared schema.

Finished-table identities prevent repeated payload scans because new writes already use the codec. A compare-and-swap update preserves concurrent document writes, including case-only differences on MySQL and MariaDB.

## Deployment

SQLite is automatic at `/data/archive.sqlite3` (or the selected app data directory).
It uses WAL, `synchronous=FULL`, foreign keys and a five-second busy timeout. The
local directory/database are owner-only. Blocking reads/writes run outside request
processing on worker threads. Startup initializes before serving requests. One app
lock serializes mutations. Cancellation drains a writer before releasing that lock.
Hard invalidation revokes commands immediately in memory, then commits off-loop
before a protected API response. Public page and static-asset GET/HEAD requests,
and the bounded public setup-session metadata, do not wait for this write. They
contain no device commands or authenticated settings. This keeps the Home Assistant
sidebar and Installation page shell available during a slow or failed archive write.
Responses from authenticated API handlers retain the durability barrier, including
handler errors. Authentication failures return immediately without waiting for writes.
Background invalidations are also flushed at the existing
worker cadence and shutdown. Failed invalidation persistence never reinstates
commands. Failed publication retains only the previous normally valid plan.

For a new external archive, configure a dedicated database with supported credentials:

- PostgreSQL: `postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE`.
- MariaDB: `mariadb+pymysql://USER:PASSWORD@HOST:3306/DATABASE?charset=utf8mb4`.
- MySQL: `mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE?charset=utf8mb4`.

Use URL-encoded credentials and the driver's TLS parameters appropriate to the
server. MariaDB/MySQL tables explicitly use InnoDB and utf8mb4, with case-sensitive IDs.
Use the HA OS app's optional masked `database_url` setting, or `OEP_DATABASE_URL`
for a standalone container. The HA OS launch script reads the Supervisor option.
The installation wizard asks no database questions. No Recorder tables are used.
Database URLs/passwords never enter plan content, API responses or error messages.
The app disposes its owned engine only after workers have stopped. Injected engines
remain caller-owned.

External engines use five-second connection/pool waits. PostgreSQL statements are
bounded to 30 seconds and lock waits to five seconds. PyMySQL (MariaDB and MySQL)
uses 30-second socket read/write limits. These limits apply to both normal runtime
and explicit backend transfer: individual migration statements must finish within
the same bounds, while the complete transfer may take longer. A failed operation
remains an explicit failure. No timeout releases an engine while its worker is
still using it. SQLite retains its five-second busy timeout.

Connection, schema or transaction failures are explicit. There is no fallback to an
empty SQLite database. Only one active app instance is supported. The owner-only
`archive-backend.json` records the active location and source connection URL so
later migrations can reconnect. Treat this file as a secret, including in backups.
URLs never enter logs or HTTP responses. Password changes at the same location
update the private record without copying data.

## Switching database backends

The normal UI route is **Settings > Data storage > Change database**. Test the
destination connection, review existing target OEP data and explicitly confirm
replacement. The maintenance shell stays reachable while the app runtime drains,
copies and verifies all tables, checks startup and restarts automatically. The
source is retained. Before release it can be restored. After release, returning
requires a new migration to preserve new writes. A machine/process restart during
transfer stays in maintenance until recovery is chosen.

UI-selected connection settings persist outside the archive and override old
Supervisor/container database options, including an explicit return to SQLite.
Keep the entire private bootstrap directory in backups. See
[database maintenance and recovery](STORAGE-MIGRATION-UI.md).

The following explicit startup route remains available for administrators before
a UI-managed connection has been selected. After a UI-managed migration, use
Change database for subsequent transfers. Changing deployment options alone does
not override that selection. Do not delete recovery records to bypass this rule.

SQLite remains the default. To move existing database history to PostgreSQL,
MariaDB or MySQL (or back to SQLite):

1. Stop the app and ensure no other instance writes either archive. Keep its same
   persistent data directory: configuration, credentials and provider archives
   outside this database remain there unchanged.
2. Set the target `database_url` in HA OS app configuration, or `OEP_DATABASE_URL`
   for a standalone container. Clear/unset it to return to the default SQLite.
   Enable `migrate_database`, or set `OEP_MIGRATE_DATABASE=1`, explicitly accepting
   destruction of existing **OEP archive tables** in the target database. Use a
   dedicated database. Unrelated target tables are not deleted.
3. Start the app. Before serving requests or starting workers, the SQLAlchemy
   adapter validates the source, rejects source/target aliases, rebuilds target
   archive tables, copies all rows in bounded batches and compares every row.
   Original plans, inputs, configuration/source/model versions, selection evidence,
   identity, pause intent and active state survive.
   No plan is recalculated. Only a verified copy becomes the active backend.
4. Disable the migration option after success. A restart with the same location
   never repeats destructive migration, even if the option was left enabled.

A backend location change without explicit migration is rejected. Failure leaves
source history and the active connection record intact. The app fails startup
rather than using an empty archive. The rebuild commits before copying on every backend, so a failed copy may leave
the target schema empty. Correct the cause and retry, or restore the old target setting
to resume from the retained source. Do not delete source data until independently
satisfied with the destination. Subsequent archive schema upgrades are a separate
concern. Unsupported format versions still fail closed.

This transfers **all OEP database tables** from the shared schema registry:
plans, state, input blocks/references, configuration/source/model documents,
forecast pairs, published actuals and source-selection decisions. Every runtime
store borrows the same app-owned engine. Selection tables and their index use the
`oep_price_` prefix, so unrelated `pairs`, `actuals` or `decisions` tables are never
read, changed or dropped by OEP. On success no source connection is required,
including after another restart. The source remains only as recovery evidence.
Keep the small private bootstrap directory when changing backends: it contains
credentials and the selected connection, not an alternative configuration archive.

## Pre-release storage decision

No OEP version has been released. There is no converter for development JSON files
or previous development schemas, and no compatibility framework for those formats.
The user will manually reconfigure the test installation. Old development files
are not read or imported. Only transfers between supported backends containing the
current OEP schema are implemented. Unsupported format versions fail closed.
Home Assistant sensor identities and Recorder history are not modified by this change.

## Authenticated read API

- `GET /v1/plans?limit=20&before=PLAN_ID`: newest-first summaries with ID, UTC
  generated time, strategy and solution status. Limit is 1–100. `next_before` is
  the last returned identity when another page may exist. A final empty page is
  permitted. Equal timestamps use a stable case-sensitive ID tie-breaker.
- `GET /v1/plans/PLAN_ID`: original reconstructed JSON or 404. It does not install
  or change a plan. Read corruption/unavailability returns 503, not fabricated data.
- Both use the existing app authentication/ingress boundary. Invalid pagination
  returns 400. The existing current-plan endpoint remains unchanged.

## Backup and recovery

For HA OS, stop the app, include its complete app data in the normal HA backup, then
start it again. A raw copy of a live SQLite main file without WAL is not a consistent
backup. Standalone deployments use the equivalent controlled stop and volume copy.
For PostgreSQL/MariaDB/MySQL, stop app writes, take a server-consistent backup with the
server's tools and copy the matching app data directory before restarting. External
databases are not automatically included by a Home Assistant backup.

Restore database and matching app data together while the app is stopped. HA mode
automatically requests fresh Energy configuration from the integration without
opening the wizard, and requires fresh observations and a newly validated plan before commands are made
available. History inspection does not bypass this. Reopen tests prove archive
recovery; they do not establish complete HAOS backup/restore behavior.

## Tests

`tests/database/contracts/test_plan_archive_backends.py` runs the same reconstruction, block-reuse,
restart, immutable-ID, transaction-rollback, revocation, pagination, large-input,
corruption and unsupported-schema contracts on SQLite, PostgreSQL, MariaDB and MySQL.
`tests/database/migrations/test_archive_migration.py` checks every ordered pair of different backends,
plus SQLite-to-SQLite, with exact reconstruction and injected copy failure. Startup
checks additionally exercise SQLite-to-each-server and back while preserving the
same app directory. Both these tests and the existing forecast-selection storage
contracts use `tests/database/support/backends.py`. MariaDB does not substitute for MySQL.
`tests/database/contracts/test_document_backends.py` checks every durable typed store on all four
backends, including atomic version/pointer rollback and private-file exclusion.
Startup migration tests restart with the source unavailable and read retained
configuration/history entirely from the destination.
Local checks additionally prove SQLite settings and that a failed external
connection does not expose its password or create a fallback archive.

GitHub CI creates temporary real database services on its own runner. Required
database configuration cannot silently skip a backend. Tests use disposable CI
databases and reset only this archive's tables. App/actual-HA tests additionally
cover source changes, delayed publication, pause, stale acknowledgements, failures
and the authenticated history API. No physical equipment is controlled.

## Query index coverage

Primary keys serve immutable plan IDs, content hashes, `(owner, field)` input
parts and `(namespace, name)` configuration/source documents. The
`oep_plan_chronology (generated_at, plan_id)` index supports bounded newest-first
history pages, including equal-timestamp cursors. Forecast scoring and latest
source decisions use the [indexed price-read contracts](PRICE-FORECAST-SELECTION.md#indexed-reads-with-indefinite-retention).
Indexes are tied to actual query predicates. Unrelated foreign-key or JSON-payload
indexes are not added speculatively. The five-year price tests do not establish
full-plan reconstruction or settlement-query performance at that scale.
