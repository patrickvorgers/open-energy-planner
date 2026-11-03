# Battery reserve recovery

BAT-05/BAT-06 allow a real measured state below the configured reserve. OEP
retains that measurement and the reserve independently. It does not raise the
measurement, lower the reserve or reject an otherwise usable installation.

Each battery has **Restore minimum reserve immediately** (`recover_reserve`,
default false) in the app-owned wizard. This is a planning permission, not a
physical protection setting. HA automations and local equipment retain physical
control and safety. Device membership still comes exclusively from Energy.

## Physical invariant

Below reserve, the battery can idle or charge, but cannot discharge. Partial
recovery cannot be undone. Once its reserve is reached, every subsequent planned
state must remain at or above it. Independent validation and the pre-publication
measurement gate apply this same invariant using energy conservation, without
importing native solver constraints. All capacity, charge/discharge, converter,
AC/DC efficiency, shared-inverter and grid limits continue to apply.

With immediate recovery **off**, the financial strategy chooses when to charge.
With it **on**, the planner first minimizes total missing reserve energy at the
end of the first planning interval, fixes that proved minimum, and repeats for
successive intervals until all participating batteries have recovered or the
horizon ends. Then it optimizes the existing financial objective within those
constraints. There is no monetary penalty that a sufficiently favourable price
could outweigh. Both `minimize cost` and `minimize consumption` now run after
these mandatory priorities. See [consumption strategy](CONSUMPTION-STRATEGY.md).

Recovery uses an absolute numerical proof/fixation tolerance of `1e-8 kWh`.
It must not spend the shared budget proving distinctions below that precision.
The original financial gap setting is restored before cost optimization.
Once all reserves are restored, explicitly bound subsequent energy stocks by
that reserve (within the same numerical tolerance). This is already implied by
the no-discharge invariant, and gives native presolve a stronger formulation.

For example, a lossless battery at 0 kWh with a 1 kWh reserve and a 1 kW charging
limit recovers to 0.25, 0.50, 0.75 and 1 kWh over four quarters. A cheaper later
quarter cannot postpone that trajectory when recovery is enabled. Household
load consumes grid headroom first through the ordinary hard balance constraints.
Unavailable headroom can make recovery partial or impossible. A remaining
shortfall alone is not physical infeasibility: an idle battery below reserve is
valid. An impossible household balance remains infeasible.

The priority is aggregate missing internal kWh across enabled, initially
below-reserve batteries. Shared power limits can produce multiple equally early
allocations. Financial optimization breaks those ties. This is not a per-battery
fairness guarantee. Recovery means earliest feasible **interval-end** stocks at
the configured planning resolution, not a fast local regulator or a new deadline
setting. Equal-power control groups and the mixed-resolution horizon remain in
force.

## Solver ownership and bounded work

`optimization/adapters/highs/recovery.py` adds the priority stages to the same
isolated native model. The original financial objective is restored on every
exit. All stages and the final financial solve share the configured solve-time
budget. A reserve stage must prove its optimum before financial optimization.
An interrupted/unproved priority returns no publishable candidate. An existing
accepted plan survives only within its normal validity rules. Financial result
status and bound describe the financial problem subject to the proved recovery
profile, not an unconstrained cost optimum. The separate worker timeout remains
the final process bound.

The automatic shared native budget is 120 seconds. It starts before model
construction and is shared by every priority stage and the financial solve.
The parent-worker deadline is 125 seconds, allowing five additional seconds for
process startup and result transport. EV work reserves another five seconds for
HA handover, giving a 130-second dispatch lead.

The HA OS test installation intermittently exhausted the previous 30-second
budget. The larger ceiling gives difficult inputs and slower machines more time.
It does not delay a solve that finishes earlier. The exact cause of those
timeouts has not been established. No Raspberry Pi runtime guarantee follows
from this policy, and a larger budget does not guarantee a feasible candidate
or an optimality proof. HiGHS still uses one thread. These defaults have one
owner in the domain/worker policy. Explicit tighter caller budgets, including
historical replay and shadow comparisons, remain unchanged. Input freshness,
publication validity and the quarter/five-minute planning cadence are unchanged.
The HTTP API and HA synchronization remain asynchronous. Tune further only
using measured construction/solve times, termination reasons and candidate
validation on representative hardware.

