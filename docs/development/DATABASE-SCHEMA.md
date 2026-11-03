# Database schema and data dictionary

This guide describes the implemented app database, not Home Assistant Recorder.
The authoritative SQLAlchemy Core registry is
[`storage/adapters/lifecycle/schema.py`](../../src/open_energy_planner/storage/adapters/lifecycle/schema.py).
All 18 tables use the same app-owned engine: SQLite by default, or PostgreSQL,
MariaDB or MySQL. The HA integration exchanges API messages and never reads SQL.
See [the plan archive guide](PLAN-ARCHIVE.md) for deployment, backup and cross-backend transfer.

## Storage conventions

- `PK` identifies every component of a primary key. `FK` identifies an enforced
  SQL foreign key. All columns are non-null unless explicitly marked **nullable**.
- Hashes are SHA-256 hexadecimal strings, up to 64 characters. Content-addressed
  plan blocks contain canonical finite JSON without numeric rounding. Hashes are
  identities, not encryption, and JSON payloads can contain installation/entity data.
- `String(n)` is bounded text. `Integer` stores integers. `Double` stores approximate
  numeric values. JSON uses `Text`, with `LONGTEXT` on MariaDB/MySQL. Tables use
  InnoDB/utf8mb4 there. Keys declared with `text_key(...)` use binary collation
  there to preserve case-sensitive comparisons. Ordinary `String` columns retain
  the backend's ordinary collation, including forecast-selection versions in
  `oep_price_pairs.primary_version`, `oep_price_pairs.alternative_version` and
  `oep_price_actuals.version`, even though these contain source-version hashes.
- Indexed plan and settlement timestamps are fixed-width UTC ISO 8601 strings
  with microseconds. Price-comparison evidence uses its adapter's UTC ISO strings.
  Decision `day` is a UTC calendar date. Original JSON timestamp strings are retained.
- Energy is kWh. Power fields declare W or kW through their `_w`/`_kw` suffix.
  Market prices and MAE are
  EUR/kWh. Settlement money is in the evaluation's currency. Negative prices and
  negative financial results are valid. Missing evidence is not replaced by zero.
- The schema is shared by runtime and backend transfer. There is no ORM object
  graph, database per module or direct connection to HA Recorder. A test-only
  selection-store constructor can create isolated SQLite. App runtime does not use it.

## Capacity read model

Settings > Data storage reads the active engine through authenticated
`GET /v1/storage`. It adds no tables, writes, pruning or migration.
The separate authenticated `/v1/storage/migration` maintenance API manages
cross-backend transfer from the same page. It adds no tables or columns. It copies
and verifies the complete shared registry with writers stopped, then validates
metadata, state and a rolled-back write before releasing the candidate runtime.
Private connection selection and the migration journal stay outside the moving
database. See [database maintenance](STORAGE-MIGRATION-UI.md) for ownership,
startup, interruption and recovery rules.
`database_bytes` is the logical file size including existing WAL and shared-memory
sidecars for file-backed SQLite. These files can change during measurement.
PostgreSQL uses `pg_database_size(current_database())`. MySQL and MariaDB sum
`information_schema.tables.data_length + index_length` for the connected schema.
Those table/index statistics are estimates and depend on the user's visibility.
External sizes include non-OEP tables and exclude server-wide storage overhead.

`disk_total_bytes` and `disk_free_bytes` measure the SQLite file's containing
volume. They are not reserved OEP capacity or quota information. Symbolic links
are resolved before measuring SQLite files and their volume. In-memory SQLite,
SQLite URI connections and external databases return null volume metrics.
The volume probe uses the database file itself so a file mount cannot report
its parent directory's unrelated capacity. If the file is missing, the probe
uses its containing directory while retaining the failed file-size measurement.
No remote failure falls back to application-host disk space. Null means unknown,
never zero. A measurement failure preserves independently available metrics and
sets `measurement_failed`. Exception messages, credentials, paths and database
names are excluded from the response. Reads run in a worker without the planning
state lock. Existing engine connection and SQL timeouts apply.

`status` is `low` when measured free bytes are below 1 GiB or below 5.0% of the
volume. Equality passes. A low reading takes precedence over partial measurement
failure. Otherwise status is `unavailable` on measurement failure, `unknown` when
capacity cannot be measured, or `ok`. This is an on-demand warning in the storage
page, not background monitoring or an HA notification. Refresh retries failures
and removes stale evidence. No data is deleted or automatically compacted.

## Relationships

Solid relationships below are SQL foreign keys. Other references, such as a
contract version inside JSON or an evaluation revision in a document, are
application-validated logical references rather than SQL constraints.

```mermaid
erDiagram
    oep_archive_plans }o--|| oep_archive_records : record_ref
    oep_archive_parts }o--|| oep_archive_records : owner
    oep_archive_parts }o--o| oep_archive_records : record_ref
    oep_archive_parts }o--o| oep_archive_values : value_ref
    oep_archive_state }o--o| oep_archive_plans : plan_id
    oep_plan_activations }o--o| oep_archive_plans : plan_id
    oep_settlement_evaluations }o--|| oep_settlement_frames : start
```

Parts have exactly one child reference: either a value or another record. Plans,
active state and activation events share plan identities. Settlement evidence is
separate from plans. An evaluation uses activation chronology and recorded context
to determine which advice and installation applied. Source-selection pairs join
published actuals logically on `(area, target)`. Published price events refer
logically to source versions retained in the `prices` document namespace.

## Immutable plans and execution state

Owned by [`SqlPlanRepository`](../../src/open_energy_planner/storage/adapters/lifecycle/sqlalchemy.py).
Publishing commits missing blocks, the immutable plan, activation history and
the current state in one transaction. Reading a historical plan reconstructs and
hash-checks its original content. It does not rerun the solver or apply today's rules.

Daily forecast quality projects `forecast_quality_controlled` through the existing
plan, parts and values keys for enabled activation identities. It reads no solver
inputs and verifies each distinct control leaf's content hash and list of entity
IDs. Missing plans and missing or corrupt referenced values fail the read. An
absent optional field contributes no controls, matching older full-plan reads.
This projection verifies the selected leaf rather than the complete plan hash.
No tables, columns or stored plan documents change.

### Current plan JSON and archived evidence

The SQL archive schema remains version 1. The execution document's `schema: 2`
is a separate JSON protocol version. It does not imply a SQL table migration.
Current calculations freeze an `InstallationProblem` under
`input_snapshot.problem` and publish per-device installation flows:

| Member | Current meaning |
| --- | --- |
| `intervals` | Contiguous absolute start/duration, buy/sell prices, household/PV energy, per-source PV quantities and optional original `control_group`. Billing splits retain each original control decision. |
| `inverters` | Stable converter identities, AC ratings, efficiency and topology. |
| `batteries` | Per-device `device_id`, `inverter_id`, nested battery capacity/initial stock/reserve/terminal limits/cell losses/wear/recovery state, DC efficiency and grid-following permission. |
| `pv_sources` | Source identity, converter connection and on/off curtailment capability. |
| `grid_import_limit_w`, `grid_export_limit_w` | Aggregate installation connection limits. |
| `export_bonuses` | Aligned compiled aggregate-grid-export rules with chronological counters and quotas. No inert singular `bonus` member or source-attribution bonus contract. |
| `terminal_valuations` | Per-device bounded usable inventory, reserve and terminal rate. |
| `appliances`, `ev_work`, `thermal_work` | Immutable work/session/comfort inputs governed by their independent validators. |
| `reserve_checkpoints` | Original interval boundaries used for ordered reserve recovery after billing/command refinement. |

Common `Interval`, `Battery`, `Costs`, `Status`, `SolveOptions` and `SolveResult`
Python contracts remain shared. Former single-network/shared-bus problem builders
and source-to-sink flow facades are not an alternate current JSON contract. Each
`expected_flows` entry retains aggregate grid import/export and identified
battery, inverter, solar, EV and thermal outcomes. Original `independent_costs`,
bonus results, immutable input hash and version evidence remain auditable.

Schema-2 `steps[].devices` contains authoritative typed per-device commands.
There is no scalar `battery_power_w` execution fallback. Original billing flows
remain separate from merged control steps. Appliance schedules, EV session
context and thermal context retain their execution safeguards.

Previously stored plans keep their exact original JSON, hashes, IDs, activation
history and measurement/configuration evidence. Archive reconstruction and
read-only history can return those documents without recreating old solver
classes, rerunning optimization or converting them to today's shape. Unsupported
old execution schemas are not sent as executable advice. This cleanup requires
no table, column, index or data migration and never rewrites historical plan
content. Archive schema version 1 remains unchanged.

