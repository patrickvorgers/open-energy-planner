# Automatic forecast model selection

Household consumption uses the economic decision policy under FORE-04–06 and
FORE-10. Automatic solar source selection uses matched forward forecast accuracy under
FORE-17, with optional manual PVWatts, HA or ML selection. See
[solar forecasting](SOLAR-FORECASTING.md#ha-forecast-adapters-and-source-selection).
The economic solar comparison implementation and its historical archives remain
available as retained research evidence. The production worker no longer applies
their preferences to solar forecasts. The sections below describe the economic
decision policy and its original solar research support.
The user supplies no ML settings and does not approve model activation. Installation
settings > Forecast models shows the current model, independently validated
preference, training/evaluation status, pending window, last evaluation and the
three score summaries. `GET /v1/models` exposes the same authenticated, read-only
status. These are simulated decision costs, separate from measured EMS savings.

## Existing models, no new ML dependency

Household consumption compares the existing weekday/hour (or startup) profile
with the existing NumPy ridge regression. Calendar-only and temperature-enabled
regression are separate evidence families. Solar compares the existing pvlib
PVWatts baseline with its bounded local scalar calibration, independently per
source. Other sources and household consumption remain fixed during a source's
comparison. Both baseline and challenger forecasts, parameters and source versions
remain archived even after a learned model becomes active.

Old HA history can train a challenger immediately. It cannot recreate weather
forecasts, contract knowledge, stock or plans that OEP never retained. Therefore
production selection collects forward decision evidence instead of backdating
new forecasts. The older injected chronological `DecisionScorer` remains a tested
research seam, not the production source of invented historical planning context.

## Freeze the decision before knowing the outcome

A background worker captures at most one pending pair per model family. It solves
both variants with the same original published plan's topology, limits, other
forecasts, strategy, prices, dated contract rules, initial inventory assumptions
and bonus positions. Both solves must be proved optimal and pass the existing
independent physical/financial validator. Shadow plans never replace real commands.

The pair begins on the next complete UTC quarter. Opening stock and bonus budget
for planning are projected from the already published plan to that boundary and
are equal for both variants. The first partial interval is deliberately omitted.
The captured window must cover nearly the full configured planning horizon.
Shortened plans cannot establish evidence. With today's 48-hour policy, at most
two initial quarters may be omitted. This follows the shared planning constants.

Ordinary new sensor values do not cancel a frozen comparison. Later configuration,
contract, confirmed-counter, location, strategy or model-feature changes create a separate
family. Old evidence cannot activate a preference for that changed family.
Original plans and completed comparisons are never recalculated in place.

## Realization and independent settlement

After the window ends, selection requires contiguous recorded quarters and actual
published market prices. Hourly interpolation or missing prices/meters cannot
promote a model. Each variant then follows its retained commands sequentially,
seeing only the current realized household/PV interval. It never reoptimizes a
whole day with future actual consumption or solar production.

The existing native AC/DC installation model clamps infeasible fixed commands to
physical capability and applies the existing reserve priority. Fixed commands
have priority over optional zero-grid following. Every resulting flow passes the
independent installation validator. Both replays start with the same measured
battery stock at the first full quarter, then develop independently. The opening
planning projection remains retained separately. It is not claimed as a measurement.

The existing financial settlement applies published prices, original dated formulas,
clock/daylight bonuses and annual caps. Confirmed seed versions are loaded from the
original plan's evidence. Each scenario establishes the same measured opening
budget and then advances its own counters. Losses and configured wear are included.
Both scenarios use the original common terminal valuation curves. A challenger
cannot reward itself with its own optimistic battery valuation. Consumption strategy
also requires no increase in independently calculated import plus terminal stock
replacement before financial benefit can qualify.

Production eligibility is deliberately honest: potentially curtailed PV and hybrid
AC-only production do not establish independently observable available DC. Clipped
production is also insufficient. The check aggregates all sources on a shared
inverter. Until suitable route/control evidence is supported, those installations
retain the baseline with an explicit reason. A fitted model or a smaller MAE does
not bypass this condition. This is a remaining evidence limitation, not proof that
the learned forecast is worse. Residual forecasts with separately controlled-load
exclusions also need matching realized load evidence. The current implementation
does not substitute total-household actuals for residual demand.

## Conservative automatic preference

At least three chronological nonoverlapping near-horizon windows must each improve
economic cost by more than the larger of EUR 0.001 and 1% of absolute baseline
cost. These existing thresholds now have one shared owner in
`forecast_selection/policy.py`. A single loss or insignificant advantage in the
retained three-window cohort keeps/restores the baseline. With complete evidence,
initial warm-up is approximately six days, in addition to model training needs.
This conservative gate is not a guarantee of future savings.

The status separates preference from actual current forecast. Preferences take
effect through the existing forecast worker: the selected forecast is archived
before a new plan can use it. Missing/unusable candidates fall back to the baseline.
The normal planner's quarter schedule and five-minute early-round limit are unchanged.

## Ownership, failure and storage

- `forecast_selection.runtime.ModelSelection` owns comparison cadence, preference
  policy and cached status. `SelectionStore` owns atomic evidence/index writes.
- The existing native model remains the sole owner of physical constraints.
  `optimization.adapters.highs.replay` changes only the recourse objective.
- The shared bounded spawned-child lifecycle isolates numerical work from HTTP,
  HA and the SQL engine. One background round performs at most one capture or
  evaluation. Each shadow solve has a ten-second limit. The capture parent budget derives from
  both solves plus startup overhead. Evaluation separately derives its bound from
  the maximum fine frames, both scenario passes and the two-second per-frame native
  ceiling plus startup overhead (773 seconds for the current 48-hour policy).
  A stop event terminates numerical children promptly on app shutdown. Owned SQL
  work still drains before engine disposal. A timeout retains the pending window
  for retry instead of discarding its evidence.
- Missing evidence retries after five minutes. A window still incomplete after
  one additional day is retained as failed evidence, never scored as zero. It does
  not prevent another model family from making progress.
- All captures/evaluations remain immutable in the existing `planning` SQL document
  namespace. Original plan IDs reuse existing input/topology evidence. The current
  index retains three evaluation references and one pending reference per family.
  Older evidence is not deleted. No new database, tables or dependencies are added.
- The existing namespace/name primary key supports bounded point reads. Frame and
  published-price retrieval reuse existing indexed range queries. Full backend
  transfer copies the index and every evidence document on SQLite, PostgreSQL,
  MariaDB and MySQL. Credentials and SQL engines never enter native child payloads.
- Storage corruption/failure or solver failure cannot revoke an otherwise usable
  real plan. Unreadable selection evidence disables preference restoration and
  appears in status/logs. No replacement history is silently created.

See [the database dictionary](DATABASE-SCHEMA.md),
[demand forecasting](DEMAND-FORECASTING.md), and
[solar forecasting](SOLAR-FORECASTING.md). Regression scenarios independently check
cheap charging versus expensive realized demand, wrong timing, idle commands,
empty batteries, zero-grid following, missing/estimated evidence, consistent
benefit, consumption priority, restart, isolation and all database backends.

Per-frame native timeouts, including reserve recovery, retain the pending comparison for retry under the same evidence expiry policy. They do not classify complete observations as invalid or advance the evaluation cohort.