The final financial stage permits a relative MIP gap of 0.005 (0.5 percent).
Recovery, appliance service, EV service and minimum-consumption priorities keep
their existing precision. The financial tolerance is applied only after these
stages finish. The native dual bound is retained. An optimal status means
optimal within configured tolerances, not necessarily an exact best objective.
The relative gap compares the incumbent objective with the best bound using
the absolute incumbent as denominator. Near zero it may offer little stopping
relief. It concerns modeled cost, including wear and terminal battery value,
not a guarantee on a future energy bill. The 120-second ceiling still applies.
A financial timeout can retain an independently validated feasible candidate
without claiming the 0.5-percent bound. Without a candidate, or with an unproved
priority, no new plan is published. Existing plan validity is not extended.

HiGHS 1.15.1's [MIP timer is local to each run](https://github.com/ERGO-Code/HiGHS/blob/v1.15.1/highs/mip/HighsMipSolver.cpp#L88-L94),
although `getRunTime()` reports cumulative runtime. Each new run receives only
the remaining wall-clock budget. Adding previously spent time would extend the
total. A multi-stage measured regression guards this distinction.

After proving the priorities, seed the financial run explicitly with that feasible
column solution. Adding the final priority row otherwise invalidates automatic
incumbent reuse. A short financial search can then retain known feasible recovery
instead of rediscovering a first candidate. Its result still requires independent
validation and is labelled feasible, not optimal, if interrupted.

The battery's existing charge/discharge direction variable prevents discharge
below reserve. Terminal valuation remains zero for energy at or below reserve.
The native valuation formulation also permits a final stock below reserve when
full restoration is impossible. Independent valuation recomputes the accessible
amount from the returned physical stocks.

## Visibility, history and compatibility

The setup view and each HA battery device expose **Reserve status**:

| Raw state | English label | Meaning |
|---|---|---|
| `measurement_unavailable` | Waiting for usable measurements | No current usable planning snapshot. Do not reuse an old SoC |
| `reserve_satisfied` | Reserve satisfied | Current measured energy is at or above the configured reserve |
| `recovery_required` | Recovery required | Below reserve with immediate recovery enabled |
| `below_reserve` | Below reserve | Below reserve. Charging timing follows the financial strategy |

The diagnostic **Mandatory reserve recovery** setting is a separate yes/no
entity. These are app-derived diagnostics, readable without an active command.
They do not assert that physical equipment is charging. The status becomes
unknown-to-the-app when required inputs are unavailable. Every catalogue read
rechecks snapshot validity: expired or future-dated cached measurements cannot
continue to report a usable reserve status. HA uses explicit enum
translations while automation-facing raw states remain stable. Below-reserve
intervals publish fixed-power commands, retaining the calculated charge/idle
target instead of replacing it with a zero-grid follower. After restoration,
the existing permission-gated zero-grid selection can be used again.

The setup wizard labels its status as **at last refresh**: reopening/reloading
settings updates that snapshot without replacing edits in progress. The HA
diagnostic updates during the normal integration synchronization.

Settings and immutable input snapshots retain each battery's policy, actual
initial energy and configured reserve. False is omitted from canonical
installation settings so old default configurations retain their version hashes.
Original plans and their financial evidence are never recomputed or rewritten.
The single active SQLAlchemy database stores the setting on SQLite, PostgreSQL,
MariaDB and MySQL. Existing whole-database transfer includes it.

The continuous self-consumption **reference** retains its existing behaviour:
maximal PV production, no active grid charging, discharge only above reserve.
Mandatory EMS recovery does not turn this reference into an optimized charger.
Both scenarios retain independent real battery inventories.

## Regression proof

`tests/app/installation/test_reserve_recovery.py` contains independent numeric oracles for enabled
versus optional recovery, expensive-first charging, power/grid headroom, several
batteries sharing a converter, direct-DC losses, post-recovery arbitrage,
unreachable recovery, zero accessible terminal value, and the complete 120-step
horizon. It also checks deadline/status failure, original-objective restoration,
fixed commands and unchanged reference dispatch. Legacy scalar/shared-AC model
paths use the same policy.

`tests/app/installation/test_installation_app.py` proves real HTTP bootstrap from measured SoC
below reserve through validated commands and consistent setup/snapshot/status
responses. `tests/app/planning/test_publication.py` proves charging/idle admission and rejects
further discharge using updated measurements. Real HA entity/translation tests
cover readable settings/status independent of command expiry. Desktop/mobile
Playwright covers policy editing and retention. Typed installation-store tests
run on all four actual database backends. These are simulation/software proofs,
not physical battery or automation acceptance.