### `oep_archive_meta`

Schema compatibility marker. Normal operation uses the singleton row `id = 1`.
An unsupported version fails startup. This is not a released-schema migration ledger.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `id` | Integer, PK | Singleton metadata identity. |
| `version` | Integer | Implemented archive schema version, currently 1. |

### `oep_archive_values`

Reusable leaf values: scalars, arrays and unsplit objects. Unchanged plan/input
blocks are stored once and referenced from many plan versions.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `digest` | String(64), PK | SHA-256 identity of canonical JSON content. |
| `payload` | Text | Exact canonical finite JSON value, verified during reconstruction. |

### `oep_archive_records`

Reusable objects whose individual fields live in the parts table. The record hash
represents the reconstructed object, not a separate payload row.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `digest` | String(64), PK | SHA-256 identity of the complete reconstructed object. |

### `oep_archive_parts`

The field map of each content-addressed record. Nested input/problem records can
reuse unchanged fields independently of changing measurements. Arrays remain blocks.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `owner` | String(64), PK, FK → oep_archive_records.digest | Record containing this field. |
| `field` | String(128), PK | Exact case-sensitive JSON member name. |
| `value_ref` | String(64), nullable, FK → oep_archive_values.digest | Leaf value. Null when the child is a record. |
| `record_ref` | String(64), nullable, FK → oep_archive_records.digest | Child record. Null when the child is a value. |

The `oep_one_part_reference` check requires exactly one non-null child reference.
Reconstruction also checks hashes and the adapter's maximum nesting depth.

### `oep_archive_plans`

Immutable plan identities and lightweight list metadata. Reusing an ID with
different content fails. Metadata avoids loading full plan JSON for history lists.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `plan_id` | String(128), PK | Stable plan identity used by APIs and acknowledgements. |
| `record_ref` | String(64), FK → oep_archive_records.digest | Full original plan: commands, grid, inputs, assumptions, expected flows and economics. |
| `generated_at` | String(40) | Normalized UTC creation time for history ordering. |
| `strategy` | String(128) | Original optimization strategy, retained independently of the user's current setting. |
| `solution_status` | String(128) | Solver outcome stored with the original plan. |

### `oep_archive_state`

Mutable singleton runtime checkpoint. It is not an additional historical plan.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `id` | Integer, PK | Singleton state identity, normally 1. |
| `payload` | Text | Canonical runtime state excluding the plan object: `instance_id`, boolean `enabled`, `last_control_id`, `acknowledged_plan`, and `measurement_config` once sources are stored. Invalidation clears the separate `plan_id` and records an activation, rather than adding a payload flag. |
| `plan_id` | String(128), nullable, FK → oep_archive_plans.plan_id | Current retained plan, or null when no plan is retained. Its presence alone does not establish current command validity. |

### `oep_plan_activations`

Append-only chronology of changes in the retained plan or enabled state. Generation
time and activation time serve different purposes. Settlement needs activation time.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `activated_at` | String(40), PK | UTC time the app commits the change. Not proof of physical execution or an HA acknowledgement. |
| `plan_id` | String(128), nullable, FK → oep_archive_plans.plan_id | Plan associated with the change. Null for removal/invalidation. |
| `enabled` | Integer, default 1 | Boolean flag encoded as 0/1. Whether execution was enabled. |
| `context_hash` | String(64), default empty string | Hash of comparison bindings to detect installation/source changes. Empty when no plan. |

Settlement's `context_plan()` skips null-plan rows and resolves the most recent
non-null archived plan at or before the interval for its physical/evaluation
context. Clearing commands therefore does not erase that historical context.
This does not make the preceding advice active: `active_advice()` reads the full
activation timeline, including null/disabled rows, and clips advice to plan validity.
An interval without active advice can still be settled from its measurements.

## Durable documents

Owned by [`SqlDocuments`](../../src/open_energy_planner/storage/adapters/documents.py)
and each typed store. Document names ending in `.json` are database keys, not files.
The adapter permits updates. Immutability and payload validation belong to the owner.

### `oep_documents`

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `namespace` | String(128), PK | Validated module namespace from the list below. |
| `name` | String(128), PK | Flat document key, such as `current.json`, a version or an evaluation pointer. |
| `payload` | Text | Finite JSON object, array, scalar or null, decoded and validated by its typed owner. |

| Namespace | Owner / payload contents | Version and pointer use |
| --- | --- | --- |
| `contracts` | `ContractStore`: effective-dated purchase/sale formula and bonus rule books. | Immutable `<version>.json`. `current.json` selects a book. |
| `bonus-counters` | `CounterStore`: confirmed historical counter seeds, period context and optional origin evidence. | Immutable `<version>.json` books and `<seed-digest>.origin.json` provenance. `current.json` selects a book. Scenario continuation is in settlement state. |
| `savings-bonus-counters` | `CounterStore` via `HistoricalBonuses`: automatically derived historical bonus seeds and optional origin evidence. Confirmed planning books remain in `bonus-counters`. | Immutable `<version>.json` books and `<seed-digest>.origin.json` provenance. `current.json` selects a book of at most 64 seeds. |
| `installation` | `InstallationStore`: Energy-bound virtual devices, limits, relations and settings. | Immutable `<version>.json`. `current.json` selects settings. |
| `demand` | `DemandStore`: selected consumption sources/settings. Forecast values and fitted model parameters. | `configuration-<version>.json`, `forecast-<version>.json`. `current.json` selects configuration. |
| `solar` | `SolarStore`: per-source configuration, forecasts/models, weather/history blocks and receipt times. | Content-addressed kind/version documents. Unchanged daily `block-<hash>.json` reused by manifests. `current.json` selects configuration. |
| `prices` | `PriceStore`: normalized source series with original receipt/issue/expiry times, published/forecast flags and resolution. | Immutable `<version>.json`. `current.json` is the list of current series references. No raw provider payloads or API secrets. |
| `planning` | `StrategyStore` in `optimization/storage/__init__.py`: requested optimization strategy. `SelectionStore`: immutable forecast-decision captures/evaluations and current model preference evidence. | `current.json` remains the strategy scalar. `model-selection.json` indexes families. `model-capture-<hash>.json` and `model-evaluation-<hash>.json` preserve content-addressed decision evidence. |
| `savings` | `SavingsRuntime` and `PlanValuations`: capture cursor, evaluation metadata, repricing queue, retained valuation results and pointers. | `capture.json`, `physical-reference.json`, `head.json`, `working.json`, `evaluation-<revision>.json`, `repricing.json`, `valuation-<identity>.json`, `planvalue-<hash>.json`, `valueissue-<hash>.json`. Current pointers/queue can change. Retained evaluations/valuations preserve original evidence. |

These are module-owned JSON contracts, not additional SQL columns. Follow the
typed stores and wire models for nested fields: the database adapter does not
interpret devices, formulas or model coefficients. Database URLs, tokens,
certificates, Supervisor options and technical log files remain outside this table.

Solar `configuration-<version>.json` source objects may contain `panels`, a bounded
list of exact planes with `panel_id`, `capacity_w` in Wp, `azimuth` in degrees
clockwise from north and `tilt` in degrees. Plane capacities must sum to the source
capacity. They describe geometry behind the existing source meter, not new meters
or physical inverter connections. Empty planes retain the manual orientation
convention. `ha_imports` retains the original normalized snapshots separately from
editable planes. Each contains adapter identity, HA config-entry identity, original
planes and optional `inverter_limit_w` in W. This limit is provider model metadata,
not proof of physical wiring. Credentials are excluded. Immutable configuration
versions preserve import provenance after edits. `current.json` changes only on
explicit save. Empty optional geometry/provenance fields are omitted from the
canonical version input so unchanged saved manual definitions retain their identity.
Each plane may also contain a display `name` of at most 200 characters. Missing
names default to empty text and empty names are omitted from canonical version
input, preserving existing archived identities. Names are retained in editable
planes and original HA snapshots. They do not change stable `panel_id` values or
the source accuracy family. Renaming remains an explicit configuration save.

The HA catalogue's optional `suggestions.solar_configurations` list associates each
Energy production `entity_id` with normalized `providers` and an availability
`issue`. Only complete unambiguous groups seed new drafts. Later catalogue refreshes
do not overwrite saved OEP geometry. This discovery metadata belongs to the existing
catalogue contract and does not add tables, columns or a second source registry.

Solar source objects also contain `forecast_source`, one of `automatic`, `pvwatts`,
`ha` or `ml`. Its default `automatic` is omitted from the canonical configuration
hash. Forecast documents may contain `valid_until`, an exclusive aware UTC provider
freshness deadline. Null leaves the existing OEP forecast age policy authoritative.
Selected HA forecasts must pass both deadlines. `weather_version` is `none` when
an explicit all-HA forecast was built without independent weather.

