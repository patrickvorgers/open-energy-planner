# Plan evidence and decision explanations

The app provides two distinct layers. Neither uses generated narratives or a
language model. All statements refer to the selected original plan, not today's
configuration, revised forecasts or corrected contract prices.

## Layer 1: recorded inputs, actions and limits

Implemented by `savings/evidence.py`, exposed on the authenticated original-plan
comparison response, and rendered in **Planning > Understand this plan**.
Planning first requests `comparison?view=plan`, then `comparison?view=simulations`.
The first projection includes only the original chart, command and interval
inputs. It omits archived forecast/training evidence, source bindings and replay
results, and skips evidence construction, scenario replay and repricing. The second
omits the repeated plan and evidence while retaining the original financial basis.
Only opening the evidence disclosure requests `comparison?view=evidence`, which
returns the plan identity and evidence without scenario replay or repricing.
The unfiltered comparison response and financial period queries retain their
existing contracts. JSON encoding runs outside the application event loop.
The complete original archive remains unchanged and available through its
authenticated original-plan route. Closed original-plan detail tables are built
only when opened, using the latest confirmed plan and supplemental comparison.
An already open table follows confirmed refreshes and plan selection.
Savings reads start only when Savings opens or its period is explicitly refreshed.
Opening Savings directly does not request a plan list or replay comparisons.
Source-name changes detected on Savings mark the retained Planning labels stale.
Reopening Planning refreshes its selected plan without reading hidden Savings.
Only a successful plan render confirms that name generation. Failed refreshes
retain the prior plan and retry state, including historical selections and
manual chart visibility choices.
Each read has its own deadline. Selection generations reject late plan-list and
simulation responses. Evidence belongs to its active shared read and displayed
plan, so a failed refresh retaining that plan can still complete its evidence.
A simulation or evidence failure leaves the displayed
plan usable. Evidence reads are deduplicated for the displayed plan and can be
retried by reopening the disclosure after failure.
The coordinated load waits for simulation and already pending disclosed evidence
reads before releasing shared loading state. Graph rendering remains progressive.
Successful original-plan selection is independent of supplemental availability,
whose retry state does not mark a linked plan as missing. Supplemental failure
guidance uses canonical text for network and rendering failures alike.
The primary view covers the **whole original planning period**, with explicit
start/end/duration, total forecast demand/PV, per-source forecast model/quality,
total planned grid exchange and per-battery charge/discharge and start/final SoC.
These totals are not the values for the first selected interval. The original
household and available-solar forecasts also have their own time chart, alongside
the original spot-market/import/export price chart. Mean kW describes the rate during each block. kWh is
an energy quantity over the explicitly stated interval or whole horizon.

Open **Selected interval details**, use the keyboard-accessible time slider or
select a graph bar for a drill-down. Its header shows exact bounds/duration and
states that energy quantities apply only to that interval. Cards show original
household/PV forecasts, published/forecast price status,
effective import/export prices before conditional bonuses, planned grid exchange,
per-battery DC charging/discharging and end SoC, PV use/curtailment permission,
and shared inverter AC limits/efficiency. Prepared bonus rates, period caps,
counters and recorded credited volumes remain separate from base prices.

Forecast provenance comes from the plan's own `demand_evidence` and per-source
`solar_evidence`, not the currently trained model or a newer shadow candidate.
Distinguish annual-estimate startup profiles, learned weekday/hour profiles,
ridge ML regression, physical PVWatts and recorded learned solar corrections.
Household demand includes baseload. There is no invented separate baseload
forecast. If an older plan omitted provenance, show **Not recorded**. Solar
correction candidates currently evaluated in shadow mode must not be presented
as the physical forecast actually used by the planner.

Expandable plan assumptions show strategy, solver quality, original contract
version, starting battery energy/reserve, cell losses, wear and the bounded
remaining-energy estimate. Original expected financial components remain
separate, including inventory value, which is not cash income.
The battery wear source coefficient retains its original numeric precision. Its
projected fact uses the owned `key: wear_cost_per_kwh`, so presentation never
identifies it by label text. Other money and rates, including remaining-energy
valuation, retain the ordinary two-decimal display. This projection does not
modify archived inputs or calculated amounts.

A reached charge/discharge, grid or converter rating is an observation. So are
ending at reserve/capacity and a disabled PV command. They do **not** prove that
the bound caused the solver's choice. A binding constraint can have no economic
impact. Several solutions can be equally good. Feasible financial results are
never presented as proven optimal.

For example, “Charge power limit reached” is supported by terminal energy divided
by actual interval duration. “The rest is sold because the battery cannot charge
faster” is not asserted by this layer. Shared AC-bus flows do not uniquely identify
whether PV or imported electricity supplied a specific battery. The UI does not
invent such provenance. Forecast energy, planned outcomes and measured realization
remain distinct.

Missing fields are **Not recorded**, never zero. Missing whole intervals retain
their original timeline position. Incomplete snapshots show an explicit evidence
issue. Evidence reads do not solve, publish commands, change strategy or write
new plan/evaluation versions. Existing immutable plan documents already retain
the necessary source facts, so no database schema or archival rewrite is needed.
The response names its projection version (`plan-facts-v1`) and original plan ID.

## Reference balances and original price inputs

Planning shows **Optimizer**, **Battery self-consumption** and **Without battery: Baseline**
in that order. They share time ranges, units, flow colors and a kW scale.
Value-axis bounds combine visible signed stacks across all three charts. The without-battery chart uses the same frozen
household and available-solar forecasts but removes storage.
PV serves demand, exports surplus and imports deficits. Converter efficiency,
physical clipping and grid ratings still apply. A physically impossible reference
is explicitly unavailable. No flow or price curtailment is invented to hide it.

