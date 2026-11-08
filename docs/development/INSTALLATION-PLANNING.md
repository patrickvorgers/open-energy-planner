# Installation configuration and live planning

The installation supplies the physical planning basis. A live app
does not need an injected synthetic/test installation to join its independent
prices, dated contract rules, demand forecast, PV forecast and measured storage
states. Hardware control and physical safety remain in user-owned HA automations.
For the setup sequence and visible controls, use the [user installation guide](../users/installation.md). This reference defines ownership and runtime contracts. It is not a declaration of a stable product release.

## Setup and ownership

Install the HA integration once and connect it to the app. Its Configure flow
and virtual-device configuration link open the app's `/setup` interface. The
integration has no installation, source-selection, battery or PV wizard.

The intended public onboarding uses a one-time HACS integration installation.
Public HACS distribution is not available in the development preview. Install a
matched app/integration pair through the [installation acceptance route](HAOS-ACCEPTANCE.md).
On HA OS,
Supervisor performs authenticated discovery and connection, while HA ingress
provides browser authentication. No copied address, token or pairing approval is
needed. Standalone containers use local discovery with explicit consent and a
manual networking fallback. The app never installs integration files or needs
writable HA configuration access. All HA data travels through the integration.
The app does not require a HA user token. Internal credentials remain separate
from browser authentication.

The English responsive app wizard collects connection/household settings, solar
properties for the solar sources and batteries detected in Energy, then previews the complete definition before saving.
Confirm aggregate import/export limits in kW. Annual whole-home consumption is estimated from recent complete Energy history, reused from saved settings, or requested in kWh/year only if history is insufficient. These are not inferred from fuse currents or phase allocation. Zero PV
sources and zero batteries are valid. Each battery keeps its own SoC/power
sensors, capacity, limits and standalone/hybrid connection. Existing definitions
open in the same wizard for later editing, preserving stable device identities.
An interrupted save reloads the independent stores' current revisions on retry.

The app serves static HTML/CSS/JavaScript without another frontend service. The
shell contains no installation data. Direct standalone access authenticates data
and discovery APIs with the app administration token, held only in page memory,
never in links or browser storage. HA OS ingress uses the existing HA session
instead. Companion/HTTPS reachability and final public distribution retain their
separate acceptance gates. The app never installs the HA integration itself.

