# First release scope: Open Energy Planner

Development proceeds in the public `patrickvorgers/open-energy-planner` repository.
See [development foundation](DEVELOPMENT-FOUNDATION.md) for reproducible onboarding
and CI. Source availability does not establish a stable product release or change
any functional inclusion below.

This document consolidates the agreed first product release. It does not narrow earlier commitments or turn an implementation proposal into a confirmed requirement. Detailed behaviour remains defined by [requirements](REQUIREMENTS.md). Implementation boundaries are in the [technical design](TECHNICAL-DESIGN.md). Changes to this boundary require an explicit product decision. Finishing a milestone or solver experiment does not constitute the first product release.

## Product boundary

Open Energy Planner provides local energy optimization for Home Assistant, with minimal setup and hardware-independent virtual commands. One EMS installation has one grid connection and may contain multiple solar installations and batteries. Solar and storage are optional. The model remains extensible to flexible consumers, with release admission governed by the exclusions and follow-on agreements below.

| Included in the first release | Required behaviour and traceability |
|---|---|
| Local app and thin HA integration | The app owns configuration, forecasts, planning and evaluation. The integration exposes virtual devices, current commands and availability. User automations perform physical control and fallback. No mandatory MQTT or direct HA database access. ARCH-01–14. |
| Simple onboarding and later configuration | Require a complete HA electricity Energy dashboard. Reuse validated meters/history, hide already known inputs, explain remaining fields and offer typed HA sensor dropdowns. Report missing/incomplete Energy configuration instead of manual reconstruction. Ask only necessary device characteristics, limits and relationships. No user-built measurement templates/helpers or mandatory automation setup. CONFIG-01–11. |
| Joint device planning | Multiple PV/battery instances, standalone AC converters and hybrid AC/DC relationships. Configured efficiencies, shared inverter limits and aggregate grid constraints. Stable IDs, per-unit reserves, measured SoC/power and optional wear cost, default zero. Device capabilities determine exposed command entities. MODEL, PV, BAT and GRID requirements. |
| Two strategies | Default `minimize cost`. Optional `minimize consumption` with gross import plus terminal stock replacement first and economic cost second. Strategy is persistent configuration, recorded per plan. New choices become active with a validated replacement plan. GOAL-01–14. |
| Commands and failure behaviour | Fixed battery power or zero-grid balancing with explicit applicability. Publish current commands from a fully accepted plan, with coherent mode/target semantics. Bundle recalculations, avoid needless command changes, honour validity, pause and restart/restore rules. HA performs fast physical regulation and safety. GRID, PLAN and UITVAL requirements. |
| Forecasting and horizon | Automatic Open-Meteo weather input, simple initial estimates and locally learned PV/whole-home demand using available history. Adopt learned models only after chronological decision-relevant evaluation. Published market prices precede externally predicted prices. Target 48 hours: first 24 in quarters, last 24 in hours. 120 aligned intervals. Explicit shortened-horizon fallback and automatic terminal energy valuation. FORE and HORIZON requirements. |
| Contract rules | Import/export formulas with examples. Separate bonus rules with explicit tested price/time conditions and period counters over aggregate grid export. Visual rule builder plus equivalent text representation, without arbitrary executable scripts. Versioned effective dates, corrections and automatic affected-cost reevaluation. One contract may span multiple rule periods. Unsupported constructions are rejected. PRICE-01–15. |
| Reference and savings | Expected no-battery reference by default, plus continuous self-consumption reference with ordinary PV production even at negative export prices. Matched initial SoC and bonus counters, then independent states. Show forecast and evaluated financial/import differences, grid costs, wear and inventory value separately. Hardware changes are explicit inventory adjustments. UI-03–18. |
| Durable history and storage | Preserve original plans, inputs, rules, forecasts, configuration and evaluations through immutable shared versions. Store required settlement measurements without copying all HA history. No routine expiry. Explicit configuration-related deletion identifies lost history. SQLite by default, optional PostgreSQL, MariaDB or MySQL via SQLAlchemy. Consistent backup/restore procedures appropriate to each deployment. OPSLAG requirements. |
| Built-in user interface | One entry from HA for setup, plan, history, savings and later device changes. English-only first release, responsive mobile/desktop flows. Planned, forecast and measured data remain distinct. Actionable faults appear in logs and user-facing notifications. Technical log retention is bounded. UI and development requirements. |
| Distribution and quality | Same tested release for app/container and HACS integration, installed separately. One-time HACS integration installation, then automatic Supervisor-authenticated app connection and HA-ingress access on HA OS, without confirmation/code/token/address. Standalone containers retain explicit consent. Manual networking fallback remains. The app never installs integration files or requires writable HA configuration access. HA OS custom app repository and standalone Container deployment. Latest tested stable HA, AMD64 primary. ARM64 experimental only after native gates. Python, direct highspy/HiGHS, modular classes and independent validation. Dev Container, Ruff, pytest and applicable Playwright checks. CodeRabbit supplements these. Open source under the agreed intended AGPL-3.0-only model, subject to dependency checks. DEV, DIST and SOLVER requirements. |