The `solar` namespace additionally owns these JSON contracts:

| Document name | Contents and semantics |
| --- | --- |
| `ha-first-<version>.json` | First normalized observation of exact provider configuration and native AC `steps` with aware `start`, `duration_h` in elapsed hours and `kwh`. Partial sunrise/sunset and native quarter intervals are retained. Contains `observed_at`, nullable `provider_issued_at` and nullable `provider_refreshed_at`. Content identity excludes observation and refresh times. The first record is never rewritten by cache reads. |
| `ha-receipt-<hash>.json` | Immutable meter `entity_id` and normalized prediction including actual observation/refresh times. Credentials and raw provider payloads are excluded. New real refreshes retain separate provenance. |
| `source-day-<family>-<local-date>.json` | First forward capture for one measured source/local day. Retains exact UTC `hours`, HA timezone, original configuration/baseline/ML references, original `ha_versions` and per-model hourly kWh values. The family scopes physical source/inverter definitions and location, excluding requested source preference. |
| `source-choice-<forecast-version>.json` | Original source-selection statuses, requested/active source, matched comparison days, common-cohort per-model MAE in kWh per hour, required/completed day counts, measurement history version, policy and HA observation references. Choices do not rewrite earlier forecasts or selections when Recorder history changes. |

Selected forecast documents also retain their source selection and HA observation
references. Raw HA observations are AC production evidence, not independently
observable available DC. Source-day windows are immutable. Rankings may be derived
again from corrected measured sums, while earlier choices retain their original
history version. All documents use the shared SQLAlchemy engine, existing namespace
and namespace/name index, and participate in four-backend transfer. No new SQL
columns, table, file store or retention deletion is introduced.

HA prediction observations additionally retain `configuration_revision`, an opaque
hash of HA's public entry ID and modification timestamp. Options and subentry
updates change it without storing or hashing credentials. Source-day documents
retain the sorted `ha_configuration_versions` for their HA candidate. Comparisons
require the current revisions, even when predicted values happen to be unchanged.
Source-day captures are written only once every currently usable model covers the
whole comparison window. Malformed or unreadable optional comparison records
disable automatic promotion and remain retained. They do not block valid physical
forecast publication or force a manually selected source to use accuracy evidence.

Thermal storage uses the existing `planning` namespace. `thermal-state.json`
selects the complete configuration. Immutable `thermal-state-<digest>.json`
documents retain each saved configuration before its pointer changes. A store
retains its opaque UUID, Energy consumer, temperature and power entity bindings,
litres, minimum/maximum/ambient temperatures, W/°C loss coefficient, whole-watt
heater rating, enabled intent and 24 local-hour thermal draw rates. These are
model assumptions. Current observations are not restored as fresh after restart.
Original plan documents retain `thermal_work`, `thermal_evidence`, execution
`thermal_context` and interval `thermal` flows, preserving source reports,
configuration, declared draw, AC intake, temperature, bounded unserved draw
(`unmet_draw_kwh`) and comfort deficit. The existing
namespace/name key serves point reads. No new table, column or index is needed.
Backend transfer includes all of these documents and plan archives.
Thermal work also retains `state_at`, the model initialization time, and
`initially_heating`, its measured-running admission classification. Original HA
report timestamps remain in `thermal_evidence`. Each thermal reading also retains
`captured_at`, the integration's current-state capture time. Admission expires
from that capture, not from an unchanged HA sensor timestamp. It is runtime
evidence frozen into the original plan, never a fabricated physical sample time.
General measurement evidence similarly retains original `observed_at` and
`reported_at` alongside the complete report capture. Explicit device sample metadata is retained
without an age-only rejection. The combined export-counter view uses its verified capture
time with internal `timestamp_kind = ha_capture`, while retaining original component
readings. This tag identifies a checked HA view, not a new physical device sample.
The incoming sensor-report contract still uses its original provenance kinds.
These fields distinguish passive
state evolution from the permission boundary for starting new heat.

The `installation` JSON additionally retains Energy-owned battery
`charge_energy_entity` (the cumulative charging meter) alongside `energy_entity`
(discharging), `soc_entity`, normalized `power_entity` and usable `capacity_kwh`.
`additional_grid_power_entities` is an ordered list of normalized grid power
sensors after the first `grid_power_entity`. All participate in the same required
measurement generation. `additional_grid_export_entities` similarly retains all
cumulative export meters after `grid_export_entity`, with separate required kWh
readings. Empty additional lists and absent charge links are omitted
from records so older immutable hashes remain readable. New HA catalogues supply
the complete links. The setup API rejects overrides. Valid changes create new
archived definitions instead of rewriting old documents or plan snapshots.
Household demand retains all signed cumulative meters in its own `terms`. Grid
settlement retains separate import/export channels rather than subtracting them
before billing. See [Energy mapping](ENERGY-DASHBOARD.md) for ownership and units.

Multi-meter bonus seeds additionally retain `component_meters`: ordered
`[entity_id, cumulative_kwh]` pairs at their baseline. The aggregate `entity_id`
is a stable internal identity derived from the exact sorted physical meter set.
It is not a new HA entity. `meter_kwh` equals the component sum. These endpoints
prevent a reset in one meter from being masked by growth in another. Empty
component evidence is omitted from legacy seed documents/hashes. Compact history
responses carry the same endpoints. Aggregate histories require common complete
hourly coverage. Counter archives and backend transfers retain the whole seed.

### Annual statistics counter document extension

`bonus-counters` seed JSON additionally stores `statistics` for annual Recorder
sum accounting. It contains aware UTC `reference_at` and `latest_at` completed
hour endpoints, `series` with each `statistic_id`, integer `metadata_id`, `source`,
native `unit`, normalized kWh `reference_sum` and `latest_sum`, nullable
`prefix_kwh`, nullable `confirmed_at`, SHA-256 `history_digest`, boolean
`physical_available`, nullable `issue` and nullable SHA-256 `rejected_digest`.
An unknown prefix is null. It is never an implicit zero. The only persisted issue
is `bonus_statistics_revalidation_required`. Sum values may be signed. The
nonnegative annual volume equals prefix plus the sum of nonnegative component
differences. Negative endpoint differences contribute no measured export, while
raw signed sums retain their original values and the fixed reference never moves.
The history digest binds the accepted identity and reference/latest endpoint pair.
Intermediate rows are not continuity evidence for this annual sum route.

`used_kwh` retains that volume at `observed_at = statistics.latest_at` and is null
until a required prefix is confirmed. Physical
`meter_kwh` and `component_meters` remain endpoint evidence for settlement only.
When physical state is absent, `meter_kwh` is null, `component_meters` is empty
and `physical_available` is false. They never become synthetic zero readings.
Annual planning uses the sum basis.
The source identity equals the exact series membership. Each series is checked
independently. A negative difference is floored separately per meter before addition.
New latest-sum corrections are accepted. Changed reference identity/value or
missing endpoints still invalidate the counter. Previous book documents remain immutable.

Immutable origin documents contain `statistics`, `recorded_at`, `source`
(`home_assistant_history` or `manual_confirmation`) and nullable `supersedes`
(the predecessor book version). These retain original and rejected correction
provenance. Books, origins and the current pointer use one existing document
transaction. No SQL table, column, index or external store is added. The shared
four-backend transfer includes all of these documents. See
[annual statistics counters](BONUS-STATISTICS.md) for validation and API semantics.

### EV document extension

The existing `planning` namespace stores `ev-state.json` and immutable
`ev-state-<sha256>.json` archives. Their JSON contains `vehicles`, `points` and
`assignments`. Vehicle records contain opaque ID, HA percentage-SoC entity,
usable `capacity_kwh`, AC `maximum_w`, `target_soc`, local `departure_time` and
optional aware `departure_override`. Point records contain opaque ID, Energy
`energy_entity`, AC `power_entity`, binary `connected_entity`, active
`minimum_w`/`maximum_w`, `control` capability and allowed `vehicle_ids`.
Assignments link point IDs to explicit vehicle IDs and current connection
`session_id` nonces. They are intent, not fresh measurement evidence. A restart
requires new HA readings and matching connection identity before use.

Each accepted mutation archives the complete state and updates its pointer in
one SQLAlchemy transaction. No SQL column/index/table is added. Queries use the
existing document primary key. Cross-backend transfer includes every pointer and
archive. Credentials and technical logs are excluded.

