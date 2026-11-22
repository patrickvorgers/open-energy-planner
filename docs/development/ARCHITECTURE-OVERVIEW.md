# Architecture overview

OEP consists of a local calculation app and a thin Home Assistant integration. Start here to choose the responsible layer before changing code. Use the [technical design](../../design/TECHNICAL-DESIGN.md) for complete contracts and the [user explanation](../users/how-oep-works.md) for product concepts.

## Application at a glance

The local app connects input sources to validated plans, historical evaluation
and the web interface. The arrows show data flow. The Home Assistant integration
appears twice to show its roles as an input bridge and as the publisher of
current virtual commands. Equipment measurements and execution feedback return
through Home Assistant on subsequent synchronization.

```mermaid
flowchart LR
    accTitle: Open Energy Planner application flow
    accDescr: Home Assistant and external providers supply inputs. The local app builds forecasts, solves a planning problem and validates the result. The published plan feeds both the app's archive and the virtual commands that user automations can use.

    subgraph SOURCES["Input sources"]
        direction TB
        HA["Home Assistant<br/>Energy, entities, Recorder and feedback"]
        BRIDGE["OEP integration<br/>Source and history bridge"]
        PROVIDERS["External providers<br/>Weather and electricity prices"]
        HA --> BRIDGE
    end

    subgraph CORE["Local app forecasting and planning"]
        direction TB
        INPUTS["Admit measurements, prices<br/>and installation settings"]
        MODELS["Forecasts and device models<br/>Solar, demand, appliances, EV and thermal"]
        SNAPSHOT["Freeze a planning problem"]
        SOLVER["Calculate a candidate<br/>in the isolated HiGHS worker"]
        CHECKS["Validate balances, limits and costs<br/>Check source versions and publish"]
        INPUTS --> MODELS --> SNAPSHOT --> SOLVER --> CHECKS
    end

    subgraph REPORTING["Local app storage and interface"]
        direction TB
        ARCHIVE[("Shared database<br/>Configuration, history and plan archives")]
        INSIGHTS["Evaluate savings and forecast quality"]
        UI["Authenticated API and web UI<br/>Configuration, plans, insights and activity"]
        ARCHIVE --> INSIGHTS --> UI
    end

    subgraph EXECUTION["Home Assistant execution"]
        direction TB
        COMMANDS["OEP integration<br/>Current virtual commands"]
        EQUIPMENT["User automations and equipment<br/>Physical control and local safeguards"]
        COMMANDS --> EQUIPMENT
    end

    SOURCES --> CORE
    CORE -->|Validated plan| REPORTING
    CORE -->|Complete plan| EXECUTION

    style CORE fill:#eef5fc,stroke:#1c5a90,color:#172c41
    style REPORTING fill:#eef5fc,stroke:#1c5a90,color:#172c41
    style EXECUTION fill:#ecf8f3,stroke:#237558,color:#173a30
```

## Internal module and data flows

Follow **Live planning inputs** from the input diagrams into the planning round
and **Published plan** from planning to execution and evaluation. Boxes name the
responsible module or service. Arrows show the direction of data and decisions.
App paths are relative to `src/open_energy_planner/`.

