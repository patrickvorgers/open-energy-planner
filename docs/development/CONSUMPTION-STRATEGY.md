# Consumption-priority planning

The app supports two persistent manual strategies: **Minimize cost** (default)
and **Minimize consumption**. Configure the choice in Installation settings.
There is no HA strategy selector or automatic strategy switching.

## Objectives and the rolling horizon

A rolling 48-hour horizon moves the final interval but does not remove its
boundary. Treating starting battery energy as free can manufacture lower import
by leaving an empty battery just outside the calculation.

Consumption planning uses these lexicographic priorities:

1. Restore mandatory minimum reserves at the earliest feasible interval ends,
   using the existing reserve-recovery policy and all physical limits.
2. Minimize **corrected grid import**, in grid-side kWh:

   ```text
   gross grid import within the horizon
   + sum over batteries(
       max(0, initial internal stock - final internal stock)
       / grid-to-internal charging efficiency
     )
   ```

3. Fix the proved primary optimum, then minimize the existing complete monetary
   objective, including effective tariffs, bonuses, wear and terminal inventory
   value. The financial objective and its bound remain currency-valued.

There is no arbitrary conversion weight between money and kWh. Export cannot
offset import. Each battery's deficit is evaluated separately. A surplus on a
second battery does not cancel it. Surplus stock receives no primary credit,
so negative prices or high financial terminal value cannot buy extra imports.

For a configured installation, charging efficiency is cell charge efficiency ×
battery DC connection efficiency × inverter efficiency. Single-network models
use cell × grid-converter efficiency. Shared-AC models use cell × that battery's
AC charger efficiency. No discharge loss is applied again: validated end stock
already reflects discharge losses. Direct PV/DC charging retains its physical
efficiency advantage in the energy balance, but hypothetical replacement is
always measured from the grid boundary.

Example: ending with 0.8 internal kWh less than the start, with 80% total charging
efficiency, adds 1 kWh to the primary objective. This is **not** an instruction to
charge that kWh before the plan ends. No hard final-SoC target is added. A
lossless battery may still end lower when corrected primary results tie and
the financial objective prefers it. That depletion is not reported as free
primary-objective improvement.

This is deliberately conservative. It assumes grid replacement rather than
predicting free sunshine after the horizon, so it can preserve more starting
stock than hindsight needs. Every new plan uses its own measured initial stock.
The correction is local planning evidence, never summed as realized import or
retrospective savings. Actual gross import and financial savings retain their
existing independent accounting. The monetary terminal valuation is unchanged.

## Solver and independent checks

The same HiGHS model/adapter handles both strategies. Reserve recovery, primary
kWh optimization and financial tie-breaking share the existing native time
budget. The parent process retains its existing bounded timeout.

`CONSUMPTION_PRIORITY_TOLERANCE_KWH` is 1e-8 kWh and is defined once. The primary
MIP absolute gap and its fixed optimum row use that precision. A primary stage
that times out or has an unproved optimum returns no publishable candidate.
An interrupted financial stage may publish a validated feasible result with
explicit `feasible` status. This does not claim the cheapest tie-breaker.

Independent physical/financial validation remains mandatory. A separate pure
evaluator recalculates real import and replacement from detached end stocks.
Publication checks the reported primary optimum within the documented solver
precision plus a 1e-7 kWh numerical validation allowance. It checks arithmetic
consistency, not a second mathematical proof of global optimality. Independent
small scenario oracles validate the chosen decisions.

Each consumption plan freezes `consumption_objective`: gross grid import,
hypothetical terminal replacement, corrected import, the proved primary optimum,
tolerance and method. Overview shows the breakdown with the selected original
plan. Repricing an archived plan does not rewrite it or its strategy.

## Persistence and activation

The requested choice lives in the `planning` document namespace of the shared
active SQLAlchemy database. SQLite, PostgreSQL, MariaDB and MySQL use the same
store. Complete backend transfer includes it. No secrets or connection settings
are stored there. Unreadable strategy settings explicitly block new planning
until a valid manual choice is saved.

Authenticated `GET /v1/strategy` exposes requested, published and active strategy.
`PUT /v1/strategy` accepts `strategy` and `expected_strategy` for compare-and-set.
Strategy saves do not alter the physical configuration revision, discard fresh
measurements, reset reference simulation, or rewrite history/counters. They
request an immediate complete replacement round. An in-flight candidate for
the previous strategy cannot replace it.

An existing published plan retains only its normal validity until replacement.
Failed calculation does not extend it. Active strategy is taken from the last
HA-acknowledged plan while enabled and unexpired. A newly published plan need
not yet be accepted by HA. Acceptance never proves physical execution. No
integration protocol change or additional HA command entity is necessary.
Revoking a plan or changing its physical configuration clears active strategy.
An old acknowledgement cannot reinstate invalid commands. Archive reads run
off-loop under the app lock and unavailable/corrupt evidence returns HTTP 503.
The settings page refreshes acceptance status automatically without overwriting
an unsaved selection or its compare-and-set baseline. A failed auxiliary strategy
read does not prevent Energy setup/discovery from continuing.

## Regression evidence

`tests/app/planning/test_consumption_strategy.py` covers negative prices, equal-import price
arbitrage, stock depletion, in-horizon solar replacement, converter losses on
all three model types, multiple batteries, capped bonuses, financial terminal
value, mandatory reserve precedence, unproved primary results and inconsistent
reported objective evidence. The real worker test retains the 120-step horizon.
`tests/app/planning/test_live_app.py` checks delayed manual activation, unchanged old commands
and archived plans, stale-write rejection, and separate HA acknowledgement.
Database persistence runs on all four real backends. Browser tests save/reload
the choice at desktop and mobile sizes.
