# Architecture overview

OEP consists of a local calculation app and a thin Home Assistant integration. Start here to choose the responsible layer before changing code. Use the [technical design](../../design/TECHNICAL-DESIGN.md) for complete contracts and the [user explanation](../users/how-oep-works.md) for product concepts.

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

Live HTTP and background refresh owners await `LiveInputs.refresh_async` or
`receive_async` while retaining the application state lock. A thread constructs
the installation basis and appliance, EV and thermal snapshots. Coordinator
invalidation, snapshot installation and submission remain on the event loop.
Cancellation drains the preparation worker before releasing the state lock,
including repeated cancellation. Installation rechecks measurement and future
input expiry after the worker finishes. The synchronous domain entry points
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

| Area | Source location | Start with |
|---|---|---|
| Shared planning and publication | `planning.py`, `live_inputs.py`, `planning_policy.py` | [Technical design](../../design/TECHNICAL-DESIGN.md) |
| Physical installation and commands | `installation/` | [Installation planning](INSTALLATION-PLANNING.md) |
| Native optimization and pure models | `optimization/` | [Solver selection](../../design/SOLVER-SELECTION.md) and [independent scenarios](../../design/PLANNER-SCENARIOS.md) |
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