Original plan JSON adds `input_snapshot.ev_revision`, `ev_evidence` and typed
`problem.ev_work`. It freezes session/point/vehicle IDs, aware readiness/departure,
usable capacity, measured/target SoC and physical power/control limits.
EV evidence also freezes the SoC source entity, optional device timestamp
attribute, original sample/report timestamps and the versioned delivered-power
loss curve. `problem.reserve_checkpoints` preserves the original stationary
reserve-priority boundaries when EV stop events refine billing intervals.
`expected_flows[].ev` stores `session_id`, AC `ac_kwh`, useful `stored_kwh` and
active `power_w` for that billing segment. `steps[].devices` has EV commands with
kind, finite nonnegative AC power in watts, vehicle and session identity. `ev_schedule` retains
per-session required/planned useful kWh, departure, predicted SoC and shortfall.
All are immutable original evidence, transferred with the plan archive.

## Price forecast selection

Owned by [`SqlAlchemySelectionStore`](../../src/open_energy_planner/prices/storage/adapters/selection_store.py).
Paired forecasts are frozen before published prices are known. Scoring joins only
matched target hours, averages errors across cohorts per hour and weights hours
equally. Daily decisions are per market area and lead-time bucket, not global.
See [price source selection](PRICE-FORECAST-SELECTION.md) for policy thresholds.

### `oep_price_pairs`

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `area` | String(32), PK | Market/bidding area. Keeps evidence from different markets separate. |
| `bucket` | Integer, PK | Zero-based forecast lead bucket defined by price policy. |
| `cohort` | String(40), PK | UTC receipt hour shared by both as-received forecasts. |
| `target` | String(40), PK | UTC delivery hour being predicted. |
| `primary_price` | Double | Primary source's original hourly EUR/kWh prediction. |
| `alternative_price` | Double | Alternative source's prediction for the same hour/cohort. |
| `primary_version` | String(64) | Logical reference to the normalized primary source version. |
| `alternative_version` | String(64) | Logical reference to the normalized alternative source version. |

The composite key prevents overwriting an original pair on repeated receipt.
Provider identities are supplied by composition. They are not stored per pair.

### `oep_price_actuals`

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `area` | String(32), PK | Market area matching forecast pairs. |
| `target` | String(40), PK | Complete published UTC delivery hour. |
| `price` | Double | Mean published hourly EUR/kWh price used as scoring truth. |
| `version` | String(64) | Normalized published source version. Newer published observations can update this projection. |

This hourly scoring projection is distinct from native-resolution settlement prices.

### `oep_price_decisions`

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `area` | String(32), PK | Market area assessed. |
| `bucket` | Integer, PK | Forecast lead bucket assessed. |
| `day` | String(40), PK | UTC assessment date (`YYYY-MM-DD`). One assessment per day. |
| `preferred` | String(128) | Selected source ID after this assessment. |
| `challenger` | String(128), nullable | Source accumulating evidence to replace the preference. |
| `streak` | Integer | Consecutive qualifying daily assessments for that challenger. |
| `primary_mae` | Double, nullable | Primary mean absolute error in EUR/kWh. Null without scored evidence. |
| `alternative_mae` | Double, nullable | Alternative MAE on the same matched evidence. |
| `hours` | Integer | Number of matched target hours in the scoring window. |
| `days` | Integer | Number of distinct settled target dates represented. |
| `reason` | String(64) | Machine-readable outcome, such as insufficient evidence, pending challenger or switch. |

## Published price projection and correction events

Owned by [`prices/storage/adapters/published.py`](../../src/open_energy_planner/prices/storage/adapters/published.py).
Saving normalized sources also updates these tables in the same transaction.
Only published facts enter them. Forecast prices never become measured settlement truth.

### `oep_published_prices`

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `area` | String(32), PK | Market area. |
| `currency` | String(32), PK | Currency, currently EUR in the market adapters. |
| `start` | String(40), PK | UTC delivery interval start at native published resolution. |
| `end` | String(40) | Exclusive UTC delivery interval end. |
| `received_at` | String(40) | Receipt time of the retained projection. Older receipts cannot replace it. |
| `version` | String(64) | Logical normalized source-version reference. Original versions remain in price documents. |
| `price` | Double | Published wholesale price per kWh, before contract formulas/bonuses. |

This is the current projection. Corrections may replace its values. Evaluations
retain the exact tariff/source evidence they used, so old results remain explainable.

### `oep_published_price_events`

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `sequence` | Integer, PK | App-assigned increasing ingestion sequence. Not database auto-increment or provider time. |
| `area` | String(32) | Market area for correction scans. |
| `currency` | String(32) | Currency for correction scans. |
| `start` | String(40) | Delivery interval whose price/coverage changed. |
| `version` | String(64) | Source version that introduced the change. |

Insertion records new prices and semantic changes to price or interval end.
A version-only refresh creates no event. Consumers retain a sequence cursor so
a later-received correction with an old delivery timestamp is still noticed.
There is no SQL FK to a projection row or JSON source version.

## Measurements and continuous savings

Owned by [`SettlementStore`](../../src/open_energy_planner/savings/storage/adapters/sqlalchemy.py),
with calculation in `savings/accounting.py` and orchestration in `savings/runtime.py`.
Only selected settlement evidence is copied from HA, not every sensor state.
See [savings](SAVINGS.md) for the reference scenario, valuation and quality policy.

### `oep_settlement_frames`

Immutable measured/estimated evidence at up-to-quarter-hour intervals, including
fragments at configuration boundaries. Conflicting replacement fails explicitly.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `start` | String(40), PK | UTC interval start, inclusive. |
| `end` | String(40) | UTC interval end, exclusive. Not necessarily a whole quarter. |
| `payload` | Text | JSON `{frame, context}`: selected source evidence plus its binding/counter/location context. |

Inside `frame`: `start`/`end`, `channels` (non-negative AC kWh deltas by HA
entity ID), `battery_start`/`battery_end` (internal kWh by virtual battery ID),
`meter_start` (cumulative start readings for counter initialization), and `quality`
(`recorded`, `estimated_hourly`, `estimated_boundary` or `estimated_soc`).
`estimated_soc` marks bounded historical unavailable-SoC reconstruction with
equal valid bounding percentages and unchanged charge/discharge counters. It
may also include estimated energy endpoints. It never resets the independent
reference stock or replaces existing immutable evidence. Direction comes from
source bindings. Absent channels remain absent. Context retains bindings, counter seeds and location. The applicable plan is
resolved separately through activation history and the immutable plan archive.

### `oep_settlement_evaluations`

Immutable interval result per evaluation revision. Repricing produces a new
revision rather than modifying the original result. Typed numeric projections
support SQL period aggregation without loading years of JSON.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `revision` | String(64), PK | Logical identity of this continuous evaluation and its assumptions. |
| `start` | String(40), PK, FK → oep_settlement_frames.start | Measured interval evaluated. |
| `payload` | Text | Full interval result: end, quality, both scenarios, tariff evidence and evaluation details. |
| `actual_purchase` | Double | Actual scenario's signed grid purchase cost. |
| `reference_purchase` | Double | Reference scenario's signed grid purchase cost. |
| `actual_sale` | Double | Actual scenario's signed export revenue. |
| `reference_sale` | Double | Reference scenario's signed export revenue. |
| `actual_bonus` | Double | Actual scenario's earned bonus. |
| `reference_bonus` | Double | Reference scenario's earned bonus, with independent period counters. |
| `actual_grid_cash` | Double | Actual purchase minus sale minus bonus. |
| `reference_grid_cash` | Double | Reference purchase minus sale minus bonus. |
| `actual_wear` | Double | Actual configured proportional battery cycle cost. |
| `reference_wear` | Double | Reference proportional cycle cost under the same settings. |
| `actual_inventory_change` | Double | Actual closing minus opening stored-energy value. |
| `reference_inventory_change` | Double | Reference closing minus opening stored-energy value. |
| `actual_economic_cost` | Double | Actual grid cash + wear − inventory value change. |
| `reference_economic_cost` | Double | Reference grid cash + wear − inventory value change. |
| `actual_import_kwh` | Double | Actual grid import energy. |
| `reference_import_kwh` | Double | Reference grid import energy. |
| `actual_export_kwh` | Double | Actual grid export energy. |
| `reference_export_kwh` | Double | Reference grid export energy. |
| `savings` | Double | Reference economic cost minus actual economic cost. Savings can be negative. |
| `avoided_import_kwh` | Double | Reference minus actual grid import. Avoided import can be negative. |
| `estimated` | Integer | 0/1 flag: non-recorded frame quality, estimated available PV or estimated subinterval reconstruction. |
| `duration_h` | Double | Interval duration in hours, used for coverage rather than assuming one row equals one quarter. |

