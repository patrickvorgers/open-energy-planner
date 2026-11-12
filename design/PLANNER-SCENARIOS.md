# Planner scenarios: Open Energy Planner

Platform evidence: [container validation](https://github.com/patrickvorgers/open-energy-planner/blob/3a564d4c9679b917f81ebb5ac269c9838ae56fe5/solver-experiment/CONTAINER-VALIDATION.md). Historical selected suite: 165 checks on Windows AMD64, Linux AMD64 and emulated ARM64. Native ARM and HA app release gates remain open.

These scenarios elaborate the [requirements](REQUIREMENTS.md) and [technical design](TECHNICAL-DESIGN.md). They are written independently for this project. External projects inform coverage categories only. Their fixtures, test code, numerical inputs and expected results are not reproduced. See [research and coverage](TEST-COVERAGE.md).

The four core cases below have complete numerical inputs, candidate schedules, independently derived optimal values and acceptance checks. Companion cases extend their boundaries. The wider coverage catalogue is planned work, not a claim that executable tests already exist.

## 1. Common contract

Unless overridden explicitly:

- Optimization strategy is `minimize cost`. The separate `minimize consumption` strategy first minimizes gross imported kWh and then minimizes economic costs among equally minimal-import plans. Do not expect the arbitrage and negative-price imports below under that objective. See P37–P39 in the coverage catalogue for the strategy checks.

- Time starts at `2026-02-03T00:00:00Z`. Each interval is 15 minutes, duration `0.25 h`. All values in a table are constant over their interval. Small horizons isolate the calculation and do not replace the product's 48-hour horizon.
- Use EUR, kWh and W. Prices are effective import/export prices in EUR/kWh, already including any applicable contract formula. No additional tax, fixed fee or bonus is implicit.
- Positive grid power means import. Positive battery power means discharge. Energy volumes for each direction are non-negative. A negative export price means paying to export.
- Battery energy is internal stored energy. `E_next = E + eta_charge * charge_AC - discharge_AC / eta_discharge`. Every conversion is counted once. Power limits refer to the AC battery connection unless a case explicitly uses DC routes.
- One-way limits of 4000 W permit at most 1 kWh per interval. Battery minimum energy is 0 kWh. No simultaneous charging/discharging of the same battery. No simultaneous import/export at the same modeled grid connection. This does not prohibit historical meter readings containing both directions within an interval, or all possible transfers between different batteries.
- No unmodeled energy sink, artificial load, invented production or unbounded circulation is permitted. PV may be curtailed only where explicitly authorized. All demand must be met by allowed routes.
- Battery wear defaults to zero. When enabled: `wear_cost = wear_cost_per_kwh * internal_discharged_energy`. Charging is not charged again. Capacity and minimum SoC do not scale this rate.
- Cash cost `C = import_cost - export_income - bonus + wear_cost`. A negative cost is net income. Planning objective `J = C - V(E_end)`. The initial stock value is a constant for a planning run and does not change its optimum.
- Financial evaluation is different: `C_eval = C + V_start - V_end`, with the separately recorded hardware-stock adjustments where applicable. `J` is not a realized savings figure. Net cash and stock value are always reported separately.
- Terminal functions supplied by these fixtures isolate planner behavior. They are not a hidden decision about how production derives terminal prices beyond available forecasts. That method remains to be validated separately.
- Each case expects a valid optimum unless its status is explicitly different. Check optimality status and an independently recomputed cost. A successful return or a finite objective alone is insufficient.

### Numerical acceptance

Use exact decimal/rational arithmetic for the reference calculations below. Proposed initial assertion tolerances: `1e-6 kWh` for energy and `1e-6 EUR` for these small-case costs. These are test-proposal tolerances, not permission to violate physical limits or production defaults. Record the actual solver feasibility/optimality settings and justify any change.

The published whole-W command must also remain executable. A non-integer-W optimum may differ after command conversion. Test the converted plan against an independently feasible bound instead of pretending rounding has no effect. The four base cases have whole-W schedules. Do not weaken an assertion automatically when a dependency update fails it.

## 2. S01: Buy low, sell high

Requirements: BAT-03, BAT-12, SOLVER-02/04, DEV-10/12.

### Inputs

- One battery: capacity 1 kWh, initial energy 0 kWh, charge/discharge limits 4000 W, both efficiencies 1.
- Grid: import/export limits 4000 W. No PV, household load, wear or bonus. Terminal value `V(E) = 0`.

| Interval | Buy price | Sell price | Household energy | PV energy |
|---|---:|---:|---:|---:|
| t0 | 0.12 | 0.04 | 0 | 0 |
| t1 | 0.50 | 0.40 | 0 | 0 |

### Expected optimum

| Interval | Grid import | Grid export | Battery charge AC | Battery discharge AC | End energy | Battery command |
|---|---:|---:|---:|---:|---:|---:|
| t0 | 1 | 0 | 1 | 0 | 1 | -4000 W |
| t1 | 0 | 1 | 0 | 1 | 0 | +4000 W |

`C = 1 * 0.12 - 1 * 0.40 = -0.28 EUR`. `J = -0.28 EUR`.

Proof: each purchased kWh carried from t0 to t1 earns 0.28 EUR. At most 1 kWh can traverse the route because of capacity and both interval limits. Maximum transfer therefore attains the lower bound `C >= -0.28`. Other allowed purchases/sales cannot improve it. The economic energy schedule is unique. Internal solver variables need not be.

The self-consumption reference has no household demand or PV, never grid-charges for arbitrage, and remains empty. Its cost is 0 EUR. For this isolated case, optimized savings are 0.28 EUR with equal start/end stock value.

### Companion cases

| ID | Change | Independently expected result |
|---|---|---|
| S01-L | Charge efficiency 0.80. Discharge efficiency 0.90 | Import 1 at t0. Store 0.80. Export 0.72 at t1. Cost `0.12 - 0.72*0.40 = -0.168 EUR`. Discharge command +2880 W. |
| S01-W | Original efficiencies. Wear rate 0.35 EUR per internal kWh discharged | Discharging the stored 1 kWh would cost `-0.28 + 0.35 = +0.07 EUR`. Optimal throughput and cost are zero. |
| S01-E | Original efficiencies. Wear rate exactly 0.28 EUR per internal kWh discharged | Every feasible discharge quantity from 0 to 1 kWh has cost zero. Assert the optimum and invariants, not a unique schedule. No unagreed tie-break preference. |
| S01-P | Original inputs. Battery charge limit 2000 W | Charge/export 0.50 kWh. Cost -0.14 EUR. Capacity alone must not determine throughput. |
| S01-G | Original inputs. Grid import limit 1000 W | Charge/export 0.25 kWh. Cost -0.07 EUR. |
| S01-R | Initial energy 0.40. Hard minimum 0.40. Capacity 1.40 kWh | Use the additional 1 kWh as in S01. Finish at 0.40. Cost -0.28 EUR. Never discharge the reserve. |

## 3. S02: Negative import prices without fictitious consumption

Requirements: MODEL-01, PV-04, BAT-12, SOLVER-04.

### Inputs

One interval. Battery capacity 1 kWh, initially empty, 4000 W limits and unit efficiencies. Grid limits 4000 W. Buy price -0.16 EUR/kWh. Sell price -0.24 EUR/kWh. No PV, household demand, wear or bonus. Terminal value zero for this isolated case.

### Expected optimum

Import and charge exactly 1 kWh. Finish with 1 kWh. Battery command -4000 W. Export and discharge are zero.

`C = -0.16 EUR`. `V_end = 0`. `J = -0.16 EUR`.

Proof: import earns 0.16 EUR/kWh, but only the 1 kWh of free battery capacity is an allowed sink. No route can consume additional energy. Filling the battery attains the bound `C >= -0.16`. Simultaneously cycling energy, labeling a disabled load as consumption, or importing more than the connection allows must fail validation.

This fixture deliberately assigns zero terminal value. It proves physical boundedness at a negative price, not the production valuation policy.

### Companion cases

| ID | Inputs differing from S02 | Independently expected result |
|---|---|---|
| S02-F | Battery initially full | No import, no throughput, cost zero. A full battery is not an unlimited negative-price sink. |
| S02-N | Remove the battery. No other loads | No import/export and zero cost. No SoC input required. |
| S02-PV | No battery. PV available 1 kWh. Load 0.40 kWh. PV supports only full on/off. Buy +0.20, sell -0.08 | Keep PV on. Consume 0.40, export 0.60. Cost +0.048 EUR. Switching off would cost +0.080 EUR. Negative export price alone does not justify shutdown. |
| S02-OFF | S02-PV but buy price +0.10 | Switch PV off and import 0.40. Cost +0.040 EUR, below +0.048 EUR with PV on. |
| S02-NC | S02-OFF but PV cannot be controlled | PV stays on. Export 0.60. Cost +0.048 EUR. Authorization is a hard boundary. |

Partial PV output is forbidden in these on/off companions. A separate controllable-power fixture may allow partial curtailment. Do not quietly substitute one capability for the other.

Confirmed reference policy: PV is not curtailed in response to prices. With no battery in these companions, the reference consumes 0.40 kWh and exports 0.60 kWh. Its cost is 0.048 EUR. Thus S02-PV has zero savings, S02-OFF saves `0.048 - 0.040 = 0.008 EUR`, and S02-NC has zero savings. These are synthetic deterministic comparisons. Real available-PV estimates must retain their quality label.

## 4. S03: A partially used, chronological bonus cap

Requirements: PRICE-06/08/09/10/11, UI-14/15, SOLVER-04.

### Synthetic rule and inputs

This is an invented test contract, not a claim about Zonneplan or NextEnergy terms.

- Two intervals. No battery or household load. PV energy available is 1 kWh each interval. Continuous PV curtailment from 0 to 4000 W is explicitly permitted. Grid export limit 4000 W. Import cannot create an allowed load.
- Base export prices: t0 = -0.03 EUR/kWh. T1 = -0.07 EUR/kWh.
- Bonus: +0.13 EUR per eligible direct-PV-export kWh, on the first 2 kWh in this period. Previously used qualifying volume is 1.60 kWh. 0.40 remains.
- For this rule the stored counter is capped credited volume: `b_t = min(eligible_export_t, max(0, 2 - q_t))`. `q_next = q_t + b_t`. A different rule may count gross volume, but must say so explicitly.
- Eligibility and counter advancement are chronological. A candidate plan reads the initial counter. Planning does not update the actual realized counter.

### Expected optimum

| Interval | Direct PV export | PV curtailment | Credited bonus volume | End credited counter |
|---|---:|---:|---:|---:|
| t0 | 0.40 | 0.60 | 0.40 | 2.00 |
| t1 | 0 | 1.00 | 0 | 2.00 |

PV command: 1600 W at t0 and 0 W at t1. Base export cost = `0.40 * 0.03 = 0.012 EUR`. Bonus = `0.40 * 0.13 = 0.052 EUR`. Total `C = J = -0.040 EUR`.

Proof: the remaining 0.40 kWh earns a net 0.10 EUR/kWh at t0 and only 0.06 at t1. Beyond the cap all export incurs a charge. Maximum net benefit is therefore `0.40*0.10 = 0.040 EUR`, achieved by the stated schedule. Do not pay the bonus on curtailed energy or reset the cap between intervals.

### Companion cases

| ID | Change | Independently expected result |
|---|---|---|
| S03-F | Initial credited counter 2.00 | No bonus remains. Curtail all PV. Cost zero. |
| S03-Z | Initial credited counter zero | Export both kWh, consume full cap. Base cost 0.10, bonus 0.26. Cost -0.16 EUR. |
| S03-ORDER | No curtailment permitted. Initial counter 1.60. Sell prices t0=-0.03, t1=+0.05 | Export 1 kWh both intervals. Credit only first 0.40 at t0. Cost `0.03 - 0.05 - 0.052 = -0.072 EUR`. The solver cannot assign the first-volume bonus to a later interval instead. |
| S03-ROUTE | No PV. Initially stored battery energy, export price -0.03, direct-PV-only bonus | Battery export earns no bonus. Retain energy. An energy label must not make stored energy newly generated PV. |
| S03-REPEAT | Evaluate the original case twice from the same input snapshot | Same optimum. Actual stored counter remains 1.60 until qualifying realization is ingested. |

The explicit counter semantics above are fixture inputs, not permission to assume every real supplier uses this definition. Contract-year reset, gross-volume counting and other eligibility definitions need their own cases in the catalogue.

The confirmed reference performs no price-driven curtailment. It therefore exports 1 kWh in each interval, credits 0.40 kWh of bonus in t0 and finishes with a credited counter of 2.00. Reference cost is `0.03 + 0.07 - 0.40*0.13 = 0.048 EUR`. Optimized savings are `0.048 - (-0.040) = 0.088 EUR`. There is no battery-stock difference in this case. Both scenarios start with the same remaining bonus budget, then advance their own counters.

## 5. S04: Preserve valuable energy beyond the horizon

Requirements: HORIZON-06/07/09, UI-05/12/13, SOLVER-04.

### Inputs

- Two intervals. Battery capacity/initial energy 1 kWh, minimum zero, efficiencies 1, limits 4000 W. No PV, load, wear or bonus.
- Buy price both intervals 0.30 EUR/kWh. Sell prices t0 = 0.10 and t1 = 0.14 EUR/kWh.
- Terminal function supplied to the test: `V(E_end) = 0.28 * E_end`, with sufficient assumed subsequent feasible use for the whole 1 kWh. This supplied function is not an API forecast beyond its coverage.

### Expected optimum

Hold 1 kWh throughout. Both battery commands 0 W. Cash cost zero. Terminal value 0.28 EUR. Planning objective `J = -0.28 EUR`.

Proof: exporting a kWh earns at most 0.14 EUR but forfeits 0.28 EUR of terminal value. Importing costs 0.30 EUR per kWh, so replenishment cannot improve the result. Retention is optimal. With the same start/end stock value, this period's evaluated cost is zero, not a realized gain of 0.28 EUR.

### Companion cases

| ID | Change | Independently expected result |
|---|---|---|
| S04-NOV | Set terminal function to zero, for comparison only | Export 1 at t1. Cash cost/objective -0.14 EUR. Final energy zero. This isolates the unwanted horizon-end depletion. |
| S04-LIM | `V(E)=0.28*min(E,0.25)+0.06*max(E-0.25,0)` | Retain 0.25 and export 0.75 at t1. Cash cost -0.105, terminal value 0.070, objective -0.175 EUR. Command +3000 W at t1. Do not value all stock at the best marginal price. |
| S04-LOSS | Original linear terminal function defined by later AC use at 0.28 EUR/kWh, but discharge efficiency 0.90 | Internal terminal value is 0.252 EUR/kWh. Retain all. Apply the discharge loss once, not twice. |
| S04-SHORT | Published price coverage ends at t1. No additional forecast | Keep the supplied terminal method, flag shortened horizon, invent no missing zero-price intervals. Same optimization result as S04 with that input. |
| S04-COUNT | Present the future use used in terminal value as an explicit later in-horizon slot instead | Remove that same use from residual value. Total economic benefit must not be credited twice. Exact numerical fixture to be expanded before implementation. |

## 6. Independent oracle and release gate

### R01: Reserve recovery precedes both strategies

Two quarter-hour intervals. No PV or household load. One 2-kWh battery initially contains 0.50 kWh. The configured minimum is 1 kWh and mandatory recovery is enabled. Charge limit 2000 W, grid import limit 4000 W, unit efficiency, no wear/bonus, zero terminal value. Buy prices are 0.50 and 0.10 EUR/kWh. Sell price is zero in both intervals.

Expected for either strategy: charge 0.50 kWh in t0 (command -2000 W), reach the reserve at t0's end, then hold 1 kWh with command 0 W. Imported energy is 0.50 kWh and cost is 0.25 EUR. Waiting until t1 would cost 0.05 EUR but violates the confirmed earliest-feasible recovery priority. Charging more than 0.50 kWh is neither required for recovery nor beneficial under the fixture's prices/terminal value. The measured initial state below the reserve must not cause false infeasibility.

The charge limit makes the first interval's end the earliest feasible recovery time. No assumption about subinterval switching is needed. This numerical recovery example supplements the four initial economic cases and P07. Without mandatory recovery the behavior must instead follow the chosen objective and its ordinary bounds. Do not apply the recovery override unconditionally.

### Oracle requirements

For each executable planner scenario, retain:

1. Stable case ID, purpose, requirements and all physical/contract/terminal inputs.
2. Frozen UTC intervals and explicit duration. No live weather, price service, wall clock or production HA.
3. A candidate schedule and hand proof, or exhaustive enumeration with a stated finite domain and resolution. Enumeration of a discretized grid alone does not prove a continuous optimum.
4. Expected feasibility, optimum or proven bound, and an explicit uniqueness statement.
5. Recomputed energy, SoC, per-route losses, cash components, bonus counters and terminal value.
6. Adapter status checks and the applicable numerical tolerances.
7. Counterexample mutations that the independent validator must reject: excessive SoC, excess power, missing loss, duplicate bonus, fictitious energy or a rounded command violating a limit.

Do not call the model under test to generate its own expected answer. Do not approve a snapshot merely because a solver/library changed. Multiple optimal schedules are acceptable unless an independently specified tie-break is being tested. A secondary preference may not worsen primary economic cost.

All enabled first-release scenario groups must actually run in required CI. Unavailable solver dependencies, missing expected results or silent skips must block release rather than appear as passed scenario coverage. Keep performance measurements separate from correctness gates. No hardware-independent time threshold is claimed yet.

## 7. Independent validation

The independent [arithmetic audit](scenario-arithmetic-audit.json) retains 30 exact checks and 30,906 candidate evaluations. Finite enumeration supplements the continuous proofs. It is not the solver's own expected answer.

The [maintained regression contracts](SOLVER-REGRESSION-MIGRATION.md) cover horizon energy, shared grid limits, converter losses, stable identities, whole-watt commands and independent costs. Component/scenario isolation and native status/error boundaries have separate tests. Hand-fixed reference traces validate specific scenarios. Automatic reference behavior is covered by the savings suites. See [coverage contracts](TEST-COVERAGE.md) for the wider product acceptance boundary.

## Application publication acceptance: implemented, separate from optimizer oracles

The current [planning/publication tests](../tests/app/planning/test_planning_pipeline.py)
retain independently derived -.28 EUR and -.34 EUR cycles, converter/cell losses
and integer-watt capacity boundaries. Temporal/IO faults check that invalid,
stale or unpersisted candidates do not replace accepted plans. The
[solver regression contracts](SOLVER-REGRESSION-MIGRATION.md) define compatible numerical and physical expectations. These are synthetic acceptance outcomes, not measured household savings.
