# Developer reference library

Use this index when changing a subsystem or investigating an implementation contract. For operating the product, start with the [user guide](../index.md). User instructions explain the task. These references explain ownership, invariants, interfaces and verification boundaries.

## Start a contribution

| Document | Use it for |
|---|---|
| [Development](README.md) | First checkout, supported workflows and finding the responsible code |
| [Real Supervisor environment](DEVELOPER-ENVIRONMENT.md) | Accepting a numbered package, host startup, retained data and recovery |
| [Interactive developer scenarios](USER-TEST-ENVIRONMENT.md) | Populated simulated devices, histories and integration exercises |
| [Contributing](../../CONTRIBUTING.md) | Signed commits, focused changes, documentation impact and PR expectations |
| [Review and CI stages](CI-STAGES.md) | Suite selection, provider review, Ready admission and exact-tree merge evidence |
| [Trusted signatures](VERIFIED-COMMITS.md) | Base-only GitHub signature gate and enforcement limits |
| [Joint release identity](RELEASE-IDENTITY.md) | Matching app/integration artifacts and loaded identities |
| [Source provenance](SOURCE-PROVENANCE.md) | Public snapshot boundary and retained provenance |

## Architecture and maintained specifications

| Document | Use it for |
|---|---|
| [Architecture overview](ARCHITECTURE-OVERVIEW.md) | A short map of the app, integration, data and physical-control boundaries |
| [Technical design](../../design/TECHNICAL-DESIGN.md) | Detailed model, protocol, lifecycle and persistence decisions |
| [Requirements](../../design/REQUIREMENTS.md) | Stable requirement identifiers and agreed behavior |
| [First release scope](../../design/FIRST-RELEASE-SCOPE.md) | Product admission gates and explicit exclusions |
| [Development foundation](../../design/DEVELOPMENT-FOUNDATION.md) | Repository responsibilities and initial development contract |
| [Solver selection](../../design/SOLVER-SELECTION.md) | Why the native HiGHS adapter owns optimization |
| [Planner scenarios](../../design/PLANNER-SCENARIOS.md) | Independent numerical inputs and expected results |
| [Solver regression contracts](../../design/SOLVER-REGRESSION-MIGRATION.md) | Preserved expectations and physical semantics |
| [Coverage catalogue](../../design/TEST-COVERAGE.md) | Scenario and test-layer responsibilities |
| [Device settings proposal](../../design/DEVICE-SETTINGS-PROPOSAL.md) | Design rationale and proposed settings, not a current setup tutorial |

Specifications contain both agreed contracts and sections explicitly marked as proposals or remaining work. Preserve those distinctions. An agreement or passing model test is not proof that a feature is publicly released or physically verified.

## Installation, measurements and control