`actual` derives import/export from measured channels and wear from measured
battery discharge with configured losses and cycle costs. Contract and inventory
valuation assumptions determine the financial amounts. Recorded advice is
retained for provenance and curtailment/reference reconstruction. It does not
replace measured quantities or prove that physical devices followed the plan.
`reference` is the continuous
self-consumption scenario with unrestricted PV. Battery states and bonus counters
evolve independently from a shared starting point. A date filter reads these
continuous results. It does not restart the reference battery or bonus budgets.

### `oep_settlement_state`

Mutable continuation checkpoint, committed with its interval result to prevent
double-counting after retries or restart.

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `revision` | String(64), PK | Logical evaluation identity shared with evaluations/documents. No SQL FK. |
| `payload` | Text | Continuous actual/reference battery inventory, bonus counters and other continuation state used by the evaluator. |

## Indexes and query ownership

Primary keys provide the lookup/order prefixes below. The schema also declares
three secondary indexes. No per-JSON-field or speculative index is implied.

| Key / index | Query it supports |
| --- | --- |
| Plan PK `plan_id` | Exact original plan lookup and activation/state references. |
| `oep_plan_chronology(generated_at, plan_id)` | Newest-first plan summaries and stable keyset pagination, including equal timestamps. |
| Value/record PK `digest`. Parts PK `(owner, field)` | Batched block reuse and field reconstruction of one retained plan. |
| Document PK `(namespace, name)` | Exact configuration/version/current-pointer reads. |
| Activation PK `activated_at` | Last activation at/before an interval, subsequent changes and configuration-boundary scans. |
| Pair PK `(area, bucket, cohort, target)` | Idempotent forecast-pair insertion. |
| `oep_price_pair_targets(area, bucket, target)` | Bounded scoring window without scanning every retained receipt cohort. |
| Actual PK `(area, target)` | Published-hour join during matched scoring. |
| Decision PK `(area, bucket, day)` | Latest daily preference per area and lead bucket. |
| Published price PK `(area, currency, start)` | Delivery-period settlement price reads. |
| Event PK `sequence`. `oep_published_event_area(area, currency, sequence)` | Global committed ingestion sequence and per-area/currency correction cursor scans. |
| Frame PK `start` | Bounded chronological evidence capture/replay. |
| Evaluation PK `(revision, start)` | Interval detail and SQL sums within one revision/date range. Day/month chart bins. |
| Settlement state PK `revision` | Resume a specific continuous evaluation. |

These support implemented access patterns. A filtered activation scan or grouped
aggregate can still examine rows in its selected range. An index is not a guarantee
that every arbitrary multi-year query is constant-time. Backend-specific implicit
indexes for constraints are implementation details. Add performance proof when a
new query shape or materially larger payload is introduced.

## Lifecycle and maintenance

- One app instance serializes writes. SQL transactions preserve publication and
  result/checkpoint boundaries. SQLAlchemy does not make arbitrary concurrent app
  writers safe. SQLite uses WAL, full synchronization, enabled foreign keys and
  a bounded busy timeout. External database failures never silently select SQLite.
  External engines bound connection/pool waits to five seconds. PostgreSQL applies
  a 30-second statement timeout and five-second lock timeout. MariaDB/MySQL use
  30-second socket read/write timeouts. Runtime and backend migration share these
  limits per operation, rather than a timeout for the entire migration.
- There is no automatic pruning of plans, measurement frames, evaluations or
  referenced content. Mutable pointers, checkpoints and current projections can
  advance. Historical plan reconstruction and revaluation are different operations.
- Cross-backend transfer rebuilds the target's OEP tables only after explicit
  consent, copies **every registered table** in dependency order, and verifies
  retained data. The stopped source is retained. After successful switching the
  runtime uses only the target. Connection/bootstrap files remain outside SQL.
- Back up the database consistently, including all related tables, and the
  required private bootstrap files. HA's app-data backup can include SQLite.
  An external server requires its own consistent database backup. Do not copy
  a live SQLite main file alone while ignoring WAL/transaction consistency.
- This guide describes schema version 1. Every database change requires updating
  this dictionary, including semantic, index/query, JSON-contract and ownership
  changes even when the registry does not change. Update the applicable
  four-backend contract/transfer tests when their contract or transfer behavior
  changes. Tests check table
  and column coverage. Explanations, JSON contracts and query suitability still
  require review.



### Plan comparison chart projections

New `reference_forecast.reference` interval JSON can include deterministic
`inverters` (converter ID, AC import/export kWh) and `solar` (source ID, used DC
kWh) alongside retained grid/battery outcomes. These optional facts use the same
immutable plan archive and introduce no tables, fields or indexes. Old reference
rows remain readable. Chart replay must match saved grid/stock outcomes first.
`scenarios` in the comparison API is a read-only projection, not another database
or a reset of continuous settlement state. Both forecast references and original
spot/import/export tariff evidence are described in [plan evidence](PLAN-EVIDENCE.md).


## Forecast model decision documents

`planning/model-selection.json` has `policy` and `families`. Each family is keyed
by a digest of installation, demand, solar, contract, confirmed-counter and strategy
definitions, location and model/feature mode. A family retains `windows` (up to three
chronological evaluation hashes), `pending` (a capture hash or null), optional
`retry_after`, `issue` and `last_failure` evaluation reference. Preferences are
derived from checked financial evidence rather than an editable activation flag.

`model-capture-<hash>.json` retains policy, original plan ID, issue/start/end times,
family/kind/source, strategy/contract reference, equal projected opening stock and
counter positions, and baseline/candidate forecast references, model IDs and frozen
command steps. The original plan ID is a logical reference to `oep_archive_plans`.
No additional SQL FK or duplicated topology archive is introduced. Forecast/model
references point to existing demand/solar documents. kWh, W and currency conventions
are unchanged. Both native solutions are independently checked before retention.

`model-evaluation-<hash>.json` retains capture/family references and evaluation time.
Successful records include the realized window, measured common opening battery
stock and each scenario's purchase/sale/bonus/wear, inventory change, economic cost,
corrected import, end stocks, independent counters and exact tariff versions.
Failed records retain the reason, never fabricated numeric scores. Captures,
evaluations and referenced original plans are retained indefinitely. Only the
current bounded reference index changes transactionally. All documents are copied
by complete cross-backend transfer. Names use the existing namespace/name primary
key. Frame/price reads use their existing time/area indexes. No extra index is
needed for three point-read evaluations and one pending capture per family.

Forecast documents retain their original model identity. Quarter-projection
algorithms use `startup-v2`, `weekday-hour-v2`, `ridge-v2`, `pvwatts-v2` and
`pvwatts-calibrated-v2`. V1 evidence remains immutable/readable. The JSON shape,
tables and cross-backend migration contract are unchanged. Selection families
include these identities and cannot reuse v1 decision windows for v2 preferences.

### Individual forecast documents in `demand`

Phase-1 consumer forecasting uses the existing `documents` table and its `(namespace, name)` primary key. No new table, index or backend-specific query is added. `consumer-settings.json` contains a bounded list of `{entity_id, weather_sensitive}` choices. Each save also retains `consumer-settings-<sha256>.json`. Energy source membership and upstream hierarchy arrive separately through HA metadata, not user-editable meter definitions. The optimistic settings version includes the current source catalogue.

Individual-consumer ownership now lives in `appliances/forecasting.py`. Cycle,
EV and thermal services live in `appliances/cycles`, `appliances/ev` and
`appliances/thermal`. The shared selected-device settings presentation does not add another table,
document or index. EV inputs display kW while persisted power fields remain
whole watts. Display conversion does not change the JSON contract. Existing demand/planning
namespaces, pointer/archive names and payloads are unchanged. Module paths are
not serialized. See [shared appliance structure](APPLIANCE-ARCHITECTURE.md).

`consumer-forecast-<forecast-version>.json` stores the complete composite household forecast and its `components`: source identities, per-device forecast intervals (UTC start, duration in hours, kWh), residual forecast, settings version and fitted profile/Ridge parameters. Forecasts and parameters retain their original issue/training identities. No executable estimator pickle is stored. The composite has no excluded devices because residual plus device demand is already recombined. A missing component cannot be archived as zero. Selection/evaluation references stay in the existing planning documents. History streams are transient training inputs. The archived forecasts/parameters are the evidence used for planning. All these rows join existing all-table database transfer and backup procedures automatically and must be covered by the four-backend document contracts.

