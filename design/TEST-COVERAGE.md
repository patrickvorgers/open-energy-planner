# Test research and coverage plan: Open Energy Planner

## Price forecast selection

[Source-selection scenarios](../tests/app/price/test_price_selection.py) independently exercise matched MAE arithmetic, thresholds, consecutive days, area/lead/window isolation, restart and storage faults, normalization, actual-price priority and fallback. Responsive app tests show source preference versus actual use. See [policy and limitations](../docs/development/PRICE-FORECAST-SELECTION.md). Synthetic data does not prove live forecast superiority.

## Current validation contracts

Use maintained planning/publication tests, independent solver regressions, actual-HA tests and the selected CI suites. The [solver regression contracts](SOLVER-REGRESSION-MIGRATION.md) define physical and numerical invariants. Record execution outcomes in CI artifacts. A passing simulated test does not establish native hardware acceptance.

- One factory builds separate reference and optimized instances from the same model/component definitions. Changes to supported efficiencies, capacities, limits or contract formulas apply consistently to new instances. Mutation/fixing in one instance cannot change the other.
- A fixed reference trace cannot improve with prices: forbid hidden battery cycling, price-based PV curtailment and alternative route allocations that improve bonus eligibility. Reject an inconsistent reference trace without freeing fixed decisions. Preserve independent battery states and chronological bonus counters across rounds.
- Check shared highspy model cost expressions against an independent plain-data evaluator and hand-derived examples. Include the unit check `10 kW * 0.25 h * 0.30 EUR/kWh = 0.75 EUR`, negative prices, taxes/formulas, bonus ceilings, loss-side measurements and correctly normalized wear. Inject wrong coefficients or duplicate bonus credit so shared-source bugs cannot pass solely through agreement between the two model instances.
- Distinguish contractual net cost, wear, residual value and the economic objective. Do not label consumption's kWh objective as EUR. Verify hard reserve/grid limits cannot be bought off through penalties.
- Retrospective comparison uses actual EMS costs versus a reference recomputed under matched observed circumstances, with estimation labels retained. In the illustrative EUR 7.80 reference / EUR 6.62 actual / EUR 6.35 forecast case, the net-cost comparison is EUR 1.18, not EUR 1.45. Keep wear and inventory changes separate and retain the existing non-overlapping period accounting.
- Preserve original model/policy/cost versions and historical outputs across upgrades. Explicit contract corrections create new evaluations without optimizing an old plan. Include shared-reference overhead in the later performance comparison.

Additional acceptance contracts:

- Confirmed horizon: 48 hours, first 24 hours at 15 minutes and last 24 hours at 60 minutes. 120 decision intervals when aligned. Verify duration-weighted energy/costs, continuity at the resolution transition, partial first intervals, UTC/DST handling and existing shortened-horizon fallback. Coarse decisions must preserve applicable price/contract subinterval accounting, bonus thresholds and physical limits. An hourly net balance alone must not hide an infeasible within-hour deficit.
- Device composition: multiple instances of a type, installations without PV/battery, explicit AC/DC paths and shared inverter limits, stable IDs and correct port measurement sides. No cross-instance state or accidental alternate energy route.
- Reference policy: share physical properties and financial definitions while preserving independent reference state/counters and unconditional PV production subject to ordinary physical limits. Do not introduce price-dependent reference behavior through solver freedom.
- If incremental updates are implemented: compare each changed coefficient, forecast, initial state, bound and counter with a clean rebuild. Structural changes invalidate reuse. Native highspy updates are optional. This conditional work is not required merely because the interface supports them.
- If warm starts are implemented: shifted horizons, invalidated starts and cold/warm runs must preserve feasible economics and selected strategy. Equivalent optima need not produce identical traces.
- Platform release checks: validate native package ABI/libc fit and the actual image on each eligible architecture under DEV-17. Compare results using independent invariants and objective tolerances. Emulated timing does not satisfy native performance evidence.
- Performance: instrument available stages and total duration. Document combined timings, sample size, p95/max, memory, status and gap. The production stack selection is complete. Measure regressions within the selected stack.

## 1. Scope and provenance

Use external tests as inspiration for coverage categories, never as fixtures or expected answers. All Open Energy Planner cases, parameters, arithmetic and assertions are independently designed. Do not translate or lightly rename another project's test case and call it original.