| Technical reference | Related user task |
|---|---|
| [Installation planning](INSTALLATION-PLANNING.md) | [Configure an installation](../users/installation.md) |
| [Energy mapping](ENERGY-DASHBOARD.md) | [Prepare authoritative meters](../users/before-you-start.md#prepare-home-assistant-energy) |
| [HA measurement sources](HA-MEASUREMENT-SOURCES.md) | [Understand inputs](../users/how-oep-works.md) |
| [HA history transport](HA-HISTORY.md) | [Understand measured and missing history](../users/forecasts.md) |
| [Automatic pairing](AUTOMATIC-PAIRING.md) | [Connect Home Assistant](../users/home-assistant.md) |
| [Virtual devices](VIRTUAL-DEVICES.md) | [Keep physical control separate](../users/home-assistant.md#keep-control-separate) |
| [Optional HA installation acceptance](HAOS-ACCEPTANCE.md) | [Install a matched pair](../users/home-assistant.md) |
| [Local API](LOCAL-API.md) | App/integration development rather than manual user configuration |

## Forecasts, planning and financial interpretation

| Technical reference | Responsibility |
|---|---|
| [Demand forecasting](DEMAND-FORECASTING.md) | Household reconstruction, startup/history models and individual consumers |
| [Solar forecasting](SOLAR-FORECASTING.md) | Source geometry, weather, AC/DC boundaries and retained model evidence |
| [Forecast model selection](MODEL-SELECTION.md) | Chronological shadow decisions and conservative model promotion |
| [Forecast quality](FORECAST-QUALITY.md) | Fixed midnight baselines, measurements, exclusions and metrics |
| [Price inputs](PRICE-INPUTS.md) | Published/forecast adapter timing, cache and availability |
| [Price forecast selection](PRICE-FORECAST-SELECTION.md) | Provider eligibility, lead buckets, preference and fallback |
| [Contract prices](CONTRACT-PRICES.md) | Formula language, dated terms, repricing and API |
| [Dutch contract examples](CONTRACT-EXAMPLES-NL.md) | Worked formula/bonus building blocks and exactness limits |
| [Export bonuses](BONUS-RULES.md) | Eligibility, chronological caps and separate measured/planned counters |
| [Bonus statistics](BONUS-STATISTICS.md) | Opening evidence from cumulative Home Assistant statistics |
| [Control grid and settlement](PRICE-TIMELINE-AND-CONTROL-GRID.md) | Physical control intervals versus finer billing segments |
| [Consumption strategy](CONSUMPTION-STRATEGY.md) | Lexicographic import priority and terminal-stock correction |
| [Battery inventory valuation](TERMINAL-VALUATION.md) | Bounded useful-energy value beyond the command horizon |
| [Reserve recovery](RESERVE-RECOVERY.md) | Mandatory recovery precedence and below-reserve measurements |
| [Plan evidence](PLAN-EVIDENCE.md) | Archived inputs, original comparisons and explanation limits |
| [Settlement and savings](SAVINGS.md) | Metered intervals, continuous reference, corrections and evidence quality |

For task-level reading, use [forecasts](../users/forecasts.md), [contract setup](../users/contract.md), [Planning](../users/read-your-plan.md) and [Savings](../users/savings.md).

## Appliances

| Technical reference | Responsibility |
|---|---|
| [Shared appliance structure](APPLIANCE-ARCHITECTURE.md) | Identity, forecast, capability, permission and daily-intent ownership |
| [Shiftable appliances](SHIFTABLE-APPLIANCES.md) | Complete-cycle tasks, claims, feedback and cancellation |
| [Automatic cycle learning](APPLIANCE-LEARNING.md) | Detection, complete profiles, provisional evidence and manual fallback |
| [EV charging](EV-CHARGING.md) | Vehicles, charge points, assignments, departures and execution |
| [Thermal storage](THERMAL-STORAGE.md) | Resistive tank model, comfort priorities and control boundary |

The [user appliance guide](../users/appliances.md) explains where to configure equipment and where to make today's request.

## Storage, interface and maintenance

| Technical reference | Responsibility |
|---|---|
| [Plan archive](PLAN-ARCHIVE.md) | Immutable records, active engine, backup and supported backend transfer |
| [Database dictionary](DATABASE-SCHEMA.md) | Every registered table, column, JSON owner, unit and relationship |
| [Storage migration](STORAGE-MIGRATION-UI.md) | Maintenance admission, copy/verification, release and recovery |
| [Fixed-value ownership](FIXED-VALUES.md) | Central policy constants and persistence boundaries |
| [Configuration workspaces](CONFIGURATION-UI.md) | Editing, SaveBar, partial failures and responsive settings |
| [EMS visualization](EMS-VISUALIZATION.md) | Chart boundaries, scales, interval semantics and accessibility |
| [Courtyard theme](COURTYARD-THEME.md) | Shared identity, app navigation and UI verification |
| [Notifications](PLANNING-NOTIFICATIONS.md) | Actionable explanations, source identity and recovery lifecycle |
| [Activity and module events](ACTIVITY-EVENTS.md) | [Read background activities](../users/activity.md) |
| [Logging](LOGGING.md) | Severity, safe context, timestamps and deduplication |
| [Code documentation](CODE-DOCUMENTATION.md) | Useful docstrings, independent scenario explanations and guide maintenance |
| [Documentation site](DOCUMENTATION-SITE.md) | Canonical Markdown, screenshots, local build and single Pages publication |
| [EMS comparison](EMS-FEATURE-COMPARISON.md) | Research and adoption criteria, not a support claim for another project |

For operating the database, use [Data storage and preservation](../users/data-storage.md). For reporting an issue, use [user troubleshooting](../users/troubleshooting.md) and the repository's [security policy](../../SECURITY.md).