The component JSON additionally retains `baseline_forecast` per consumer and
`residual_baseline`, using the existing historical weekday/hour algorithm. These
are frozen inputs for a changed measurement boundary when explicit cycle work
replaces a device-use expectation, not approval of an ML challenger.
Transient consumer results may retain ready device records and a residual
baseline while another selected consumer lacks history. That baseline excludes
only the ready records, retaining unavailable consumers in household demand.
No incomplete composite archive is written. Plans retain the actual boundary
and component evidence through their existing immutable input documents.
Each ready device record has its own forecast status. Current activation labels
do not overwrite the original archived component records.

`quality-components-<household-forecast-version>.json` retains either the existing
`{version, selected}` reference to a complete composite archive, or
`{selected: false, components}` with the original ready-device predictions and
fitted parameters inline when composition is incomplete. Both forms are
immutable. Inline evidence lets historical quality views retain valid device
forecasts independently of an unrelated meter's missing training history.

### Appliance documents in `planning`

No new table or SQL index is needed: active work is bounded to sixteen selected devices.
Retained records for removed devices do not count against that limit or appear
in the live device projection. Historical records remain durable. Active work is
restored once and accessed by the existing `(namespace, name)` primary key. App
HTTP actions and plan publication serialize mutations with the application lock.

| Document key | Fields / meaning |
| --- | --- |
| `appliance-state.json` | Current pointer document: `devices` keyed by Energy entity, explicit `shiftable` permission, average `profile`, optional aware ISO `finish_before` draft for HA dashboard controls. `tasks` keyed by entity containing the latest task. |
| `appliance-state-<sha256>.json` | Immutable model/task state version for each accepted transition, excluding mutable history progress. Old versions are retained. |
| `appliance-progress.json` | Mutable `devices` map containing each source-bound `learning_history` and `history_import` described below. Coverage and checkpoint-only refreshes do not create immutable task/model versions. Startup joins this document with the current state for the live device projection. |
| `appliance-task-<uuid>.json` | Latest durable task: ID, source entity, readiness request ID, aware `ready_at`/`deadline`, frozen average profile, task status, scheduled start, accepted plan ID, attempt and ordered evidence events. Earlier revisions remain in state archives. |
| `appliance-request-<sha256>.json` | Exact readiness request and resulting task ID. Prevents a transport retry from creating another runnable cycle. |
| `appliance-cycle-<sha256>.json` | Completed-cycle identity, source entity, actual start/end, fine cumulative kWh points (`at`, `kwh`) and synthetic provenance. Prevents duplicate learning or reuse with different evidence. |
| `appliance-hourly-<sha256>.json` | Hourly estimated run identity from source entities and UTC bounds. Stores `id`, `entity_id`, `power_entity`, `started_at`, `completed_at`, original cumulative kWh `points`, `synthetic` and `quality: hourly_estimate`. Atomically committed with the hourly checkpoint. Retained separately from fine detected cycles. |
| `appliance-detected-<sha256>.json` | Automatic cycle identity from energy/power entities and detected UTC boundaries. Stores `id`, `entity_id`, `power_entity`, `started_at`, `completed_at`, measured `energy_kwh`, `synthetic` and cumulative kWh `points`. Existing keys prevent duplicate learning. |

Profiles contain `power_w` (consecutive quarter-hour mean AC W), `samples` (positive
completed-cycle count, or one placeholder sample for manual estimates) and `evidence` (`measured`/`synthetic`/`manual`/`hourly_estimate`). Device settings may retain `manual_profile` (`duration_minutes`, `energy_kwh`), `learned_profile`, `learning_source` (Energy power entity), `last_detected_end` (UTC watermark rejecting overlapping ranges) and up to twelve `recent_cycles` summaries. The effective `profile` switches to automatic learning after two complete samples. Original manual values and detected archives remain retained. Power binding changes withdraw learned evidence and restore the manual fallback. Additional source-bound JSON fields in `devices[entity]` are:

| Field | Meaning and units |
| --- | --- |
| `learning_weight`, `first_detected_start` | Fine automatic aggregate weight at `last_detected_end` and first accepted aware UTC start. Weight uses the fixed 90-day half-life. Legacy aggregates recover weights from retained fine archives on the next accepted run. |
| `learning_history` | Latest validated fine upload: `power_entity`, `synthetic`, received `start`/`end` (exclusive), `expected_intervals`, `usable_intervals` and detected candidate count `detected_cycles`. Initial counter baseline and missing/reset intervals are not usable deltas. |
| `hourly_profile`, `hourly_weight` | Separate aggregate with `evidence: hourly_estimate`, raw accepted sample count and recency weight at `hourly_last_end`. Quarter-hour phases uniformly spread each measured hourly kWh. They are estimates, not finer measurements. |
| `hourly_first_start`, `hourly_last_end`, `hourly_synthetic` | First accepted run start, latest accepted completion and whether simulated evidence contributes to the hourly aggregate. |
| `history_import.power_entity`, `phase` | Owning power entity and `discovering`, `processing`, `complete`, `unavailable` or `failed`. Missing `start` means discovery has not found a paired range. |
| `history_import.start`, `end`, `cursor` | Aware hourly UTC initial examined boundary, target boundary and exclusive committed core checkpoint. No cursor means no bounded range yet. Failed work resumes this checkpoint. |
| `history_import.expected_intervals`, `usable_intervals`, `detected_cycles` | Cumulative core hours examined, usable paired nonreset hourly deltas and accepted hourly cycles. Retries and read overlap never add to these totals twice. Missing hours count only toward examined hours. |
| `history_import.last_batch` | Last committed core `start`, exclusive `end`, expected/usable interval counts and accepted cycle count, excluding detector overlap. |

Prefix archive reads remain namespace-bound and reuse an active transaction,
including its uncommitted rows. Numeric measurement faults are excluded from
usable-interval counts, not written as nonfinite JSON or admitted into profiles.

`learning_history` and `history_import` are stored only in the mutable progress
document, not in the current state pointer or immutable state versions. Model
and task changes retain their normal immutable versions. Progress, accepted
cycle archives and any changed state pointer commit in the same SQL transaction.
Failure rolls back every participating document before live state changes.
Backend transfer copies progress with the rest of the planning namespace.

Progress and inactive hourly candidates do not change `appliance_revision`.
Activating/replacing the effective profile does. API-only `history_request` projects
the next seven-day core from the cursor, together with app-owned `read_start` and
`read_end` including detector overlap. `learning_half_life_days`, `profile_kind`
and `learning_status` are projections, not additional persisted policies. Historical
planning uses `provisional` status. Fine readiness still requires two fine cycles.
The hourly profile activates after initial completion only without a ready fine
model or manual fallback. New models never rewrite frozen accepted tasks.

See [automatic appliance learning](APPLIANCE-LEARNING.md). Task profiles are copied
at submission. Later learning cannot change acknowledged work. An attempt has a
UUID, status, planned/actual start, issue/expiry time and optional actual completion.
Events contain attempt ID, outcome, aware observation time and cancellation assurance.
Settings, task changes, cycle/request evidence and the current state pointer commit
in one SQLAlchemy transaction before live state changes. Unknown execution survives
restart. All these documents transfer with the existing all-table backend migration.
The source database is not needed afterward. No automatic retention cleanup is added.

Plan snapshots add `appliance_revision`, exact `appliance_evidence` baseline inputs
and immutable `problem.appliances` (task ID/entity, window, profile and optional fixed
actual start). The revision excludes plan publication IDs/timestamps so ordinary
publication cannot itself trigger another solve. Refinement preserves the original
`demand_evidence` forecast/configuration references. Derived residual loads belong
to `problem.intervals`. `appliance_evidence` retains copies of the input baseline
forecasts used to calculate them, not the resulting interval loads or a fabricated
forecast archive ID.
Original plan output retains
`appliance_schedule`, chosen start/end, complete cycle kWh and exact interval kWh.
These are logical references to original plans/tasks and are never recomputed in
place. Technical credentials and physical control configuration remain outside
these app-owned documents, as before.

Appliance planning cancellation retains the original `attempt` and execution
events, clears task schedule/plan linkage and sets task `status` to `cancelled`.
Later matching observations may update execution evidence without changing that
closed planning status. Immutable transition versions retain the prior schedule.
No physical stopped/completed measurement is manufactured by cancellation.

Split HA inputs additionally retain `finish_date` (local ISO calendar date),
`finish_time` (local ISO clock time, without offset), and `finish_time_zone` (IANA
HA timezone). `finish_before` is the resulting aware timestamp or null while the
draft is incomplete/invalid. These fields never change accepted tasks or planner
revision. Readiness still requires a valid future deadline in the supported window.

`appliance-feedback-<sha256>.json` is an immutable complete snapshot of a cancelled
historical task after late correlated physical feedback. Its canonical
`appliance-task-<uuid>.json` is updated in the same transaction. The current task
pointer/revision are unchanged, so late evidence cannot affect replacement work.