This is a read-only review of selected source files and their assertions. External suites were not executed. Findings describe those files at the cited revision, not comprehensive coverage or quality rankings of those projects. No code or fixture data was imported.

### DAO

Reviewed main commit `810392a2cb4ebe76c684102cde24dd2b077e27c6`.

| Observed category | What the reviewed tests check | Inspiration and limitation |
|---|---|---|
| Arbitrage, negative/flat prices and PV timing | Objective baselines plus selected charge/discharge windows. Some cases constrain activity | Use economic bounds and negative-price variants. Stored solver output alone is not an independent optimum. |
| Battery limits and multiple batteries | Variable/SoC bounds and selected cross-battery direction restrictions | Test shared limits and heterogeneous batteries. A blanket prohibition on charging one battery while another discharges is DAO policy, not automatically our requirement. |
| Availability, deadlines and partial first intervals | Selected scheduling outcomes, reasons and exclusivity checks | Reuse categories for time/availability coverage. Defer EV-specific functionality outside our first release. |
| Scenario execution and baselines | Solver cases require opt-in. Missing objective baselines may be pending | Our required scenario gate must run and have independently justified expectations, not silently skip or bless unknown results. |

Sources: [battery cases](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/prog/scenarios/cases/battery.json), [price cases](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/prog/scenarios/cases/price.json), [solar cases](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/prog/scenarios/cases/solar.json), [assertions](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/prog/scenarios/expectations.py), [pytest entry](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/tests/prog/test_scenarios.py), [baseline handling](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/prog/scenarios/baseline.py).

### HAEO

Reviewed main commit `ce90ac2e8fcbe3614d155fa251354b1a911f5b96`.

| Observed category | What the reviewed tests check | Inspiration and limitation |
|---|---|---|
| Recorded scenarios | Successful optimization and snapshots of published HA outputs with a fixed clock | Useful integration regressions. Snapshot equality is not an independent proof of economic optimality. |
| Battery and connection models | Expected numeric series, limits, efficiencies and remaining-energy valuation | Test measurement sides and route losses. HAEO soft-bound penalties must not replace our hard reserve rule. |
| Network and route pricing | Cost application at the right measurement point, prohibited flows and no duplicate production | Test bonus provenance and AC/DC conversions, not only aggregate balances. |
| Equivalent optima and model updates | Primary-cost preservation, selected order invariance and warm/cold comparisons | Test tie-breaks separately. Add warm-start tests only when model reuse is implemented. |
| Benchmarks | Construction/update/solve measurements. Selected checks include a finite objective | Measure stages separately. Finite values do not establish correct outcomes. |

