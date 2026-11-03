# EMS workspace and interactive time charts

The app owns the interface. The HA integration continues to expose native entities.
The shared navigation groups Planning, Appliances and Device forecasts under Workspace.
Accounting contains Savings and Contract. System contains Installation settings.
Each destination has a decorative line icon and a visible text label. Group headings
remain visible on mobile. The active destination uses the existing palette, with
the same keyboard-accessible links on standalone and Home Assistant ingress pages.
One packaged HTML partial owns this navigation across all five page shells.
The original plan selector never activates an old plan. Historical data is rendered
from saved evidence without recalculating optimization or replacing measurements.

## Chart selection and source research

These references explain the information-design choices.
No product code, artwork or scenario outcomes were copied.

| Primary source | Relevant evidence | OEP decision |
| --- | --- | --- |
| [DAO wiki](https://github.com/corneel27/day-ahead/wiki) | Planning graph, detailed calculation view and browsing past optimizations | Keep original plan selection and put detailed interval tables behind disclosure controls. |
| [EMHASS battery example](https://emhass.readthedocs.io/en/latest/study_cases/basic_pv_battery.html) | Optimization results plus a distinct battery state-of-charge plot | Separate power, battery inventory and prices rather than mix units on an unreadable axis. |
| [Victron Dynamic ESS](https://github.com/victronenergy/dynamic-ess#graphs) and [manual](https://www.victronenergy.com/live/drafts%3Adynamic_ess) | Recorded versus estimated/planned graphs. Prices alongside the energy schedule | Explicit forecast/recorded/reference labels and a separate advice-versus-realization view. |
| [GBBOptimizer version history](https://gbboptimizer6.gbbsoft.pl/About) | Forecast-versus-actual load graph, battery forecast/energy-price graph, daily/monthly profit reporting | Show expected use and financial results in separate focused views. Keep inventory value distinct from cash. |

Planning shows one installation energy balance with every battery's SoC,
effective prices and net EMS/reference grid exchange. Separate battery-converter
charts are omitted to keep the planning overview focused. Converter-level flow
details remain available in the selected-interval inspector. Savings retains
evaluated benefit and recorded import versus original advice and reference.
Available solar forecasts remain in the interval inspector and original tables.
Balance charts use actually planned DC production after curtailment/clipping.
Grid following is a regulation mode, not a fixed battery-power command.

## Signed interval balances

Interactive stacked balance bars always span UTC-aligned fifteen-minute blocks.
Presentation combines accounting fragments with duration-weighted mean **kW**.
Recorded hourly means become four equal bars, without claiming finer forecasts.
Internal sunrise, sunset, price and command boundaries remain exact in the model,
archive, price graphs and interval inspector. They never create extra balance bars.
Gaps, overlaps and unavailable physical evidence invalidate the affected quarter.
Partial horizon edges average only covered time. Tooltips identify that coverage,
so a full-width bar never claims energy before the plan starts or after it ends.
SoC retains its original timestamps and separate right axis fixed at
0–100%, calculated from the plan's frozen capacity and internal stored energy,
including initial and final states. Multiple batteries retain separate identities
and labels, including when they share a converter or a display name.

Time axes disable category-style end padding. The selected start and end map to
the plot edges on balance and line charts, including archived plans and the
six-hour, 24-hour and full-horizon controls. All balance bars have quarter widths.
Calendar charts share compact day headings, alternating pale backgrounds and
midnight separators. Day boundaries use the Home Assistant installation timezone,
including 23-hour and 25-hour daylight-saving days, independently of the browser.
Headings use Today or Tomorrow where space permits and compact dates on mobile.
The headings carry the weekday and date. Shared compact clock ticks include
timezone names to distinguish repeated autumn hours without repeating the date.
Full dates remain available as accessible labels. Clipped edge days occupy only
their visible time. Archived UTC daily savings retain UTC calendar boundaries.
Relative appliance-cycle charts retain their elapsed-time axis. Bands do not
fill missing series, change interval widths or alter stored evidence.
Without battery, self-consumption and OEP share the union of their visible signed
stack extrema, with the existing ten-percent clearance and identical ticks.
Recalculate together after window, plan, visibility and size changes. SoC and
converter-specific charts do not enlarge that comparison scale.
The original forecast chart uses green for household demand and yellow
for available solar. Both forecast lines have at least 3:1 contrast against the
white plot background. Their labels distinguish forecasts from planned balance flows.
The signed balance palette below remains unchanged.

| Flow | Sign | Color / boundary |
| --- | --- | --- |
| Used solar | Positive | Green, DC production |
| Grid import / converter AC supply | Positive | Blue, AC boundary |
| Battery discharge | Positive | Red, DC terminals |
| Household demand | Negative | Amber, AC consumption |
| Grid export / converter AC output | Negative | Cyan, AC boundary |
| Battery charge | Negative | Purple, DC terminals |
| Conversion losses | Negative | Gray, converter and battery DC converter |
| Battery SoC | Separate axis | Dark colored lines, percent of nominal capacity |

At the installation boundary, used solar + grid import + terminal discharge
equals household demand + grid export + terminal charge + conversion losses.
The selected-interval inspector uses the same conservation equation for each
battery converter's local solar, AC input/output and connected batteries. Thus it reveals direct DC
charging versus AC-fed charging. AC supply may originate from the grid or
another inverter. The graph never invents a unique solar/grid attribution on a
shared AC bus. These details cover each battery-connected inverter,
including standalone battery converters and shared hybrids.

Losses are calculated from archived physical evidence and efficiencies, never
from a residual chosen to force visual balance. For inverter efficiency `eta`,
loss is `AC_import * (1-eta) + AC_export * (1/eta-1)`. For each battery DC
converter efficiency `eta_dc`, add `terminal_charge * (1/eta_dc-1) +
terminal_discharge * (1-eta_dc)`. Internal battery storage losses are already
reflected in SoC. Terminal charge/discharge bars do not count them again.
These are display calculations only. Solver inputs, commands, archives and
financial evaluation remain unchanged.

Every complete interval balances to zero within a named 0.00001 kWh display
tolerance. Missing, non-finite or inconsistent physical evidence produces an
explicit warning and missing bars, including for older incomplete archives.
No missing value becomes zero. The complete balance applies when all flow
series are visible. Hiding a series intentionally hides part of that balance.
Finite solver noise within 0.0000001 kWh of zero is displayed as zero,
matching the independent physical validator's per-flow allowance.
Material negative physical energy is rejected. Archived numbers stay unchanged.
The net grid comparison and financial charts are not conservation diagrams.
They remain separate to avoid mixing the simulated reference with EMS flows.

The supplied DAO example inspired signed stacks. [Victron's dashboard manual](https://www.victronenergy.com/media/pg/VRM_Portal_manual/en/dashboard.html)
documents kWh production/consumption bars with a SoC line. The [GBBOptimizer chart-data contract](https://gbboptimizer.gbbsoft.pl/Manual?PageNo=19)
separates AC/DC energy, charging, discharge and percentage state. [EMHASS's battery example](https://emhass.readthedocs.io/en/latest/study_cases/basic_pv_battery.html)
distinguishes optimization results from battery SoC. These are design references.
No implementation or image is copied.

## Interaction and accessibility

Chart.js 4.5.1 is bundled locally, with its MIT notice, without CDN requests or
an additional frontend framework. Hover/tap gives timestamp and values. Native
HTML tooltips use separate label, right-aligned tabular-number and unit columns.
Long labels wrap without shifting individual values. All source text is rendered
as text, and the popup remains inside the viewport.
checkbox legends work with a keyboard. The 6-hour, 24-hour and full-horizon
controls apply to every planning graph. A keyboard/touch interval inspector
explains commands, solar permission, prices and market-rate provenance at that
instant. Battery series are keyed by immutable device ID, never by the display name.
Only duplicate names receive a readable ordinal. Unique HA device names stay
unchanged. Native source resolution stays explicit. Repeating hourly forecasts
does not claim quarter-hour prediction detail. Detail tables remain accessible.
The [price timeline and control grid](PRICE-TIMELINE-AND-CONTROL-GRID.md) keeps
quarter billing separate from clock-hour controls after at least 24 hours, within
an exact 48-hour horizon. Internal billing rows do not add control decisions.

Axes use actual UTC instants with local display dates and elapsed time spacing.
Aggregated day/month evaluation bins retain UTC calendar labels. Missing coverage
breaks lines. Missing advice never becomes a zero target or zero benefit. Long
period totals and benefit bins cover the full selection. The realization chart
explicitly identifies its bounded first day of detail. Existing financial error,
version-selection and stale-response guards remain in place.

## Scheduling ownership

LiveInputs validates every report but requests ordinary planning on UTC quarter
boundaries. A new forecast/price version or a material measured deviation may
request an earlier round only after 300 seconds since the last submission.
Material defaults are 1000 W grid-power error or 5 percentage points of battery
capacity relative to the interpolated published trajectory. Expected SoC motion
is therefore not a deviation. All thresholds are named constants in LiveInputs.
Normal cadence derives from the central fine planning interval.

Initial readiness, recovery and explicit configuration/resume/replan requests
can run immediately. Hard input invalidation and publication safety checks
remain immediate. Quarter transitions need not wait for five minutes. Ordinary
sensor captures and measurement freshness deadlines are never solve timers.
Log each actual submission with its reason and previous submission timestamp.

Effective-price revision hashing coalesces adjacent segments with the same rates,
source, published/forecast kind, native resolution and contract period. Moving
lead-time bucket boundaries or control-grid splits therefore do not masquerade
as new prices. Original segmented tariff evidence remains unchanged in each plan.
Real rate, source-preference and contract changes still create a new revision.

Published commands expire at the next UTC quarter boundary plus five minutes of
recalculation grace, bounded by plan coverage (at most 20 minutes).
Fresh input admission still uses each measurement's age limit. The snapshot
separately retains real forecast/provider expiry, which also bounds commands.
The first billing interval's end is a scheduling boundary, not provider expiry. The old coupling
between a captured measurement's deadline and command lifetime would make
quarter-hour planning leave unavailable gaps. Missing required current inputs
still revoke commands promptly. HA automations/equipment retain physical safety.
An expired plan is never extended merely to avoid an unavailable state.

Vendored asset: `web/public/chart.umd.min.js`, upstream npm `chart.js@4.5.1`
(`dist/chart.umd.min.js`), SHA-256
`48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a`.
The adjacent `chart-LICENSE.txt` is packaged and served locally. Updating this
asset is a normal pinned dependency change requiring browser regression checks.


## Current prices without a plan

Price inputs shows current cached spot prices and the latest saved import/export
rules. The purple line is the total potential export rate including eligible
bonus, before the annual volume cap. It is available without a plan, household/PV
forecasts or a confirmed annual export baseline. Saving a bonus then opening or
refreshing Planning updates this preview. It does not change frozen plan inputs.
Original plan prices remain separately labelled with the archived plan evidence.

A non-quarter boundary such as 07:19 can be an exact sunrise/bonus-window edge.
It is not necessarily a market-source timestamp. Native provider resolution is
preserved and hourly forecasts are not given invented quarter variation. The
axis remains aligned to 15-minute ticks. Outside a paid window the total export
line equals the base export rate. Unknown evidence remains a gap with a reason.
Regression coverage includes microsecond daylight boundaries and unknown location,
so browser rounding cannot create an apparent missing tariff.

Daylight bonus eligibility uses complete minutes. The shared solar-window policy
rounds sunrise up to the next whole minute and sunset down to the preceding whole
minute. An already exact minute is unchanged. Boundary generation and bonus-rate
evaluation use this same policy. Ordinary 15-minute market prices remain unchanged.
Old immutable plans retain their original precise boundaries. Their chart uses the
recorded end timestamp, avoiding false gaps when legacy microsecond timestamps are
rendered at browser millisecond precision.

Value-axis bounds use each chart's own visible data, including signed stack totals.
The three planning comparisons share time windows but no longer share y extrema,
so another scenario cannot create excessive empty space. Unrestricted ends retain
ten percent of the displayed axis height as clearance. Physically nonnegative
consumption, production, appliance power/energy and forecast-quality charts have
a fixed zero lower bound and only upper clearance. Signed balances, net power,
prices and savings retain their negative domain. SoC remains fixed at 0–100%.