The integration pushes bounded sensor identity/name/unit/device metadata and
validated Energy suggestions to `/v1/ha/catalogue` at initialization and after
relevant events. Explicit refresh is handled on the next normal synchronization.
The wizard can reload saved settings/suggestions. No current state values, arbitrary attributes, credentials or full Recorder history are copied into this catalogue. A bounded history summary contains exactly two shared UTC cumulative-sum endpoints for every Energy balance meter, converted by HA to kWh. The endpoints bound the widest common period within the last 365 complete UTC days. It is memory-only setup assistance, not planning evidence. Missing/stale discovery blocks the setup wizard until the integration reconnects. Missing or incomplete Energy preferences show an actionable HA setup message. There is no manual entity-ID or parallel household-balance editor. A transient metadata refresh failure retains the last confirmed Energy membership and does not revoke an otherwise valid plan. Production startup waits for its first Energy discovery. A confirmed membership/balance change blocks incompatible plans immediately.
The authenticated catalogue route retains a dedicated 1 MiB request limit. Complete active discoveries use ordered bounded pages with typed validation and aggregate transfer budgets. There are no entity-count or configured-device cutoffs. See [event-driven discovery and complete transfers](ENERGY-DASHBOARD.md#event-driven-source-discovery) for active watches, temporary editor candidates and freshness confirmation.

The HA time zone is reused automatically. Every requested field has explanatory help. Sensor dropdowns use only the connected instance, filtered by device class, supported units and cumulative energy state class. Unambiguous detected links are summarized with a change action, not requested again.

For a new standalone battery, the editor initially suggests its largest terminal
power rating as the AC converter rating, 96% converter efficiency and 95% cell
efficiency in each direction. These are explicit model defaults, not inferred
equipment specifications. Confirm the AC rating/efficiency and retain or edit
the advanced cell/DC settings through the app configuration API as appropriate.

The integration suggests Energy dashboard meters and only unambiguous matching
power/SoC sensors from the same registered HA device. It does not change Energy
preferences or create measurement helpers. The complete Energy balance supplies household history automatically: import + PV + battery discharge - export - battery charge. Use each channel's end sum minus its start sum over the same common UTC period within the last 365 days. Two genuine endpoints per meter are sufficient. References use Recorder's completed-hour end times. A record's start timestamp is not the time of its cumulative sum. Intermediate rows are not transmitted, and corrections inside the period are included in the net difference. A complete year supplies measured annual consumption, avoiding a recent winter or summer sample when annual history exists. With a shorter period, divide its total by the actual elapsed days and multiply by 365. Label that extrapolation and its possible seasonal bias explicitly. Missing or mismatched references, negative period deltas and non-finite results remain unavailable instead of becoming zero use. A one-UTC-day reference grace keeps a fresh cached year estimate stable across midnight until the next catalogue push. The consumption period itself still cannot exceed 365 days, and normal catalogue freshness checks remain in force. Fewer than seven usable days require a saved annual estimate or a user estimate. Empty history is never zero consumption. The integration first probes only the two year boundaries. If either boundary is unavailable, it searches bounded Recorder history for the widest shared reference period and still forwards only two endpoints per meter. Both measured and extrapolated totals supply the startup scale, not learned-model evidence. Normal detailed demand training remains the separate existing post-save history workflow.

Validate boundary probes and bounded Recorder fallback against representative meter counts and retained history. Drain Recorder before measuring completed read cost. Keep synthetic history generation separate from lookup timing. A timeout waiting for a result does not cancel executing SQL.

The app owns persisted settings and derives the corresponding measurement roles.
Battery roles are `battery.<device_id>.soc` and `.power`. Every configured battery
requires both. An installation without batteries has no mandatory battery source.
Source quantities use W, kWh and percent. Grid import and battery discharge are
positive. Optional inversion applies only to measured power. Commands use whole
discharge-positive watts at the battery DC terminal.

Installation changes use a compare-and-set version, an immutable archived
definition and an atomic active-settings reference. A matching measurement
generation is installed after persistence. Restart re-derives that generation
from authoritative installation settings. Separate demand and PV definitions
retain their own versions and commits. The wizard is not a database transaction
across all three stores. Its read-only preview rejects invalid installation
ratings/links before it writes the independent forecast definitions. New plans
require a complete coherent combination, not a partially configured installation.
The legacy source editor is blocked once installation settings own the roles.

## Modules and responsibilities

| Module | Responsibility |
|---|---|
| `installation/model.py` | Immutable physical settings, linked battery states, per-source DC inputs and plain flow records |
| `installation/storage/` | Strict serialization, immutable definitions and active settings pointer |
| `installation/basis.py` | Compose existing cached forecasts and measurements into a physical planning problem |
| `installation/api/` | Authenticated settings/preview API and configured capability catalogue |
| `optimization/adapters/highs/installation_components.py` | Separate grid, PV, battery and inverter native model components |
| `optimization/adapters/highs/installation.py` | Compose those components and existing exact aggregate-export bonuses. Detach results |
| `installation/validation.py` | Independent physical and financial arithmetic, without native solver imports |
| `installation/commands.py` | Convert validated per-device outcomes to complete wire plans |
| `installation/api/setup.py`, `home_assistant/setup_catalogue.py`, `frontend/` and `web/` | App-owned setup, complete preview and responsive wizard |
| `installation/calculation/annual_estimate.py` | Validate compact common-day history and estimate provisional annual household scale |
| HA `setup_history.py` | Read shared UTC start/end sums through Recorder statistics helpers |
| HA `catalogue.py` | Read-only bounded HA sensor metadata and Energy suggestions |
| HA `device_sensors.py` | Reconcile configured virtual entities and expose only current proposed targets |

These extend the existing direct HiGHS adapter and solver lifecycle, not a second
solver backend. Components are stateless. Variables and endpoint bindings belong
to one model context. Cross-scenario binding reuse is rejected. Forecasting,
transport, persistence, physical constraints and settlement do not share native
solver/provider/HA objects.

## AC/DC model

Each converter has a local DC bus behind the shared household AC bus. PV retains
canonical available DC per source. Each battery retains its own internal energy,
reserve, terminal ratings, cell efficiencies and optional cycle fee. The latter
defaults to zero and is proportional to equivalent full internal discharges.
An optional battery `dc_efficiency` describes DC-bus-to-terminal conversion in
both directions, default one. It is separate from cell and AC-inverter losses.

For one inverter and interval, all quantities below are kWh:

`PV_used + sum(battery_discharge * dc_efficiency) + AC_import * inverter_efficiency`

`= sum(battery_charge / dc_efficiency) + AC_export / inverter_efficiency`

Direct PV-to-battery flow remains on that local DC bus. It consumes no AC
throughput and incurs no artificial AC round trip. Standalone PV and storage
cross their own converters. Multiple hybrids retain their own local connections.
The grid balance joins all converter AC ports and household demand, enforcing
the aggregate connection ratings. Opposite grid flows and opposite flows on
one converter are mutually exclusive. Different converters may exchange energy
through the common AC bus.

Curtailment permission exposes an on/off proposal per PV source. Without that
permission a source remains enabled, including at negative export prices.
Physical clipping is separate: reduced enabled-source production requires a
saturated converter AC output and, for each connected battery, full storage or
its maximum charging rate. The planner therefore cannot invent a free PV shutdown
by calling it clipping. Tests distinguish full, partially full and empty storage.

A configured export cap does not imply that fixed PV can be proportionally
limited by equipment. If that cap is below the combined fixed-PV inverter AC
ratings, the API reports `fixed_pv_export_limit` and HA issues an actionable
warning. Sunny intervals may then be infeasible when load/storage cannot absorb
the excess. This conservative warning does not claim every interval is
infeasible. Confirm equipment ratings and on/off permissions. Automatic local
export limitation is not silently assumed, including with a zero export cap.

Battery power is an integer variable in the solver, not rounded after solving.
Finer billing segments of one control interval retain equal battery terminal
power and equal PV enable commands. The independent validator checks every bus,
conversion, battery energy transition, rating, reserve, control group, clipping
condition and whole-watt target. It independently settles chronological bonus
quota/counters and all financial totals before publication.

## Forecasts, provenance and commands

The basis compiler uses the existing cached demand and physical PV predictions,
and requires their current configuration, time coverage and location to match.
It clips a partial current interval at now and uses only common future coverage.
Interior gaps remain errors. Price/contract projection scales every per-source
PV quantity when billing boundaries split intervals. Original forecast and
configuration versions remain in archived planning input. Neither active learned
models nor historical plans are silently recomputed by this change.

All executable plans use wire schema 2. The integration rejects schema 1 and requires complete per-device commands. Synthetic development planning uses the same installation model. Historical documents remain unchanged in the archive. A complete schema-2
plan contains the same configured device/kind set at every step. The HA integration
also checks it against the app's catalogue before accepting the plan.

Each battery gets its own virtual power target. Only controllable PV sources get
an enabled-target entity. Configured devices remain visible before a plan exists,
with unavailable command values. Pause, expiry and core input failure revoke
targets. Zero is a valid planned exchange. Withdrawing a capability removes the
entity and an empty virtual device, and produces an actionable notification to
check automations. Stable retained device IDs preserve existing entity identities.
HA automations translate these proposals to actual equipment. No driver or
automation generation is included.

The authenticated local API adds `GET/PUT /v1/installation` and read-only
`POST /v1/installation/preview`. Settings reject unknown fields and contradictory
converter copies/links. Current settings, issues and the capability catalogue
are included in `/v1/snapshot`. Responses carrying whole multi-device plans have
a bounded 4 MiB client limit. Ordinary settings/history endpoints retain their
existing smaller bounds. No API key, MQTT broker or direct HA database access is
added.

## Remaining product boundaries

- Users still configure the existing contract rules and market area. The full
  built-in contract/region/discovery UI remains separate work. Production price
  retrieval uses the existing app composition (`--market-area`). Tests substitute
  only external normalized prices/weather, never the physical installation basis.
- Learned-model promotion requires independent chronological decision evidence.
  [Both strategies](CONSUMPTION-STRATEGY.md) and [continuous reference/savings](SAVINGS.md)
  use the configured installation. [Automatic bounded-average terminal valuation](TERMINAL-VALUATION.md)
  remains separate from grid cash and wear and retains method/window/rate/volume evidence.
  These calculations do not establish forecast superiority or complete release admission.
- Measured energy below reserve remains usable without clamping. Optional
  [immediate reserve recovery](RESERVE-RECOVERY.md) precedes financial optimization
  within all physical limits. Further discharge below reserve is forbidden.
- Installation settings and historical plans use the active SQLAlchemy database
  with SQLite, PostgreSQL, MariaDB and MySQL contracts and backend transfer tests.
  Production backup/restore, deployment storage sizing and lossless compaction
  remain separate acceptance work.
- Phase protection and fast zero-grid physical regulation remain outside this
  planner. The app wizard is exercised in desktop/mobile Chromium against
  actual app APIs and stores. Actual HA tests cover discovery, history and device
  projection. No physical-device verification is claimed. ARM and distributable release packaging remain separate gates.

Use the [coverage contracts](../../design/TEST-COVERAGE.md) and maintained installation, HA and browser suites to validate changes. Record each execution result in its CI run or PR.


## Test against your own Home Assistant instance

Use the [real Supervisor developer environment](DEVELOPER-ENVIRONMENT.md) for development and automated acceptance. Product releases retain the optional [Home Assistant installation guide](HAOS-ACCEPTANCE.md). Supervisor establishes the connection and HA ingress opens the app without copied addresses or tokens. The integration reads Energy configuration, suitable entities and history. The app owns the setup wizard. Do not overwrite existing Energy preferences to make a test pass.

For standalone Container installations, use the authenticated discovery flow described in [connection discovery](AUTOMATIC-PAIRING.md). The former manual HTTP/token recipe is superseded. The Docker developer environment runs actual Supervisor with simulated measurements. It is not full HAOS hardware evidence.

Neither setup nor integration calls physical equipment services. Leave physical automations disconnected when inspecting plans. Configure required price/contract inputs before expecting a live plan. Missing product inputs must not be replaced by unlabelled synthetic data.

## Energy configuration and missing evidence

The HA Energy dashboard is authoritative for configured grid, PV and battery channels. Discovery retains these identities even when an entity has no current state or Recorder has no history. A structurally complete dashboard opens the wizard. It does not prove that measurements are usable. Unit/type checks run separately during preview and live observation. No history means a startup estimate is requested, not that the grid/PV disappear. An absent Energy battery creates no battery or SoC requirement.

Grid and battery power use Energy's normalized `stat_rate` first, including HA's generated net-power sensor for inverted or combined measurements. Legacy grid `power` entries and direct `power_config.stat_rate` are supported. Only when Energy contains no power selection does discovery look for an unambiguous sensor on the same physical device. It never chooses an unrelated sensor solely because its unit is W. OEP command sensors are excluded from measurement choices. For an installation with configured tariff counters but no instantaneous power selection, configure the intended power sensor in Energy. Do not infer that counter configuration establishes a power measurement.

The wizard displays the detected source counts separately from history availability and preserves configured sensor IDs in selectors when their live metadata is missing. Such a source is marked missing/incompatible. Preview cannot save a mistyped household balance.

Sources removed from Energy are automatically excluded and archived in a new active settings revision. No manual row removal or restoration is required. Configured sources whose live sensor metadata is temporarily missing remain visible with their Energy identity and a missing/incompatible indication. This is distinct from membership removal. Historical settings and plans remain preserved.


## Authoritative membership and device properties

Add/remove solar sources and storage in Home Assistant Settings > Dashboards > Energy only. The app has no independent add/remove buttons or Energy-meter reassignment selector. Refreshing Energy discovery automatically updates its source rows and live virtual-device catalogue. Existing matched meter identities retain device IDs and physical settings. New meter identities receive stable pending devices (`Setup status: setup_required`). Confirm missing ratings/SoC/power/topology in the app, then save. Ratings are never fabricated from an Energy meter or its history. Source removal automatically archives reconciled solar/storage settings and updates the household balance, without deleting old plans or immutable settings. No confirmation or manual row removal is needed. New sources still require missing ratings/bindings. The planner waits rather than using a partial installation.

New plans and publication require matching Energy solar/storage membership and the complete current household balance. Changes invalidate incompatible commands immediately when discovery arrives. Source/settings API writes cannot bypass the membership check. Integration discovery normally refreshes every five minutes, or on a requested setup refresh. All commands remain unavailable until the reconciled settings and ordinary live inputs are usable. Ordinary sensor-value updates are not membership changes and retain the anti-starvation behavior.

Grid, PV and battery devices expose separate read-only diagnostic sensors for relevant confirmed settings: grid import/export limits. Battery usable capacity, charge/discharge power limits, minimum SoC, efficiencies, wear cost and grid-following permission. PV panel capacity, orientation, tilt and curtailment permission. Inverter AC limit, efficiency and hybrid connection. Selected measurement bindings are separate text sensors. Each property has a stable entity ID and its own meaningful unit (W, kWh, percent, degrees). Stored fractional efficiencies are presented as percent. Permissions are read-only yes/no enum sensors, never switches that could modify the model. Wear cost is explicitly in contract currency per full equivalent cycle, without assuming EUR.

Settings remain readable without a valid plan and describe last confirmed settings, not measured performance. They have no measurement state class and are not energy counters. New or changed values update existing entities without recreation. A Setup status sensor distinguishes `configured` from `setup_required`. It links the Energy meter and does not bundle property values into attributes. Fixed PV has settings sensors but no Enabled target. Missing required configuration produces an actionable HA notification. Only withdrawing an actual command capability triggers the automation-impact notification.


## Readable state labels

The integration keeps canonical lower-case state codes (`not_ready`, `fixed_power`, `setup_required`) unchanged for API consumers, templates, automations and history. Human-readable labels belong to Home Assistant's native entity translations: enum sensors declare a `translation_key` and options. `strings.json` and `translations/en.json` provide explicit English state labels such as **Not ready**, **Fixed power** and **Setup required**. This applies to EMS status, grid/battery control modes, solar enabled targets, setup status and yes/no configuration permissions. Home Assistant owns **Unknown** and **Unavailable**.

Use explicit UI text rather than a Python `replace`/capitalization helper. Labels can evolve independently of public state codes, preserve meaningful wording/acronyms and use HA's existing translation infrastructure without a second formatter. Source/runtime translations must agree and tests must cover every declared option. Do not format sensor IDs, user names, free-text configuration, raw diagnostic attributes or protocol values. Developer Tools, templates and the REST API intentionally show the raw codes. Supported HA views render the labels. No entity ID or unique ID changes and no data migration are required.

## Active plan time

The EMS device exposes **Active plan generated at**, a read-only diagnostic timestamp sensor with the active plan ID as an attribute. It uses the accepted plan's original `generated_at`, never the last poll, state update or start of a schedule step. Home Assistant displays the timestamp in the user's local time zone. A newly accepted plan updates this sensor even when power/mode commands remain identical. Disconnection preserves the timestamp while the cached plan remains executable. Pause, expiry, missing coverage or revocation makes it unavailable. A retained historical plan is not presented as current execution. This identifies the plan behind virtual commands, not proof that physical automations executed them.

## Permanent native test sources

The test fixture reads both legacy grid-power selection and the Energy dashboard's nested `power_config`. On cold start, a selected power sensor without live metadata is recreated as a simulated W measurement. Existing W/kW metadata is retained. This fallback belongs only to the simulation fixture, not production source discovery or measurement validation.

The existing-source simulation fixture permanently adds a second PV source and one simulated battery if absent, retaining existing grid/PV identities, other Energy preferences and older history. New sensors are registered under labelled simulated devices. SoC/power are discovered by the same-device rule. The fixture seeds at least 32 recent days on all balance channels through Recorder, including nonzero solar/charge/discharge history. Daily battery charging at midday and discharging in the evening is included in the conserved grid/household balance. Restart/reload preserves IDs, anchor and baselines. Fixture schema changes reseed only the explicit synthetic interval. No physical service is called. This overlay is restricted to the dedicated test VM, never the product image.

## Reserve recovery

[Reserve recovery](RESERVE-RECOVERY.md) describes the app-owned per-battery policy,
lexicographic native stages, diagnostic statuses, immutable evidence and regression proof.