- [Home Assistant source discovery and measurements](#home-assistant-source-discovery-and-measurements)
- [Weather, forecasts and learning](#weather-forecasts-and-learning)
- [Market prices and contract rates](#market-prices-and-contract-rates)
- [Consumption and controllable equipment](#consumption-and-controllable-equipment)
- [Planning, validation and publication](#planning-validation-and-publication)
- [Home Assistant execution and feedback](#home-assistant-execution-and-feedback)
- [Historical evaluation, storage and the web UI](#historical-evaluation-storage-and-the-web-ui)

### Home Assistant source discovery and measurements

```mermaid
flowchart TD
    accTitle: Home Assistant source and measurement flow
    accDescr: The HA integration sends source membership, live observations and bounded historical batches to separate app owners. Installation configuration binds sources and gives them a planning role.
    ENERGY["HA Energy and entity registry<br/>Membership, meter identities and location"] --> HA["HA coordinator.py<br/>Synchronize the catalogue, observations and history"]
    STATES["HA entity states<br/>Power, battery SoC and equipment state"] --> HA
    RECORDER["HA Recorder<br/>Statistics and supported raw histories"] --> HA
    HA -->|Catalogue| CATALOGUE["home_assistant/setup_catalogue.py<br/>Admit and expose available sources"]
    HA -->|Current timestamped observations| OBS["home_assistant/observations.py and live_inputs.py<br/>Validate observations against the configured sources"]
    HA -->|History batches| HIST["Domain importers<br/>Normalize and store measured series by source"]
    CATALOGUE --> CONFIG["installation/<br/>Bind meters and configure topology and limits"]
    CONFIG --> OBS
    OBS --> LIVE["Live planning inputs"]
    HIST --> DOWNSTREAM["Forecast learning, appliance cycles<br/>and historical evaluation"]
```

Home Assistant owns source membership. The app owns the planning role of each
source and its electrical constraints. The integration reads Recorder through
HA, then passes normalized data to the app. Missing measurements remain unknown.
See [measurement contracts](HA-MEASUREMENT-SOURCES.md) and
[installation planning](INSTALLATION-PLANNING.md).

### Weather, forecasts and learning

```mermaid
flowchart TD
    accTitle: Weather and forecast module flow
    accDescr: The weather module shares one live provider run and caches daily training temperatures. Solar and demand build forecasts from current models. Forecast Quality uses the originally issued values and weather evidence.
    LOCATION["HA site location"] --> WEATHER
    METEO["Open-Meteo"] --> WEATHER["weather/<br/>Validate, version and archive provider data"]
    WEATHER --> LIVE_RUN["One live IFS run<br/>Weather version and expiry"]
    WEATHER --> HISTORY["Historical weather<br/>Cached temperature days and original forecast runs"]
    LIVE_RUN -->|Irradiance, temperature and wind| PV["solar/<br/>Build and select PV forecasts per source"]
    HAFC["HA solar forecast adapters"] --> PV
    LIVE_RUN -->|Forecast temperature| LOAD["demand/ and appliances/<br/>Build household and consumer forecasts"]
    HISTORY -->|Historical weather and runs| TRAIN["Solar and demand training<br/>Fit models against measured energy"]
    METERS["Measured energy history"] --> TRAIN
    TRAIN -->|PV candidates| PV
    TRAIN -->|Current demand fit per learner| LOAD
    PV -->|Selected PV forecast| LIVE["Live planning inputs"]
    LOAD -->|Selected demand forecast| LIVE
    WEATHER -->|Exact run evidence and satellite adapter| QUALITY["Forecast Quality<br/>Weather context for frozen forecasts"]
```

The app shares one accepted live run for the site, including when demand uses
weather and no PV is configured. Historical training uses completed observation
hours and, for PV backtests, original provider forecast runs. Demand learners
share cached temperature days for the site. Each learner replaces its stored fit
after validated training. The demand archive keeps the original forecast values
and weather reference. Demand can fall back to calendar profiles.
An eligible demand issue is reused as the planner advances within its compatible
quarter-hour coverage. A weather-backed issue retains its original weather
reference and uses that run's deadline for live admission.
See [solar](SOLAR-FORECASTING.md), [demand](DEMAND-FORECASTING.md) and
[model selection](MODEL-SELECTION.md).

### Market prices and contract rates

```mermaid
flowchart TD
    accTitle: Market prices and contract rate assembly
    accDescr: Price adapters normalize published prices and predictions. The price service selects usable coverage and the contract layer applies supplier rules before planning.
    PUBLISHED["Published day-ahead prices<br/>Nord Pool or eligible HA source"] --> ADAPTERS["prices/adapters/<br/>Validate units, intervals and provenance"]
    PREDICTED["Configured market forecast providers"] --> ADAPTERS
    ADAPTERS --> MARKET["prices/service.py<br/>Cache source data and assemble contiguous spot prices"]
    PREF["prices/selection.py<br/>Evaluate forecasts and set daily preferences"] --> MARKET
    MARKET -->|Selected version, native resolution and coverage| CONTRACT["contracts/<br/>Build effective import and export rates"]
    TERMS["Contract configuration<br/>Market area, supplier rules, taxes and bonuses"] --> CONTRACT
    CONTRACT -->|Effective buy and sell rates| LIVE["Live planning inputs"]
    MARKET -->|Source status and original market values| READ["Price API and plan evidence"]
```

Published prices take precedence over forecast prices where they overlap. The
first gap ends usable coverage. Contract rules translate market prices into the
rates the solver uses. A new fetch with unchanged price content and usable
coverage does not by itself require replanning.
See [price inputs](PRICE-INPUTS.md) and [contract prices](CONTRACT-PRICES.md).

### Consumption and controllable equipment

```mermaid
flowchart TD
    accTitle: From consumption and equipment evidence to planning inputs
    accDescr: A household forecast and optional component forecasts feed installation planning. Cycles, EV and thermal modules prepare their own device inputs, with controls gated separately from forecasts.
    BASE["demand/<br/>Household baseline and selected consumer components"] --> BASIS["installation/ and live_inputs.py<br/>Compose residual load, device inputs and topology"]
    HISTORY["Device history and live evidence"] --> CYCLES["appliances/cycles/<br/>Cycle profiles, task windows and execution state"]
    HISTORY --> EV["appliances/ev/<br/>Charging demand, session state and readiness"]
    HISTORY --> THERMAL["appliances/thermal/<br/>Thermal demand, model and eligible flexibility"]
    CYCLES -->|Profiles and eligible tasks| BASIS
    EV -->|Forecast or eligible charging constraints| BASIS
    THERMAL -->|Forecast or eligible thermal constraints| BASIS
    ASSETS["Installation configuration<br/>Grid, inverter, PV and battery limits"] --> BASIS
    RULES["User permissions and device settings"] --> CYCLES
    RULES --> EV
    RULES --> THERMAL
    BASIS --> LIVE["Live planning inputs"]
```

The installation basis keeps modeled device demand and residual household load
consistent. Separately modeled consumption must have compatible source ownership
and coverage before replacing part of the baseline. The appliance modules own
their readiness and task rules. Forecast inclusion alone grants no control.
Cycle scheduling clips the demand series to its window while preserving the
source quarter-hour boundaries within that window.
See [appliance architecture](APPLIANCE-ARCHITECTURE.md) and
[installation planning](INSTALLATION-PLANNING.md).

### Planning, validation and publication

```mermaid
flowchart TD
    accTitle: Planning round, independent validation and publication
    accDescr: Live inputs produce a coherent snapshot. A coordinator serializes planning rounds, a worker solves the problem, and independent validation and publication checks guard the plan.
    LIVE["Live planning inputs"] --> SNAPSHOT["live_inputs.py<br/>Build the problem and freeze its input snapshot"]
    SNAPSHOT --> QUEUE["PlanningCoordinator<br/>Schedule rounds and coalesce updates"]
    QUEUE --> SERVICE["PlanningService<br/>Calculate a candidate using the snapshot"]
    SERVICE --> SOLVE["optimization/adapters/process/<br/>Solve in the isolated native HiGHS worker"]
    SOLVE --> CHECK["installation/validation.py<br/>Recompute balances, limits and financial results"]
    CHECK -->|Valid candidate| MAP["CommandMapper<br/>Build the complete command document"]
    MAP --> PUBLISH["PlanningCoordinator and app publication<br/>Check validity, archive and expose the plan"]
    PUBLISH --> PLAN["Published plan"]
    CHECK -->|Rejected candidate| STATUS["Planning status and diagnostics<br/>Readiness, activity and notification reasons"]
    PUBLISH -->|Expired or invalidated basis| STATUS
```

Ordinary incoming updates are coalesced into the next round while a valid round
can finish. Hard invalidation and expiry can prevent publication. Failure to
publish never extends a previous command's validity. See
[installation planning](INSTALLATION-PLANNING.md) and the
[technical design](../../design/TECHNICAL-DESIGN.md).

### Home Assistant execution and feedback

```mermaid
flowchart TD
    accTitle: Plan synchronization and feedback in Home Assistant
    accDescr: The HA integration checks received plans locally and exposes current virtual commands. User automations control equipment, and execution feedback returns over synchronization.
    PLAN["Published plan<br/>App snapshot API"] --> SYNC["HA coordinator.py<br/>Synchronize and acknowledge a complete plan"]
    SYNC --> SCHEDULE["HA local schedule and validity<br/>Select the applicable interval or withdraw commands"]
    SCHEDULE --> ENTITIES["Virtual devices and entities<br/>Expose current proposals and status"]
    ENTITIES --> AUTOMATION["User-owned HA automations<br/>Translate proposals into device actions"]
    AUTOMATION --> EQUIPMENT["Physical equipment<br/>Apply local control and safeguards"]
    EQUIPMENT --> EVIDENCE["HA states, Recorder and execution feedback"]
    EVIDENCE -->|Observations and supported feedback| SYNC
    SYNC -->|Measurements and execution state| LIVE["App admission and live planning inputs"]
```

The integration checks local validity independently of the app. Expiry and
pause withdraw virtual commands. A receipt proves delivery. Measurements,
claims and execution feedback provide separate evidence of device behavior.
See [virtual devices](VIRTUAL-DEVICES.md).

### Historical evaluation, storage and the web UI

```mermaid
flowchart TD
    accTitle: Plan archives, historical evaluation and web UI
    accDescr: Published plans, configuration and measurements are durable. Forecast Quality and Savings read preserved evidence and expose different comparisons through the app APIs.
    PLAN["Published plan<br/>Original inputs, forecasts, rates and decisions"] --> ARCHIVE[("Shared app SQLAlchemy storage<br/>Plans, configuration, model versions and measurements")]
    HISTORY["HA historical measurements and source bindings"] --> ARCHIVE
    REFS["Forecast services<br/>Forecast references available at local midnight"] --> QUALITY["forecast_quality/<br/>Freeze the daily baseline and compare with actuals"]
    ARCHIVE -->|Forecast archives, measurements and plan activations| QUALITY
    WX["weather/ archive<br/>Original issued irradiance by weather version"] --> QUALITY
    SAT["weather/satellite.py<br/>Independent satellite irradiance"] --> QUALITY
    QUALITY -->|Chronological comparison evidence| PREFS["forecast_selection/<br/>Model preferences for future forecasts"]
    ARCHIVE --> SAVINGS["savings/<br/>Measured operation versus the reference"]
    ARCHIVE --> READ["Authenticated app APIs<br/>Settings, current state, archives and diagnostic status"]
    ARCHIVE -->|Original battery and inverter flows| BATTERY["installation/charge_sources.py<br/>Allocate planned charging sources"]
    BATTERY --> READ
    QUALITY --> READ
    SAVINGS --> READ
    READ --> UI["web/pages/ and web/public/<br/>Planning, Forecast Quality, Savings and settings"]
    UI -->|Validated configuration mutations through API| SETTINGS["Domain configuration owners"]
    SETTINGS --> LIVE["Live planning inputs"]
```

Forecast Quality captures the original forecast for the local day independently
of solver runs and compares it with matched measurements. Its weather view
resolves forecast irradiance from the original weather reference and shows
satellite irradiance separately. Savings compares settled measured operation
with its reference. The shared database also retains the frozen daily baseline,
measurement evidence and settings changes. `app.py` composes these services and their authenticated
routes. See [plan archive](PLAN-ARCHIVE.md),
[Forecast Quality](FORECAST-QUALITY.md), [Savings](SAVINGS.md) and
[database ownership](DATABASE-SCHEMA.md).

## Follow data through the system

| Stage | Owner | Boundary to preserve |
|---|---|---|
| Discover Energy sources and capture observations/history | `custom_components/open_energy_planner/` | Read through HA APIs and normalize units. No native solver or app imports. |
| Admit measurements and construct future inputs | App `home_assistant/`, `installation/`, demand/solar/price owners | Missing evidence remains missing. Provider/framework objects stay inside adapters. |
| Freeze a planning problem | Installation basis and planning use case | Retain source/configuration versions and coherent physical topology. |
| Optimize | `optimization/adapters/highs/` in the isolated native worker | One responsible adapter, bounded work, detached results. |
| Validate independently | Pure physical and financial evaluators | Recompute balances, limits and costs without using solver expressions as their own oracle. |
| Publish and archive | Planning/application lifecycle and shared SQLAlchemy stores | Reject invalidated candidates. A failed round cannot replace or prolong the last valid plan. |
| Expose virtual advice | HA integration entities and events | Complete, current, applicable commands. Receipt is separate from physical execution. |
| Operate equipment | User-owned HA automations and local controllers | Existing physical safeguards and supported claim/feedback protocols remain authoritative. |

There is no physical-device driver hidden in the HTTP UI. The app's static pages call authenticated APIs. Supervisor ingress and standalone administration retain different authentication entry points.

## Keep calculation work outside the interface loop

Snapshot reads capture immutable settlement source definitions under the state
lock, then await historical database I/O outside that lock. The handler reacquires
the lock afterward to refresh validity and read current state. A pause or source
change therefore remains responsive and cannot be hidden by an older captured
screen state. Historical transfers also read outside the state lock and reject
source changes made during validation before admitting a write. Financial
mutations retain their separate serialization lock. Concurrent readers publish
the cached historical plan identity and its bindings as one pair and use their
own local bindings. Cancellation drains the historical reader before its handler
finishes.

Enable and pause responses acknowledge persisted control intent only. They do
not fetch a snapshot or wait for historical readers. The app composition owns
this mutation boundary. The HA client validates the receipt against the exact
boolean, request identity and paired build. The coordinator clears only the
confirmed current intent, never an intent superseded during the exchange or
after shutdown. Local pause withdraws commands before waiting for the HA
synchronization lock. Its confirmation can wait for an in-flight synchronization
to finish, but the control receipt does not start another historical read.
Its next periodic synchronization retains the complete snapshot,
catalogue, observation and history flow. Resume still requires a plan generated
after the local resume boundary, so a receipt cannot revive older commands.

The shared PostgreSQL engine also bounds each native driver I/O wait to thirty
seconds. Server statement timeouts alone cannot bound an answer stalled in
transit. An operational wait failure closes the connection before propagating
the error, so cleanup cannot wait again on that connection. PostgreSQL connection
timeout applies per address attempt, not to DNS or the full multi-address process.
Pool admission and MariaDB/MySQL read/write waits remain separate limits.
A sequence of database operations can take longer than
one wait. Cancellation still drains the worker before releasing mutation
ownership or disposing its engine.

Appliance editors request `/v1/consumers?view=settings`. This projection retains
selection versions, Energy choices and specialized-meter ownership without
transferring model forecasts that these editors do not use. The default consumer
response retains complete forecast evidence for existing clients.

Live HTTP and background refresh owners await `LiveInputs.refresh_async` or
`receive_async` while retaining the application state lock. The event loop first
captures the coherent nonblocking installation and price basis. Price caches
publish on that loop outside the application lock, so their values, versions and
expiry cannot be read across a worker handoff. A thread prepares the appliance,
EV and thermal snapshots from the captured immutable input. Coordinator
invalidation, snapshot installation and submission remain on the event loop.
Cancellation drains the preparation worker before releasing the state lock,
including repeated cancellation. Installation rechecks measurement and future
input expiry after the worker finishes. A closed coordinator discards completed
preparation without submitting more work. The synchronous domain entry points
remain available for isolated callers and startup without observations.

Component coverage and balance checks integrate ordered target windows with a
single sweep through each source series. Gaps, overlaps, duplicate ownership and
replacement limits still reject invalid inputs. Task candidate windows are
computed once per preparation, and boundary lookup does not rescan every device
series for every planning interval.

The native solver runs in its separate process. Independent validation,
consumption evaluation and command-document construction run outside the event
loop. Historical repricing, retention and compression remain background workers.
Repricing reads only hash-checked contract-version leaves before deciding whether
a full original plan needs recalculation. Archive reconstruction reads all value
leaves for each record together and verifies the complete reconstructed hash.

Off-loop execution prevents synchronous interface stalls. It does not reserve
CPU or eliminate contention on small machines. Diagnose input preparation,
native solver phases, publication and archive maintenance separately. A battery
below its reserve invokes the reserve-recovery phase. Comparing those rounds
with rounds above reserve does not establish a solver regression. Preserve
reserve safety, numerical precision and historical evidence when optimizing.

## Keep the important dimensions separate

- **Identity versus configuration:** Energy owns source membership. The app owns equipment ratings, topology and planning settings.
- **Forecast versus control:** Selecting a forecast consumer does not grant scheduling permission or prove readiness.
- **Publication versus execution:** A received plan or virtual setpoint does not establish physical action.
- **Original expectation versus later evaluation:** Repricing and measured history create separate evidence rather than rewriting the original plan.
- **Description versus decision quality:** Forecast-error metrics do not replace the chronological decision gate for automatic model selection.
- **Data versus deployment secrets:** OEP tables share one active database. Connection settings, certificates, tokens and recovery bootstrap files stay outside it.

## Find the source owner

Use the [domain ownership rules](../../design/TECHNICAL-DESIGN.md#domain-ownership)
to decide which module owns a data contract, an OEP endpoint or a provider
adapter.

| Area | Source location | Start with |
|---|---|---|
| Shared planning and publication | `planning.py`, `live_inputs.py`, `planning_policy.py` | [Technical design](../../design/TECHNICAL-DESIGN.md) |
| Physical installation and commands | `installation/` | [Installation planning](INSTALLATION-PLANNING.md) |
| Native optimization and pure models | `optimization/` | [Solver selection](../../design/SOLVER-SELECTION.md) and [independent scenarios](../../design/PLANNER-SCENARIOS.md) |
| Live and historical weather | `weather/` | [Domain ownership](../../design/TECHNICAL-DESIGN.md#domain-ownership) |
| Demand, solar and forecast quality | `demand/`, `solar/`, `forecast_quality/` | [Reference library](REFERENCE-INDEX.md#forecasts-planning-and-financial-interpretation) |
| Prices, contracts and evaluated savings | `prices/`, `contracts/`, `savings/` | [Contract prices](CONTRACT-PRICES.md) and [Savings](SAVINGS.md) |
| Appliance forecasts, cycles, EV and thermal | `appliances/` | [Shared appliance structure](APPLIANCE-ARCHITECTURE.md) |
| Durable archive and lifecycle | `storage/` and domain storage adapters | [Database dictionary](DATABASE-SCHEMA.md) and [archive](PLAN-ARCHIVE.md) |
| Page rendering and browser behavior | `frontend/`, `web/pages/`, `web/public/` | [Configuration workspaces](CONFIGURATION-UI.md) and [visualization](EMS-VISUALIZATION.md) |
| HA platform integration | `custom_components/open_energy_planner/` | [Measurement contracts](HA-MEASUREMENT-SOURCES.md) and [virtual devices](VIRTUAL-DEVICES.md) |
| Suite selection and proof | `script/ci/` | [CI stages](CI-STAGES.md) |
| Example-data provisioning and Supervisor lifecycle | `script/provisioning/`, `script/supervisor/`, `test_environment/` | [Developer environment](DEVELOPER-ENVIRONMENT.md) |

Paths within the app are relative to `src/open_energy_planner/`. Check actual imports and callers as well as the directory map. Shared composition and lifecycle changes can require broader proof than a domain calculation.

## Preserve storage and test ownership

Every OEP table belongs to the shared schema and active app-owned engine. A new store must participate in the dictionary and real SQLite/PostgreSQL/MariaDB/MySQL transfer contracts. A private domain connection or file-based configuration archive would break this ownership.

The HA solar-feedback adapter may use exactly `from sqlalchemy import select` to construct its bounded native Recorder read. That read uses only Home Assistant's existing read-only session and Recorder schema. It does not create a connection, engine, ORM session or OEP store. Other direct SQLAlchemy imports remain prohibited in the integration. Native Recorder timestamps and normalized observations cross the app boundary, not database objects.

Tests are grouped by behavior and required environment. App-side HA contracts can run without starting HA. Actual integration/Recorder behavior needs real HA tests. Installation and lifecycle changes need their provisioning and Supervisor evidence. Splitting a test file or moving production code does not authorize dropping those contracts.

Before implementing a change, identify the affected sources, consumers, durable formats, API boundaries and test groups. Use [review and CI stages](CI-STAGES.md#change-based-candidate-matrix) to understand the immutable whole-PR matrix. The selector complements semantic review. It does not prove every dynamic dependency.

**Next:** [Prepare a development environment](README.md#first-start), or choose the subsystem in the [reference library](REFERENCE-INDEX.md).