**Battery self-consumption** uses the plan's recorded starting stock and bonus
budgets for new horizon comparisons. Legacy original comparisons retain their
recorded reference start. The separate retrospective Savings reference carries
its own stock and counters across time. Reading charts never resets that state. The no-battery forecast uses
the plan's recorded initial bonus budgets. It is a horizon comparison, not a new
continuous lifetime settlement scenario. The Savings screen continues to evaluate
the established continuous self-consumption reference and labels that policy.

Both battery charts show expected economic cost and benefit against the same
without-battery baseline. Optimizer benefit includes storage and smart planning.
Self-consumption benefit shows storage without price optimization. Their benefits
are alternatives, not additive. Neither includes investment costs. Inventory changes remain separate from grid cash. Original costs are used
for both graphs. Corrected cost evaluations remain separately available for the
continuous self-consumption reference. Do not silently mix original and corrected
reference/EMS costs.

These reference results are computed in the app from frozen inputs without solver
calls. Original saved self-consumption outcomes/costs are preserved. Physical bars
for legacy references require deterministic replay to match every archived grid
and battery endpoint. Mismatches yield an explicit issue. Plans are never rewritten.

The reference is an **outcome derived from input**, not the complete input itself.
Keep the original household/available-PV forecast chart: available DC PV before
clipping/losses can differ from reference production. The full-horizon price chart
shows spot market, effective import and effective export rates from original
`tariff_evidence`, at its billing resolution, even within an hourly control block.
Missing spot evidence stays a gap. Never derive spot from a retail rate. Conditional
bonuses stay separate because their payment can depend on time, conditions and
remaining period quota. Detailed factual explanation follows the graphs.

The single **Price inputs** chart uses the selected plan's frozen prices. If no
plan is available, its status explicitly identifies a current tariff preview.
Changing the selected plan updates prices together with its other original
evidence. A late preview response never overwrites archived plan prices.
Both modes draw published market intervals as
solid lines and forecast intervals as dotted lines. Import, export and potential
bonus-inclusive export rates inherit the market interval's recorded `price_kind`.
Mixed series change style at the exact interval boundary, without changing rates,
native resolution or missing-data gaps. The no-plan preview uses the current API
classification. Selected-plan prices retain their frozen classification even when
new published prices arrive. Older evidence without a classification retains its
existing line style rather than inventing forecast provenance.

## Layer 2: tested alternatives: specified, not implemented

On request, compare a precise alternative, such as exporting a stated additional
quantity in a selected interval instead of storing it. Reuse the original frozen
input, complete horizon, strategy, contract rules, counters, loss model, limits
and terminal valuation. Change only the explicit alternative requirement. Allow
the remaining schedule to adapt. Never activate the comparison as a live plan.

Report the tested quantity and scope, feasibility, objective differences and
proof status. For consumption minimization, respect corrected-import priority
before comparing costs. A more expensive feasible alternative is not proof that
all alternatives are worse: optimality or sufficient solution bounds are needed.
Distinguish proven worse, infeasible, equal within tolerance and not established
within the computational budget. Infeasibility does not identify a particular
physical cause without additional evidence. If an alternative is cheaper, report
that honestly rather than inventing a justification for the original choice.

This layer requires a separate implementation and independently specified small
comparison scenarios. It is not silently approximated by Layer 1 observations.

## Verification

`tests/app/planning/test_plan_evidence.py` checks original values, numerical limit observations,
negative prices/bonus separation, missing evidence, strategy, time-zone matching,
read authentication and immutability without solver calls. Desktop/mobile tests
in `tests/frontend/browser/test_plan_evidence.py` inspect cards, move between intervals, preserve
original prices after contract corrections and enforce responsive layout.

The planning page shows all three scenario balances without a comparison selector.
The graphs appear in optimizer, battery self-consumption and no-battery baseline
order. Each graph has its own financial header. The self-consumption scenario
aims for zero on the meter within battery and converter limits. It does not
guarantee zero grid exchange.
The no-battery baseline retains planned EV and thermal loads. It is not an
entirely unplanned household.
Each scenario header starts with its expected economic cost. Optimizer and
battery self-consumption then show expected benefit versus the same without-battery
baseline. These amounts use their original archived comparisons. The incremental
optimizer benefit against self-consumption is not substituted for baseline benefit.
Negative benefit remains signed and uses the loss colour. Zero remains neutral,
including amounts that round to zero at the displayed currency precision. Missing
comparisons stay unavailable. All headers name the complete original plan period.
Amounts do not change with chart zoom and are not measured or annualized returns.
Price inputs follow the three scenario graphs without a duplicate optimizer
result card. Cash cost, wear and non-cash stored-energy value remain in the
keyboard-accessible Cost breakdown disclosure. Its expanded state survives a
read-only refresh.
Economic cost is net electricity cost plus wear minus the signed battery value
change. A positive stored-energy value change reduces economic cost and is not cash.
Each graph identifies its original expected economic cost, net electricity cost, wear and
inventory change. Self-consumption and OEP also show expected benefit versus the
same no-battery baseline. Negative benefit remains negative. These are horizon
expectations, not summed overlapping savings. All planning graphs share the
6-hour, 24-hour and full-horizon selection.

The current plan's x-axis begins at the containing current quarter (22:10 starts
at 22:00). Explicit historical plans keep their original period, rounded down only
for axis presentation. First partial blocks retain their actual start and energy.
Every displayed time tick falls on a quarter boundary. Skip labels on long horizons
instead of inventing irregular timestamps. Source values and native price segments
are never averaged or interpolated to create visual variation.