## Confirmed follow-on: individual consumers

The user has agreed the [individual consumer increment](REQUIREMENTS.md#individual-consumption-devices-confirmed-next-increment): Energy-owned device discovery, forecast-only or explicitly shiftable operation, one learned average cycle profile, helper-free task requests and mandatory started/not started/completed feedback from HA automations, optionally derived from measured power. Not started also guarantees no delayed execution of that attempt. Missing feedback blocks retries. The [technical design](TECHNICAL-DESIGN.md#individual-consumer-forecasting-and-task-execution-agreed-next-increment) defines responsibilities and acceptance scenarios. Implementation is documented in the current domain guides. This agreement does not change the first-release exclusions below. Expanding the first-release boundary remains an explicit product decision.

## Confirmed follow-on: thermal storage

The confirmed [thermal storage increment](REQUIREMENTS.md#22-generic-thermal-storage-confirmed-development-increment)
adds generic heat storage with an electric boiler as its first application.
See the [implementation guide](../docs/development/THERMAL-STORAGE.md). This is a
follow-on agreement and does not rewrite the agreed first-release boundary.

## Explicit exclusions

- Physical brand/protocol drivers, automation generation, physical safety enforcement and phase-by-phase electrical protection in the planner.
- Multiple grid connections in one installation and planned flexible loads such as EVs or heat pumps in this first release.
- Supplier-controlled trading, arbitrary executable contract scripts and a promise to support every European contract or tax system.
- Local market-price model training, mandatory DAO/DAP services, and user-selectable internal solver components.
- Dynamic strategy-switching automations, general nonzero grid power targets, and historical reoptimization of original plans.
- Compatibility matrices across different app/integration releases or older HA releases. Full ARM parity without native/practical validation.
- A second maintained production solver adapter, custom general modelling language, or speculative performance machinery without measured benefit.

## Implementation obligations and decision boundaries

| Work item | How it is resolved | Escalate only if |
|---|---|---|
| Local transport, discovery, authentication and UI access | Implement a minimal HA OS/Container chain with explicit typed messages and tests. Equal release policy is already decided. | Minimal setup or the single configuration owner cannot be preserved. |
| Exact supported contract syntax and examples | Specify a finite supported grammar and fixture catalogue, including negative prices, price/time conditions, chronological counters and caps. Independently validate exact translation. Supplier examples require verified applicable conditions. | An agreed contract building block must be removed or a new contract feature added. |
| Terminal valuation and balancing estimates | Define and test an automatic method under HORIZON-09 for both scenarios. Test consumption-priority end effects and avoid double counting. | Objective ordering or the agreed economic meaning would change. |
| Timeliness, invalidation and runtime budgets | Set automatic defaults using measured failure/restart/performance scenarios. Keep hard limits and explicit status. | New mandatory settings or weaker guarantees are proposed. |
| Data sources and access | Verify coverage, historical availability and redistribution/access conditions before relying on a source. Keep adapters replaceable. | An account, key, payment or materially reduced coverage becomes necessary. |
| Learning and curtailed PV | Validate initial estimates, training selection, chronological backtests and estimated uncurtailed production. | The agreed learning/reference behaviour cannot be delivered. |
| Persistence, backup and release packaging | Test version reconstruction, corrections, counter continuity, consistent restore and each advertised database/platform combination. | Retention, supported databases or deployment choices would be reduced. |

Each obligation must be implemented and verified before release admission. Current capability guides below identify supported behavior and remaining limitations. They do not justify an indefinite product interview or silently dropping agreed features. UI library, class/file names, endpoint names and measured numeric defaults are implementation decisions within this boundary.

## Release acceptance

The first product release is ready only when:

1. A clean supported installation can connect the app and integration, complete the short wizard and show a validated plan without mandatory helpers or physical automations. Devices/settings can be changed later.
2. Independent planner tests cover both strategies, reserves, negative prices, bonus limits, conversion routes, shared boundaries, terminal value and defined infeasibility/status handling. Required installation combinations include the mixed hybrid/standalone case. Optional devices remain supported.
3. The HA chain demonstrates coherent current commands, unchanged-command behaviour, complete replacement, expiry, pause, disconnection, ordinary restart and backup restore. No test needs to control production hardware.
4. Forecasts, effective contract rules, external access conditions and the advertised horizon/fallback work for the declared source coverage. Missing data is never silently zero or a successful evaluation.
5. Historical plans remain readable exactly as stored. Reference states/counters continue across period filters. Period savings, contract corrections and hardware inventory adjustments pass independent numeric checks.
6. Advertised database backup/restore and supported platform/dependency combinations pass. AMD64 is mandatory. Experimental ARM64 is published only after its native admission gate.
7. The jointly versioned release passes Ruff, pytest and relevant desktop/mobile Playwright flows, includes source/licence notices and installation/recovery documentation, and has both installable artifacts available.

Every implementation task must identify a requirement and an acceptance result. A solver test pass alone is not evidence of HA integration, forecasting quality, savings accuracy or readiness for release.

## Current capabilities and release boundaries

The maintained implementation includes:

- [Installation setup](../docs/development/INSTALLATION-PLANNING.md), multiple batteries/converters, mixed AC/DC routes and capability-driven schema-2 HA commands. Synthetic development inputs use the same projection.
- [Solar](../docs/development/SOLAR-FORECASTING.md) and [demand](../docs/development/DEMAND-FORECASTING.md) forecasting, with app-owned Energy-prefilled configuration. Learned-model promotion requires independent chronological decision evidence.
- [Measurement forwarding](../docs/development/HA-MEASUREMENT-SOURCES.md), [Recorder history](../docs/development/HA-HISTORY.md), [price inputs](../docs/development/PRICE-INPUTS.md) and [contract formulas](../docs/development/CONTRACT-PRICES.md). Missing required live inputs block planning instead of invoking synthetic fallback.
- [Aggregate-export bonuses](../docs/development/BONUS-RULES.md) with exact billing segments, dated rules and chronological counters. Source-dependent supplier exclusions remain outside exact coverage. An optional paid-window constraint prevents planned battery discharge together with grid export while quota remains at the segment start, retaining self-consumption. Exhaustion releases later segments until quota is restored. Base formulas and bonuses remain separate.
- [Terminal valuation](../docs/development/TERMINAL-VALUATION.md), [reserve recovery](../docs/development/RESERVE-RECOVERY.md) and both [planning strategies](../docs/development/CONSUMPTION-STRATEGY.md). Requested, published and HA-accepted settings remain distinct.
- [Shared durable storage](../docs/development/PLAN-ARCHIVE.md), immutable plans, [settlement and savings](../docs/development/SAVINGS.md), correction generations and independent reference counters. Cross-backend transfer includes all OEP tables. Unreleased file/schema conversion is not supported.
- [Individual consumers](../docs/development/APPLIANCE-ARCHITECTURE.md), [shiftable appliances](../docs/development/SHIFTABLE-APPLIANCES.md), [EV charging](../docs/development/EV-CHARGING.md) and [thermal storage](../docs/development/THERMAL-STORAGE.md). Implementing a follow-on capability does not expand the first-release admission boundary without an explicit product decision.

The domain guides define current supported behavior and limitations. Complete contract/region coverage, deployment-wide backup/restore acceptance, storage sizing/compaction, supported-platform admission and final product-release gates remain separate obligations. Simulated and isolated HA checks do not establish physical equipment execution or forecast superiority. Use the maintained [coverage contracts](TEST-COVERAGE.md) and [solver regression contracts](SOLVER-REGRESSION-MIGRATION.md) for validation.
