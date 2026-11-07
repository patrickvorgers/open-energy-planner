# Understand calculation time

Open **Insights > Planning performance** when you want to understand how long calculations took and what result they produced. It is a read-only history. Opening a calculation does not activate its plan or change solver settings.

## Size hardware for your installation

There is no fixed product maximum for the number of configured vehicles,
charge points, boilers, appliances, solar groups, batteries or energy channels.
More devices and scheduling choices can require more CPU, memory and calculation
time. Twice as many devices does not necessarily mean twice the calculation time.

Choose hardware with enough headroom for Home Assistant, other applications,
input preparation and plan publication as well as solver work. Use the history
below to check whether normal operation produces usable plans within the
calculation budget. More powerful hardware can help with complex installations,
but cannot make contradictory constraints feasible. Transport size limits,
timeouts, physical limits and input validation remain active. There is no
universal device count that guarantees timely plans on every host.

## Inspect a day

1. Choose the **Day** in the Home Assistant timezone, or use **Today**.
2. Read the **Calculation time** chart.
3. Enable **Show additional calculations between quarter hours** when you need to include intermediate rounds.
4. Click the relevant bar to open its recorded details.

There is no duplicate calculation list below the chart. With a keyboard, focus
the chart and use Left or Right to select a bar, Home or End to jump to the first
or last bar, and Enter or Space to open its details. The selected time, result
and duration are announced. The chart itself exposes the selected calculation
to assistive technology. There is no separate selection or action bar. Closing
the popup returns focus to its opener.
Refresh retains the selected calculation when it is still present. Enable
additional calculations to navigate those bars too. Empty or failed history
reads cannot open an old calculation under the newly selected day.

If history or details cannot be read within five seconds, the page shows recovery
advice. Use **Refresh** to retry history, or close and reopen the calculation
popup to retry its details. Repeated refresh clicks share the pending read.
Choosing another day replaces that read. Reading or retrying never changes your
plans or recorded history.

The stacked bars separate the first observed financial candidate from further calculation time when both were recorded. An unknown first-candidate time produces a total-only bar. Missing historical progress cannot be reconstructed from the total duration.

Hover over either section of a bar to see one tooltip for that calculation,
including both recorded stages, the total duration and the publication result.
Times use at most one decimal place in your browser's number format. Missing
first-candidate times remain explicitly unrecorded. Clicking the bar still opens
the full calculation dialog. Its progress tooltip uses the same time precision.

[![One synthetic day with 96 calculation bars and direct detail access, without a duplicate list.](../assets/planning-performance.png)](../assets/planning-performance.png)

*Blue and amber separate the observed first candidate from further calculation. Purple marks runs without a recorded first time. These are synthetic history records, not a solver benchmark.*

## Distinguish a candidate from a usable plan

The solver can find a candidate before it finishes improving it. That observation is not independent physical validation or publication. Read the outcome as well as the duration. A calculation can reach its time budget and still produce a checked usable plan, or finish without publishing one.

The solver budget applies to solver work. Total elapsed time can also include worker startup, preparation and publication. A total above the solver budget therefore does not by itself prove a timeout defect.

## Read plan improvement

The calculation dialog shows observed candidate costs and a best possible bound when available. **Remaining possible improvement** is a model-quality bound in currency and, where defined, a percentage. It is not percentage completed or an estimate of remaining seconds.

For example, a candidate cost of EUR 10.00 and a valid lower bound of EUR 9.80 leave at most EUR 0.20 improvement under that model. This does not promise the solver will find it. Costs include the stated stored-energy valuation and are not a supplier bill or measured savings.

Compare progress within one calculation. Two calculations can have different inputs, goals and horizons, so their costs are not directly comparable. A slow bar alone cannot identify the cause. Use the detail's strategy, interval count, preparation time, service priorities and outcome before reporting a problem.

On a phone, the calculation dialog fills the screen. Use **Close** to return to the same history. An absent progress sequence remains explicitly unavailable rather than becoming an invented improvement curve.

**Next:** [Read the resulting plan](read-your-plan.md), or [report a problem with useful context](troubleshooting.md#ask-for-help-safely).