Contract calendar dates are a transport/UI convenience only: stored period and
bonus `effective_from`/`effective_until` values remain canonical timezone-aware
instants. The HA installation supplies the conversion timezone. Changing a bonus
clock through HA creates a new contract revision rather than mutating stored plan
inputs. Monetary presentation rounds to two decimals, without changing numeric
storage or calculation precision. No table, field or backend migration is added.

Contract bonus JSON stores optional `price_condition` as null or `{expression, operator, threshold}`. Threshold is a decimal string, expression is bounded arithmetic text and operator is one of the six stable comparison identifiers documented in BONUS-RULES.md. ContractStore owns validation and immutable content identity. No SQL columns or relationships change. The shared document table and all-backend transfer include this payload.

Price-period JSON `effective_until` is a derived exclusive boundary equal to the next rule start. The final value is null. ContractSchedule validates continuous coverage and unique increasing starts. The UI exposes only starts, so there is no independently editable expiry or new SQL schema. No price rule applies before the first start.


Contract document JSON can include `periods[].contract_start`, each price rule's timezone-aware original supplier contract start, independent of `periods[].effective_from`. It is not a document-level field. Changed terms keep their supplier start. A supplier change can use a different start and opaque bonus counter identity. Bonus first-period boundaries are clamped to that start, so previous suppliers' exports do not consume the new budget. It is owned by `ContractStore`, used by the editor's anniversary reset, included in new revision identity and transferred with the existing `contracts` namespace across all database backends. Older documents without this optional metadata retain their original content hash. Displaying a confirmation default does not rewrite them. There is no SQL column/index change. Preview drafts and temporary derived bonus baselines are bounded in-memory UI state, not committed configuration or historical evidence, and are never transferred or used for planning.

New browser-created contracts obtain an opaque UUID from the app. The editor does not supply a name as `contract_id`. The initial bonus counter identity is also system-managed. There are no contract-name or rule-name fields/columns in the durable contract document. Existing technical identifiers in immutable versions are retained to keep original references verifiable.

### Original forecast-quality evidence

The same `demand` namespace now owns immutable `quality-day-YYYY-MM-DD.json`
midnight baselines. `day` and `timezone` define the original HA calendar day.
`start` and `end` are its exclusive UTC bounds. `captured_at` is the actual
post-transition capture time and `observed_at` is the last pre-midnight
observation, not a backdated plan timestamp. `context` freezes source bindings,
display names, original comparison names, initial controlled-consumer identities
and `input_snapshot.demand_evidence` / `solar_evidence`. These entries reference
existing immutable forecast/configuration documents and retain `forecast_version`,
`configuration_version`, `model`, `issued_at` and PV `source_ids` where applicable.
They contain no new solver inputs or invented observations. `capture_issue` and
null `context` explicitly represent an unobserved midnight. `version` hashes the
record. A missing day record is absent evidence, never a zero-energy day.

`context.expected_sources` retains configured source IDs and display titles so
missing household, component or PV forecasts remain explicit even when another
family had usable midnight evidence.

`quality-days.json` is the descending retained date index. Baseline and index
commit atomically through the existing namespace/name primary key. Day listing
returns at most 20 dates per page and does not prune history. Controlled-consumer
eligibility reuses the existing atomic activation history and immutable original
plan fields. Publication time assigns controls to the day, including the plan
already active at midnight. No second eligibility write can fail after publication.
Read failure withholds the comparison. Daily totals, errors and
scores are derived read results in kWh or percent, not stored as measurements.
These documents participate in all four backend persistence/migration contracts
without new SQL columns, tables, indexes, relationships or external stores.

The existing `demand` document namespace additionally owns `quality-history-<binding-sha256>-YYYY-MM-DD.json` daily pointers. A pointer contains `block`, `received_at` and `request`. The binding hashes `entity_id`, HA registry `device_id` and source `unit`. `quality-block-<sha256>.json` is immutable and contains `entity_id` plus `hours`, tuples of UTC hour start, cumulative normalized kWh and meter kWh. `quality-receipt-<sha256>.json` retains each admitted pointer revision. Requests retain source, UTC start/end, configuration binding and request version. Daily addressing uses the existing namespace/name primary key. No global listing or new SQL index is required.

The same namespace owns immutable `quality-components-<household-forecast-version>.json` references to the same-issue individual composite forecast archive. This preserves component evidence when a different household model was selected. Existing forecast documents remain the prediction authority.

Original plan JSON adds `forecast_quality_sources`, `forecast_quality_names` and `forecast_quality_controlled`. These freeze metering identities, bounded display names and controlled consumer entity IDs at publication. Optional `display_sources` maps stable planning device/source IDs to HA entity IDs for presentation. Daily forecast contexts retain the same mapping for their solar sources. Historical views use these frozen links, or existing `comparison_bindings`, to overlay current Energy names in response copies. Forecast-quality views also resolve source identities from the retained original forecast configuration, including older daily captures. Removed sources retain archived names. These labels and links are outside the original solver input hash. No existing archived document is rewritten for a rename. Reads use original forecast/configuration versions. Missing bindings in older plans remain explicitly unavailable. Values use kWh, UTC timestamps and registered HA units. These documents and plan fields transfer with every table through the existing cross-backend transfer. They introduce no separate database, credentials, executable models or source-backend dependency. See [Forecast quality](FORECAST-QUALITY.md) for admission, corrections, eligibility and query bounds.

The `solar` namespace additionally retains immutable `quality-source-models-<forecast-version>.json` maps from source ID to the actual predictor used at forecast creation. A mixed calibrated/physical PV forecast therefore retains the correct model per roof independently of later model selection. These maps use the same namespace/name key and shared cross-backend transfer.

Forecast-quality reads snapshot the bounded daily-pointer set in one SQL statement through `SqlDocuments.read_many`. Referenced blocks are immutable, so a concurrent Recorder baseline correction cannot mix old and new cumulative endpoints across midnight. This avoids holding the app's planning lock while rendering a quality page. Missing pointers remain absent. The existing namespace/name key supports the bounded lookup on all four backends.

### Counter-free physical checkpoint

The `savings` document `physical-reference.json` persists one chronological physical
continuation independently of evaluation revisions. `end` is the ISO interval end.
`reference_stored` and `actual_stored` map battery IDs to kWh. `capacities` maps the
same IDs to usable capacity in kWh. Original measurement frames and activation
chronology establish its provenance. It has no contract foreign key or bonus budget.
Existing successful continuation supplies its initial independent stock once.
Each validated measured frame advances it atomically under the settlement lock.
Financial corrections cannot rewind it. Missing physical intervals cannot be skipped.
The normal document archive and backend transfer include it on SQLite, PostgreSQL,
MariaDB and MySQL. No additional table or database connection is introduced.

### Historical bonus counter documents

The `documents` table additionally permits the `savings-bonus-counters` namespace.
`CounterStore` owns immutable `<version>.json` books and the transactional
`current.json` pointer using the existing `(namespace, name)` primary key.
Books retain `recorded_at` and `seeds`. Each seed stores `rule_id`, `period_start`,
`used_kwh`, `meter_kwh`, `entity_id`, `observed_at` and `component_meters`.
Energy values are kWh. Times are aware UTC instants. The rule/period/source link
is logical, validated against the current contract and frozen measurement
bindings rather than a SQL foreign key. Component readings sum to the aggregate
meter. These automatically derived historical anchors initialize old settlement
intervals. They never replace confirmed planning books in `bonus-counters`.
Backdated anchors and their provenance are also retained in immutable settlement
results as `counter_seeds`. No new table, column or index is needed. Complete
backend transfer includes both namespaces, their immutable books and pointers.
The active historical book retains up to the shared 64-seed limit. New requested
periods always survive admission, including older periods. Remaining slots retain
the most recently observed other periods. Replaced/evicted immutable books remain
archived. This bounded cache does not prune current planning confirmations.


### Bonus opening origin documents

Both `bonus-counters` and `savings-bonus-counters` may additionally contain immutable
`<seed-digest>.origin.json` records owned by `CounterStore`. These use the existing
`oep_documents` primary key and active SQLAlchemy engine. There is no SQL schema or
index change. Complete database-backend transfer includes these documents.

The digest identifies normalized exact seed content. `seed` repeats that identity
for validation. `source` is `home_assistant_history` or `manual_confirmation` and
`recorded_at` is an aware timestamp. A missing or unreadable origin is unknown.
It never changes the seed, counter book hash, original plan or calculation result.
Original documents and previously recorded origins are not rewritten.

