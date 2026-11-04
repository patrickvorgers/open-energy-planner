# Open Energy Planner

Read the **[OEP documentation](https://patrickvorgers.github.io/open-energy-planner/)**
for the development preview, a safe first-plan demo and the maintained technical
guides. The [developer start page](https://patrickvorgers.github.io/open-energy-planner/provisioning/)
guides local Dev Container provisioning. See [site maintenance](docs/development/DOCUMENTATION-SITE.md)
for a local preview and publication checks.

> [!WARNING]
> **Early-stage software**
>
> Open Energy Planner is under active development and has not reached a stable
> production release. Features, configuration formats and APIs may change.
> Forecasts, plans and financial estimates may be incomplete or incorrect.
>
> Start in a test environment and back up your Home Assistant configuration and
> OEP data before upgrading. Independently validate outputs before enabling
> physical control. Keep hardware safety limits and device protections independent
> of OEP. Automated tests do not establish physical-device safety or guarantee
> financial savings. See the [security policy](SECURITY.md).

Public development begins from a clean source snapshot. Earlier development
history remains in a private archive. See [source provenance](docs/development/SOURCE-PROVENANCE.md).

Settings > Data storage supports verified database migration and shows the active database size and available volume space
for local SQLite. It warns below 1 GiB or 5.0% free. External database free space
remains unknown. See [capacity measurements](docs/development/DATABASE-SCHEMA.md#capacity-read-model)
for measurement scope and limitations.

Local energy optimization for Home Assistant. The current installation path connects the real HiGHS solver to the app and thin HA integration with explicit synthetic and Home Assistant measurement modes. HA mode requires separately supplied forecast/price inputs and has no synthetic fallback. It is not the first complete product release.

The tested path is: **immutable input snapshot → isolated solver worker → independent physical/cost validation → stored complete plan → authenticated local HTTP → HA virtual commands**. The app runs separately from HA. Native solver packages are not required by the HA integration.

The full Linux AMD64 acceptance suite covers native planning and app behavior, real Home Assistant integration, SQLite/PostgreSQL/MariaDB/MySQL storage contracts, and desktop/mobile browser flows. Ruff, formatting and documentation checks run alongside the behavioral tests. CI binds its results and artifacts to the exact tested Git tree. Exact test totals and reports belong to each [GitHub Actions run](https://github.com/patrickvorgers/open-energy-planner/actions), rather than a fixed milestone count.

See [development and onboarding](docs/development/README.md), [installation planning](docs/development/INSTALLATION-PLANNING.md), [release scope](design/FIRST-RELEASE-SCOPE.md) and [technical design](design/TECHNICAL-DESIGN.md).

[Plan, history and evaluated savings](docs/development/SAVINGS.md) use retained metering, a continuous self-consumption reference, independent bonus counters and versioned financial corrections. Open **Planning**, **Savings** or **Forecast quality** from the navigation. Actual, predicted and simulated results remain distinct. The implementation does not establish physical execution or causally proven savings.

## Implemented

- App-owned installation configuration and live planning bootstrap, with optional
  independent batteries, standalone/shared hybrid converters, mixed AC/DC routes,
  aggregate connection limits and per-device HA targets. Native setup reuses
  Energy sources where possible. A confirmed annual demand estimate also works
  without history. See [installation planning and remaining boundaries](docs/development/INSTALLATION-PLANNING.md).

- Local pvlib/PVWatts solar forecasting with a 48-hour horizon, multiple independent
  production sources/inverters, shared inverter caps, Energy-prefilled app wizard
  editing and per-source historical regression candidates in shadow mode. Exact
  DC/AC forecasts and compact shared weather/history evidence are archived.
  Standalone AC predictions connect to the existing planning-basis port. See
  [solar forecasting and remaining production boundaries](docs/development/SOLAR-FORECASTING.md).

- Original-plan forecast quality with interactive predicted versus measured household, individual-device and PV charts. See [evidence, coverage and model-selection boundaries](docs/development/FORECAST-QUALITY.md).
- Household energy reconstruction, measured load exclusions and always-available startup/history profiles, with local Ridge-regression candidates and a chronological decision-benefit gate. The app wizard can select a whole-home counter without helpers. Forecasts feed the existing planning-basis port with archived evidence. See [models, tools and remaining production boundaries](docs/development/DEMAND-FORECASTING.md).

- Read-only HA Recorder history adapter with normalized hourly energy contracts. Missing bonus-counter baselines are derived automatically from complete aggregate-export statistics. Manual confirmation remains available for incomplete history. No direct database access or full sensor-history copy. See [history adapter and limits](docs/development/HA-HISTORY.md).

- Safe, dated import/export formulas with authenticated preview/settings, immutable rule revisions and effective-price planning input. Separate aggregate-export bonuses support dated rules, price/time gates and chronological measured counters. Exact billing segments preserve the original control intervals. Independent settlement and small exhaustive decision tests validate the calculations. See [contract prices](docs/development/CONTRACT-PRICES.md) and [bonus rules](docs/development/BONUS-RULES.md).

- Provider-neutral price inputs with separate published/forecast HTTP adapters, 13:05 publication refresh, 30-minute missing-day retries, a 16:05 correction check, original source-version storage and authenticated `/v1/prices`. Forecast fetching and plan computation have separate clocks. See [price inputs and remaining planner boundary](docs/development/PRICE-INPUTS.md).

- In-process HA integration measurement bridge with explicit bindings, unit/sign normalization, freshness checks and immutable readings/issues. See [source boundary and limitations](docs/development/HA-MEASUREMENT-SOURCES.md). Selected observations feed typed app snapshots over the existing connection. Installation settings derive the required per-battery roles without helpers.
- Real HiGHS optimization on a 48-hour, 120-step synthetic snapshot, through the existing class-based device/network/contract models.
- Strict input versions/hash/freshness. One calculation at a time. Coalesced updates and rejection of superseded/invalid candidates.
- Native worker timeout/recovery outside the HTTP event loop. Whole-watt commands are model constraints, not post-solve rounding.
- Independent numerical checks of physics, limits, SoC, losses and costs before publication, with complete input and result records.
- Actual HA config flow, registered virtual EMS/grid/battery entities, local schedule/expiry, complete replacement, unchanged-command suppression and pause/resume with a newly calculated plan.
- Coherent command events and power applicability, zero/unknown/unavailable distinction, token authentication and action-required diagnostics. The setpoint side is explicitly battery DC terminal power.

## Start developing

The [database schema and data dictionary](docs/development/DATABASE-SCHEMA.md)
describes all persisted tables, fields, relationships, JSON document owners and indexes.

For hands-on testing without a production installation, use the [separate persistent test environment](docs/development/USER-TEST-ENVIRONMENT.md). It includes Home Assistant, two simulated solar sources, a battery, Energy history and sample contract settings.

Open the **[developer start page](https://patrickvorgers.github.io/open-energy-planner/provisioning/)** to start provisioning. It links to a local VS Code clone and explains the prerequisites. Install VS Code, Dev Containers, Git, Python 3.12 or newer and Docker with Linux containers. Verification needs existing GitHub read access through Git Credential Manager or `GH_TOKEN`/`GITHUB_TOKEN` in your host terminal. Never enter credentials on the start page. Download the original `installation-bundle-amd64` ZIP from a successful Checks candidate merged onto `main`, then check out its matching verified merge. From a host terminal in that checkout, prepare and start the package before choosing **Dev Containers: Reopen in Container**:

```console
python -m script.supervisor.dev prepare --bundle /path/to/installation-bundle-amd64.zip --run-id RUN_ID
python -m script.supervisor.dev start
```

Replace both placeholders with the original ZIP path and candidate run ID. Pages hosts public instructions only. Provisioning runs locally and preserves existing development data. See [the developer environment guide](docs/development/DEVELOPER-ENVIRONMENT.md#start-and-stop) for package selection, validation and startup.

## Development layout

| Location | Responsibility |
|---|---|
| `src/open_energy_planner/planning.py` | Immutable input, planning use case and current-input publication gate |
| `optimization/adapters/process/worker.py` | Bounded spawned native solver process |
| `optimization/` | Promoted pure model definitions, independent numerical validation and the sole highspy adapter |
| `synthetic.py` | Explicit synthetic snapshots. No live data or hardware |
| `installation/api/`, `home_assistant/setup_catalogue.py`, `frontend/` and `web/` | App-owned responsive installation wizard and read-only HA discovery API |
| `app.py` | Authenticated HTTP coordination and application lifecycle over the shared active database |
| `custom_components/open_energy_planner/` | Standalone HA integration. No app/native imports |
| `tests/app/solver_regression/` | Original independent solver scenarios with namespace/path adaptation only |
| `tests/app/planning/test_planning_pipeline.py` | Publication/freshness/worker/economic acceptance tests |
| `tests/ha_contract/` | App-side HA message contracts without starting HA |
| `tests/custom_component/unit/` and `tests/custom_component/integration/` | Standalone integration unit tests and genuine HA framework behavior |
| `tests/test_environment/` | Offline fixtures and genuine simulator/history-retention tests |
| `tests/frontend/unit/` and `tests/frontend/browser/` | Public routing/security contracts and real desktop/mobile behavior |
| `tests/provisioning/` and `tests/supervisor/` | Ordinary example provisioning and separate platform lifecycle acceptance |

After package preparation, open this folder in VS Code and choose **Reopen in Container**. The [Dev Container](.devcontainer/devcontainer.json) starts the real Supervisor developer environment with the accepted OEP app/integration pair, isolated Home Assistant and simulated devices. Login details remain in the ignored `.local/dev/developer.json`. See [development guide](docs/development/README.md) for commands. `python script/ci/ci.py` runs the complete native/HA/provisioning chain from a Docker-enabled host. GitHub Actions uses this same entry point.

## Local API

HA OS exposes administration through authenticated HA ingress and pairs automatically through Supervisor without user tokens/codes. Standalone administration requires the app Bearer token and explicit pairing consent. Both routes use pinned TLS and a separate persistent HA credential restricted to synchronization routes. See [automatic pairing](docs/development/AUTOMATIC-PAIRING.md). The explicit `--demo` mode optimizes synthetic inputs through the same installation model and schema-2 device commands as live planning. Empty archives contain no fabricated plan. Deterministic test schedules and injected solver results belong to test fixtures.

- `GET /v1/snapshot`: full accepted plan, release/configuration, enable intent, receipt acknowledgement and current calculation status/source ID. In HA mode, polling checks input validity and may request a rate-limited planning round when due.
- `PUT /v1/enabled`: boolean plus stable control request ID. Pause is immediate in HA. Resume requests a fresh optimizer round. Repeated delivery of one control request does not create repeated plans.
- `POST /v1/replan`: explicit synthetic source update (`initial_kwh`, optional first-slot `first_load_kwh`). Invalid input is rejected. In HA mode it requests a fresh calculation from accepted real measurements and the independent future-input provider. Synthetic initial-state fields are not used.
- `PUT /v1/sources`: HA-mode app-owned source configuration with expected-revision checks.
- `POST /v1/observations`: HA-mode normalized readings/issues with per-source timestamps and configuration revision.
- `POST /v1/history`: normalized hourly export evidence for the current app-requested missing bonus baseline, or an explicit read failure. Stale request versions cannot overwrite confirmed counters.
- `POST /v1/ack`: receipt of the current plan ID, never physical execution confirmation.

Battery positive W means discharge, negative W means charge. A non-applicable fixed target is `unknown`. Pause/expiry means `unavailable`. 0 W is a real idle command. An `open_energy_planner_command` event carries the coherent current command after entity writes. Filter by `entry_id`. Do not convert missing numeric states to zero. The active-plan timestamp diagnostic carries the current plan ID. Per-device target attributes contain command interpretation only, so accepting an identical command in a new plan does not retrigger target automations. Separate HA entities are not a general transactional system.

Plan generation may finish after the first covered interval began. The complete input snapshot/hash, version references, independent costs, checked flows and command side/resolution stay with the original plan. A failed round does not overwrite or prolong the last accepted plan. Plans, configuration, rules, source/model archives and evaluations use the shared active SQLAlchemy database. SQLite, PostgreSQL, MariaDB and MySQL share backend contracts and complete transfer tests. See the [database dictionary](docs/development/DATABASE-SCHEMA.md). Full deployment backup and restore remain separate acceptance gates.

## Scope still to build

First product-release admission still requires the remaining acceptance gates in [the release scope](design/FIRST-RELEASE-SCOPE.md). Reserve recovery, consumption-priority planning, automatic terminal valuation, continuous reference/savings, contract settings, shared SQLAlchemy storage and responsive planning/savings views are implemented. Learned-model promotion requires independent chronological decision evidence. Collecting that evidence is distinct from descriptive forecast accuracy. Deployment-wide backup/restore, storage sizing and lossless compaction, supported-platform admission and physical execution evidence remain separate gates. The app publishes virtual commands. No physical-device or complete product-release claim follows from the simulated and isolated HA tests. See [installation planning and remaining boundaries](docs/development/INSTALLATION-PLANNING.md).

The [solver selection decision](design/SOLVER-SELECTION.md) describes the maintained direct-highspy adapter. The project uses [AGPL-3.0-only](LICENSE). Dependencies retain their [original notices](THIRD_PARTY_NOTICES.md), and release dependencies require review. Public-source distribution and product-release admission remain subject to the release gates above.

See the [observation-to-planning boundary](docs/development/HA-MEASUREMENT-SOURCES.md#observation-to-planning-increment) for HA mode, current limits and source selection.


Installation artifacts and native HA OS verification: [acceptance guide](docs/development/HAOS-ACCEPTANCE.md). Virtual grid/battery/solar signals and zero-grid applicability: [device contract](docs/development/VIRTUAL-DEVICES.md). Installation through HACS has not been validated. The same packaged integration can be installed by an administrator for native acceptance.