Sources: [recorded scenario runner](https://github.com/hass-energy/haeo/blob/ce90ac2e8fcbe3614d155fa251354b1a911f5b96/tests/scenarios/test_scenarios.py), [battery model cases](https://github.com/hass-energy/haeo/blob/ce90ac2e8fcbe3614d155fa251354b1a911f5b96/custom_components/haeo/core/model/tests/test_data/battery.py), [connection cases](https://github.com/hass-energy/haeo/blob/ce90ac2e8fcbe3614d155fa251354b1a911f5b96/custom_components/haeo/core/model/tests/test_data/connection.py), [network assertions](https://github.com/hass-energy/haeo/blob/ce90ac2e8fcbe3614d155fa251354b1a911f5b96/custom_components/haeo/core/model/tests/test_network.py), [policy scenario](https://github.com/hass-energy/haeo/blob/ce90ac2e8fcbe3614d155fa251354b1a911f5b96/custom_components/haeo/core/model/tests/test_policy_scenario.py), [warm-start tests](https://github.com/hass-energy/haeo/blob/ce90ac2e8fcbe3614d155fa251354b1a911f5b96/custom_components/haeo/core/model/reactive/tests/test_warm_start.py), [benchmark runner](https://github.com/hass-energy/haeo/blob/ce90ac2e8fcbe3614d155fa251354b1a911f5b96/tests/scenarios/test_benchmark.py).

### EMHASS

Reviewed indexed `master` source, with some source views approximately two weeks old. No commit hash or latest test result was confirmed. Treat these observations as less reproducibly pinned than the DAO/HAEO references. Pin a revision before any future detailed comparison.

Observed assertions include negative-price solver status, AC/hybrid route restrictions, disabled loads producing no consumption, and tie-break preferences preserving primary cost. Some reviewed tests check only output structure or allow an early return on non-optimal status. A test name about infeasibility does not itself prove infeasibility is asserted.

Inspiration: guard against artificial energy sinks, forbidden exports through alternate routes, and auxiliary objectives changing the economic answer. Our expected-feasible cases must not pass by returning early on solver failure. Infeasibility cases must check the correct status.

Source: [EMHASS optimization tests](https://github.com/davidusb-geek/emhass/blob/master/tests/test_optimization.py), especially the reviewed sections concerning [disabled loads](https://github.com/davidusb-geek/emhass/blob/master/tests/test_optimization.py#L8373), [AC/hybrid routes](https://github.com/davidusb-geek/emhass/blob/master/tests/test_optimization.py#L8512) and [cost-preserving preferences](https://github.com/davidusb-geek/emhass/blob/master/tests/test_optimization.py#L8304). Line links can drift on master. Do not infer that other tests do not cover those risks.

No supplier-specific capped-bonus scenario was established in this selected review. Our bonus-cap coverage is independently derived from our own requirements, not evidence of a missing feature in another EMS.

## 2. Coverage status terminology

- **Specified:** S01–S04 contain full numerical base cases and independent optimality arguments.
- **Companion:** a named variation with stated changes and expected result. Some qualitative companions still need complete fixture data.
- **Planned:** a necessary catalogue case with acceptance criteria. Numerical fixtures and independent proof must be completed before it is executable coverage.
Implemented subset: 70 checks on the selected HiGHS stack. 19 cover AC/DC topology. See the active proof linked above. The full catalogue below remains a coverage plan, not a claim that every feature is implemented.

## 3. Planner/solver catalogue

S01–S04 and their companions are specified in PLANNER-SCENARIOS.md. The following **39 additional catalogue cases define required coverage**. Several have partial overlap with the implemented small/horizon scenarios, but the catalogue as a whole is not implemented. An overlapping fixture does not satisfy every listed criterion. H01–H06 provide explicit executed horizon evidence, not blanket completion of these IDs. Tests for flexible appliances are deliberately deferred, but the device model remains extensible.

| ID | Category / input construction | Required independent checks | Primary traceability |
|---|---|---|---|
| P01 | Grid and household only. No battery/PV | Grid supplies exactly demand. No invented SoC prerequisite or storage variables | MODEL-01, UITVAL |
| P02 | PV and demand without battery | Direct consumption first when economically optimal. Surplus export and deficit import balance | PV, GRID |
| P03 | Battery without PV | Valid import/storage/discharge routes without a solar dependency | BAT, MODEL |
| P04 | Two identical batteries | Aggregate optimal cost and all limits. Accept interchangeable dispatch unless tie-break is specified | DEV-12 |
| P05 | Two batteries with distinct efficiencies/limits | Independently derive marginal dispatch. Do not impose an unagreed cross-battery prohibition | BAT, AC/DC |
| P06 | Initial SoC exactly at minimum/maximum | No reserve discharge or overcharge. Legal movement away from bound | BAT |
| P07 | Initial SoC below reserve, mandatory recovery enabled | Earliest feasible recovery within all limits precedes either optimization strategy. No discharge or price-driven delay. See R01 for a complete numerical example | BAT-05/06 |
| P08 | Same below-reserve input, mandatory recovery disabled | No forced immediate top-up. Do not declare measured initial state impossible solely because reserve is higher | BAT reserve policy |
| P09 | Unreachable mandatory recovery/deadline | Correct infeasible/invalid-input distinction. No automatic relaxation of hard requirements | SOLVER |
| P10 | Grid import limit shared by load and charging | Total import, not each independent component, respects limit | GRID |
| P11 | Export limit with PV and battery | Sum at meter respects export limit. No phantom sink | GRID, PV |
| P12 | Separate AC inverters versus hybrid DC route | Account for each conversion once. Same physical source energy, appropriate different stored energy | AC/DC model |
| P13 | Shared hybrid inverter limit | Sum of relevant concurrent flows respects shared rating. No independent full rating per device | AC/DC model |
| P14 | Forbidden route plus alternative path | Forbidden physical/provenance flow remains forbidden. No laundering through curtailment or storage labels | PRICE-09 |
| P15 | Negative import with real conversion losses | Economic energy balance remains bounded. Losses only from physically permitted active routes | MODEL, SOLVER |
| P16 | Zero/flat prices and equivalent optima | Known objective. Valid flows. No exact trace assertion without agreed tie-break | DEV-12 |
| P17 | Price formula at negative, zero and positive market prices | Independently evaluate formula, taxes/fees and signed cash, before optimizer comparison | PRICE-03/05 |
| P18 | Bonus conditional on price and time | Eligibility at boundary and equality cases. No unsupported strict inequality silently approximated | PRICE-06/08 |
| P19 | Bonus after AC/DC losses | Credited energy measured at the declared contract boundary | PRICE-09, AC/DC |
| P20 | Gross eligible-volume counter versus credited-volume counter | Rule-specific chronological updates. Cap exhaustion differs correctly | PRICE-06/11 |
| P21 | Calendar year versus contract year | Reset at exact respective boundary. Only remaining period budget available | PRICE-11, UI-15 |
| P22 | Contract rule change inside horizon | Correct rule version on each side. No retroactive overwrite | PRICE-04a |
| P23 | Prohibited arbitrary script/nonrepresentable rule | Configuration/model compilation fails explicitly. No favorable fallback price | PRICE-07/08 |
| P24 | Grid target following with sufficient local power | Expected battery flows meet load/PV imbalance within limits. Zero-grid target has no arbitrary offset | GRID-06/12 |
| P25 | Grid target following at battery/reserve/inverter limit | Expected residual grid flow is physical. No promise of exact zero beyond capability | GRID, BAT |
| P26 | Partial first interval and quarter/hour transition | Energy uses actual duration. No full-interval charge in a short remainder | HORIZON-08 |
| P27 | Coarse tail containing a price/rule/limit boundary | Preserve boundary or reject aggregation. Do not claim equivalence where temporal detail is lost | HORIZON, PRIJS |
| P28 | DST spring/autumn day in a European timezone | UTC identity and correct elapsed duration. No missing/duplicate energy or bonus interval | time contract |
| P29 | Missing/NaN/overlapping/stale input or inconsistent units | Reject or use explicit allowed fallback. Never turn missing price/SoC into zero | CONFIG, UITVAL |
| P30 | Time limit with and without candidate | Accept only independently valid candidate with non-optimal status. Otherwise no new plan | SOLVER-02/03 |
| P31 | Infeasible versus unbounded versus technical error | Correct distinct result. No ordinary failure mistaken for infeasibility proof or profit | SOLVER |
| P32 | Tiny costs, large unit scales and rounded W command | Tolerance/scaling justified. Converted commands obey bounds and economic error bound | DEV-12, BAT-03 |
| P33 | Secondary dispatch preference | Primary optimum preserved. No hidden profitability penalty | SOLVER-04 |
| P34 | Rename/reorder devices and split an equivalent interval | Same economic result when the mathematical problem is unchanged. Allow different optimal traces | MODEL, DEV-12 |
| P35 | Bounded future use and terminal value from price information | Method respects future usable volume/losses. Separate automatic value derivation from supplied V(E) tests | HORIZON-09 |
| P36 | Whole synthetic 48-hour installation, with a shortened-horizon counterpart | All invariants and costs. No duplicate use of forecast/terminal cash. Large-case optimality claimed only with evidence | HORIZON-01/06 |
| P37 | S01 prices/hardware with confirmed minimum-gross-import-first objective | Primary optimum 0 kWh, secondary cost 0 EUR. No arbitrage. Export cannot offset import in the primary objective | GOAL-10 |
| P38 | S02 negative price with confirmed minimum-gross-import-first objective | Primary optimum 0 kWh, secondary cost 0 EUR. Do not charge merely to collect negative-price income | GOAL-10 |
| P39 | S03 bonus-cap inputs under minimum-gross-import-first objective | All feasible curtail/export choices have zero import. The secondary optimum selects exports [0.40, 0] kWh and cost -0.040 EUR, including the cap. No import increase permitted for additional financial benefit | GOAL-10, PRICE-06 |

For P07/P09, a recovery deadline is an explicit scenario input, not a newly confirmed user setting. The requirements permit a measured initial state below reserve and require no further discharge. Do not force that initial measurement to satisfy a bound retroactively.

Mandatory recovery priority is now confirmed. P07 must pass with both `minimize cost` and `minimize consumption`. An optimizer that waits for a cheaper interval fails even if its final cash cost is lower. P09's deliberately impossible hard deadline remains a model-status fixture, not a new product configuration field.

## 4. Coverage in other test layers

These do not belong in a day-long repeated-optimization test suite. Keep solver cases independent and test lifecycle effects at their actual boundaries.

| Layer | Planned case IDs | Required behavior |
|---|---|---|
| HA plan publication | I01–I04 | Complete plan swap. Incomplete plan rejected. Old transitions cancelled. Unchanged desired value emits no extra command change. |
| HA modes | I05–I07 | Grid target is authoritative. Derived battery mode. `unknown`/not applicable versus `0 W` versus `unavailable`. No stale power interpreted on switching. |
| Failure/recovery | I08–I11 | Core-data failure, optional-device exclusion, persistent pause, valid old-plan continuation, restart versus restored-backup freshness. |
| Release pairing | I12 | Matching app/integration release. Temporary mismatch does not extend old-plan validity. |
| Strategy switching | I13–I14 | Requested strategy differs visibly from the active plan until full replacement. Failed calculation preserves only a still-valid old plan. Reference/counters continue and historical plan labels remain unchanged. |
| Strategy configuration | U09 | Strategy persists as a stable EMS setting, manually editable in its UI. No HA select/action or automatic strategy switching is exposed. Active-plan strategy may be read-only status. |
| Forecast sources | F01–F04 | Source normalization, issued-at/coverage, published-price priority, missing forecast and shortened horizon. |
| Forecast learning | F05–F07 | Chronological train/test split, no realized-weather leakage, decision-cost comparison with start/weekday profile, no double counting household/battery/future flexible loads. |
| Available PV under control | F08–F09 | Reference uses estimated production without EMS curtailment, not the reduced measured yield. Recorded curtailment is handled in learning. Commanded curtailment is not proof of physical execution. Estimates retain model/input version and quality status. |
| Evaluation | E01–E04 | Continuous independent reference SoC/counters. Date slicing adds non-overlapping results. Cash versus stock value. No summing overlapping plan-horizon savings. |
| Dual benefit metrics | E09 / U08 | S01 reports +0.28 EUR economic benefit and -1 kWh avoided gross import. Show both signs without clamping, export offset or conflating forecast with evaluated results. |
| Hardware transition | E05–E07 | Added full battery is not free EMS profit. Removal/capacity correction not EMS loss. Historical versions unchanged and no global reference reset. |
| Corrections | E08 | Corrected contract evaluation stored as a new evaluation version. Original plan and original inputs remain intact. No historical solver rerun. |
| Plan repricing | E10–E11 | S01 with corrected t0 buy price 0.50 gives +0.10 EUR for the unchanged schedule, while original cost remains -0.28. Reprice a capped bonus chronologically without resetting budget or mutating original counters. Label estimated repricing separately from actuals and do not inherit an optimality claim under new rules. |
| Automatic repricing | E12 | Contract correction schedules affected evaluations automatically. Propagate cap/counter effects chronologically, including later periods. Publish matching corrected EMS/reference results only after completion. Retain original plans and prior versions, and keep live planning responsive. |
| Repricing UI | U10 | Show the latest completed repricing by default with Contract updated and calculation time. Original estimate stays in details. Pending/failed repricing must not relabel old costs as updated or estimates as measured actuals. |
| Storage | D01–D05, parameterized across four backends (SQLite, PostgreSQL, MariaDB and MySQL) | Immutable referenced versions. Exact plan reconstruction. Schema migration. Consistent backup/restore. Transactional failure leaves no apparently published unarchived plan. |
| Backend behavior | D06–D08 | SQLite crash/lock. PostgreSQL/MariaDB/MySQL connection loss. Explicit migration without split archives or empty-store fallback. |
| UI | U01–U05 | Minimal onboarding, adding/editing devices, plan/predicted/measured distinction, savings/stock display, actionable failure. Desktop/mobile Playwright. |
| Historical UI | U06–U07 | Default day timeline follows actual accepted-plan activation boundaries, retains pause/unavailable gaps and shows measurements separately. Selecting one original version restores its own strategy/forecasts/horizon. No solver rerun or historical relabeling. |
| Architecture | A01–A03 | Import boundaries, replaceable adapter contract tests, HA integration independent from app-only libraries. |

## 5. Coverage strategy and implementation order

1. Implement the four core cases with independent oracles before broad fixture generation. Turn the numeric companions into parameterized cases.
2. Add P01–P14 and the bonus/terminal cases before claiming the full installation model works. Unimplemented first-release functions do not count as tested because they have catalogue IDs.
3. Complete time, input-validation, status and rounding cases. Add validator mutations and targeted combinations such as negative prices + losses + inverter limits and bonus cap + contract boundary.
4. Add deterministic, bounded generated cases for broader combinations. Always validate invariants. Compare tiny instances with independent enumeration or analytic bounds. Compare solver candidates on the same mathematical inputs, not unrelated implementations.
5. Run P36 and representative scale cases for latency/memory: single PV/battery. Several distinct batteries/PV systems. Shared hybrid routes. Many bonus segments. Record hardware, threads, model build time, solve time, memory, candidate status and gap. Do not invent universal timing budgets before measurement.
6. Make the relevant pytest scenario group mandatory in CI alongside other required gates. A skipped or pending known-outcome scenario is not a passed release gate.

Code/line coverage is supplementary. Track requirement-to-case mapping, assertion strength, meaningful branches and deliberately broken-plan rejection. Do not claim 100% functional coverage from a count of cases or a percentage of lines.

## 6. Product choices still exposed by the scenarios

- S04 retains application of a supplied function. The configured installation now independently checks automatic `bounded-average-v1` derivation, bounded future volume, shared limits, post-horizon/proxy provenance and solver choices in `tests/app/installation/test_terminal_valuation.py`. [Method and limits](../docs/development/TERMINAL-VALUATION.md). These tests establish arithmetic correctness, not empirical forecast skill or continuous-reference savings.
- The exact set of contract functions/conditions must be enumerated and proven representable. S03 is a synthetic bounded rule, not a live supplier profile.
- Confirmed reference policy: no active price control. PV keeps producing, even when exporting its surplus costs money. Existing local battery self-consumption and physical limits still apply. The same reference applies to either EMS strategy. S02-PV/S02-OFF and S03 include reference-cost comparisons under this fixed policy. The local yield model supplies estimated production without EMS restriction when actual PV is curtailed. The estimation method and training-point selection still require technical validation, not another choice of reference behavior.
- EMS strategy and reporting reference are separate. `Minimize consumption` has confirmed lexicographic priorities: gross import first, economic cost second, with no export offset to import. S01–S04 explicitly use cost minimization. P37–P39 specify corresponding strategy checks. `Minimize cost` is the confirmed first-setup default. Switching strategy activates only with the accepted new plan. Every plan retains its original strategy label. Validate terminal-stock replacement and objective tolerances through the [consumption-strategy contracts](../docs/development/CONSUMPTION-STRATEGY.md).
- Exact mode-switch event ordering and tolerance/budget defaults must be validated technically. Their semantic requirements already exist.

These are visible limitations of the current design coverage. No result from another project's tests resolves them automatically.

## Retained price-history query coverage

The shared SQLite/PostgreSQL/MariaDB/MySQL matrix includes a five-year hourly
history fixture in `tests/database/performance/test_price_history_performance.py`. It verifies indexed,
bounded latest-status reads, market/lead/time filtering, independent 30-day MAEs,
retention counts and a lossless/idempotent scoring-index rebuild. Native plans and
seven warmed baseline/current timing samples are saved as CI artifacts. These checks do not benchmark the complete plan archive.

## Implemented BAT-05/BAT-06 recovery proof

See [reserve-recovery scenarios](../docs/development/RESERVE-RECOVERY.md#regression-proof).
P07/P08 now have executable independent numeric coverage for the implemented cost
strategy. Unreachable restoration alone is valid partial recovery. P09 explicit
hard-deadline infeasibility remains a separate fixture, not a product setting.

## Implemented consumption-priority proof

[Consumption scenarios](../docs/development/CONSUMPTION-STRATEGY.md#regression-evidence)
separate gross imported energy, hypothetical replacement and monetary objectives.
The independent cases cover price-versus-import decisions, finite capped bonuses,
losses across single-network/shared-AC/multi-inverter models, per-battery deficits,
reserve precedence and publication rejection for unproved or inconsistent primary
results. Real HTTP tests hold a replacement calculation open while checking the
old plan/history and subsequent HA acknowledgement. Strategy persistence is tested
on all four database backends, with desktop/mobile browser save/reload checks.