Automatic origin fields are `history_start`, `history_end`, `anchor_hour` (aware
instants), `hourly_rows` (count including the pre-boundary anchor),
`start_sum_kwh`/`end_sum_kwh` (cumulative reset-aware HA statistics),
`request_version` and `history_digest` (SHA256 references). Their difference
reproduces the seed's used export kWh. Source meter IDs, physical cumulative
readings, period start and opening measurement date remain in the attached seed.
The metadata contains summary evidence, not all transferred hourly rows. A digest
supports content comparison and is not independent proof of provider authenticity.
Seed, origin and pointer writes share one document transaction on all backends.

## Additional identified price comparisons

The original `oep_price_pairs` and `oep_price_decisions` retain the initial
EnergyPriceForecast/Epex history. Additional pairs have explicit source identities.
Both tables belong to the active engine and every verified backend transfer.
There are no database foreign keys to immutable normalized price documents.
Version fields are logical references into the `prices` document namespace.

### `oep_price_comparison_pairs`

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `area` | String(32), PK | Market bidding area. |
| `bucket` | Integer, PK | Receipt-to-delivery lead bucket, zero based. |
| `cohort` | String(40), PK | UTC collection hour of the first matched original receipt. |
| `target` | String(40), PK | UTC delivery-hour start, used for scoring and joining published truth. |
| `primary_price` | Double | Original complete-hour primary forecast in EUR/kWh. |
| `alternative_price` | Double | Original complete-hour challenger forecast in EUR/kWh. |
| `primary_version` | String(64) | Immutable primary normalized source version. |
| `alternative_version` | String(64) | Immutable alternative normalized source version. |
| `primary_source` | String(128), PK | Explicit ordered primary provider identity. |
| `alternative_source` | String(128), PK | Explicit ordered alternative provider identity. |

The composite primary key prevents another receipt from replacing an original
pair. `oep_comparison_targets` indexes area, primary source, alternative source,
bucket and target. Equality predicates precede the bounded target range.
The shared `oep_price_actuals` key `(area, target)` supports the truth join.

### `oep_price_comparison_decisions`

| Field | Type / key | Meaning and use |
| --- | --- | --- |
| `area` | String(32), PK | Market bidding area. |
| `bucket` | Integer, PK | Independent forecast lead bucket. |
| `day` | String(40), PK | Completed daily assessment date in UTC. |
| `preferred` | String(128) | Pair-local statistical preference after assessment. |
| `challenger` | String(128), nullable | Pending pair-local challenger. |
| `streak` | Integer | Consecutive qualifying UTC daily assessments. |
| `primary_mae` | Double, nullable | Primary mean absolute error in EUR/kWh. Null without matched evidence. |
| `alternative_mae` | Double, nullable | Alternative error on exactly the same samples in EUR/kWh. |
| `hours` | Integer | Number of equally weighted matched delivery hours. |
| `days` | Integer | Number of distinct settled delivery dates. |
| `reason` | String(64) | Stable policy outcome code. |
| `primary_source` | String(128), PK | Ordered primary source identity for this assessment. |
| `alternative_source` | String(128), PK | Ordered alternative source identity for this assessment. |

`oep_comparison_latest` indexes area, primary source, alternative source, bucket
and day. The latest-decision lookup uses equality filters and descending day with
limit one. Retention is indefinite. Scoring still reads only the completed
30-day window and does not rewrite historical assessments.

### Price selection document contracts

Within `oep_documents`, namespace `prices`, `SourceSelection` owns
`source-selection-<area>.json` and daily
`source-selection-<area>-<UTC-date>.json` snapshots. They map lead-bucket strings
to global automatic decisions, including preferred source, challenger, streak,
assessment date, reason, assessment-pair source identities and available matched
scores. Those identities travel with the scores, including retained preferences.
Pair-local decisions alone
do not establish the global recommendation among three sources.

`source-preferences-<area>.json` stores `preferences`, a three-element array of
source IDs or null for Automatic, and `version`, a SHA-256 digest of area plus
preferences. `source-preferences-<version>.json` preserves the immutable revision.
Registered source IDs that are no longer market-eligible remain unchanged in
these documents and their digest. Runtime treats them as Automatic until the
source is eligible again or the user explicitly saves a replacement. Restoring
does not rewrite the pointer or immutable revision. New choices must be
market-eligible and support the selected window's horizon. A save may retain an
ineligible value only unchanged in its original bucket. Effective API preferences
remain distinct from exact stored preferences, so editing another bucket cannot
implicitly clear an inactive choice. Selecting Automatic explicitly clears it.
Optimistic saves check the current durable revision inside the transaction.
Runtime manual preferences change only after commit. Normalized price documents,
the existing `current.json` list, old hashes and historical plans are unchanged.


### Monthly bonus window documents

The `contracts` namespace in `oep_documents` additionally accepts bonus JSON with
`window: "monthly_schedule"` and `monthly_schedule`, an ordered twelve-element
January-to-December array. Each entry is null (disabled) or exactly
`{start_time: "HH:MM", end_time: "HH:MM"}`. Endpoints use the contract timezone,
include the start and exclude the end. An overnight range belongs to its opening
date even across month/year boundaries. Other strategies omit this array.
`ContractStore` validates and versions this immutable configuration. The shared
window resolver compiles it into exact billing edges and payment eligibility.
The existing SQL document table owns persistence and all-backend transfer.
There are no new SQL tables, columns, indexes, relationships or migrations.
Existing counters, identities, historical plans and archived bytes are retained.

The private `archive-backend.json` bootstrap binding is written by
`storage/adapters/lifecycle/files.py` during retained repository startup.
It is technical connection identity outside the active database. Moving this
writer changes ownership only and preserves its atomic replacement and private permissions.

## Read-only latest-decision ownership

`prices/storage/adapters/queries/selection.py` reads the latest decision for each
lead bucket through an existing SQLAlchemy connection. It preserves area and
provider-identity isolation, descending decision-day ordering and a one-row
limit per bucket. `SqlAlchemySelectionStore` retains initialization, index repair,
atomic ingestion and decision writes. This ownership change does not alter any
table, column, type, index, row representation, unit or archived JSON contract.
The functional migration suite remains responsible for transfer of every table.


### Planning calculation history

`CalculationHistory` in `optimization/performance.py` owns `planning` keys `calculations-YYYY-MM-DD.json` in `oep_documents`. The date is the UTC start date. Each atomically appended array row stores opaque `id`, aware `started_at` and `finished_at`, whole-round `duration_seconds`, nullable actual `time_limit_seconds`, original `snapshot_id`, `strategy`, `intervals` count, publication `reason`, nullable native `stop_reason`, published `plan_id` and contract `currency`. No credentials or raw provider failures enter the document.

`calculations-<UTC-date>.json` arrays retain summary rows without `progress`. Each separate `calculation-<id>.json` document retains the full observation, including its bounded `progress` curve. Both writes share one transaction. The authenticated daily endpoint returns summaries. The detail endpoint loads one record by validated opaque ID when requested.

Nullable `build_seconds`, `solve_seconds` and `financial_started_seconds` are worker-relative durations. Nullable `worker_start_seconds` aligns the worker with the coordinator. Nullable `first_candidate_seconds` is whole-round elapsed time and requires an observed finite incumbent during final financial search, including a retained service-stage seed. `progress` contains up to 256 observations with whole-round `elapsed_seconds`, finite currency-valued `objective` and nullable finite `best_bound`. Service priorities are already fixed. Native feasibility is distinct from publication. No old archives are rewritten or backfilled.

The existing composite primary key supports direct daily lookup. Local days read adjacent UTC partitions and filter aware starts. No new table, index or foreign key is introduced. `plan_id` is descriptive evidence. Shared database transfer retains and verifies these documents with all other data. Restart and source-independent transfer regressions use the shared SQLite, PostgreSQL, MariaDB and MySQL matrices.

Retention is explicit: completed calculation evidence remains in the active database alongside original plans, without automatic deletion. This preserves requested historical day lookup. There is no additional retention setting. Separating curves from the index bounds ordinary daily response/write size by summary count rather than sample count. Database transfer preserves both the index and individual observations.


Optional calculation recording uses one serialized background writer. The coordinator only captures a detached summary and at most 256 samples, then enqueues without waiting for SQL. Up to 32 pending observations bound memory. Queue saturation logs an unrecorded observation rather than delaying planning or fabricating a history row. Individual write failures are contained and later writes continue. Normal shutdown stops planning, drains queued observations and only then closes the shared archive engine. Forced termination or storage failure cannot guarantee retained diagnostics. This changes no device command, solver budget or existing history.
