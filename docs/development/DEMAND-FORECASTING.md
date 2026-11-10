# Household demand reconstruction and forecasting

There is always a forecasting model once the user has supplied a demand
definition and the minimal annual-demand estimate. Missing usable history selects
the startup model. It does not create an empty forecast or measured zero load.
Baseload is part of household consumption, not a separately configured constant.

Saving an unchanged demand definition preserves the learned forecast, candidate
and loaded Recorder history. A demand-only edit preserves unrelated settings. Live planning still requires
a configured supplier contract and valid market prices. A visible demand
forecast cannot authorize placeholder-priced commands. Recorder
history failures produce a deduplicated HA notification naming the affected
sensors. The available forecasting model remains active. A forecast/configuration
fault takes precedence, and recovery dismisses the history notification.

## Model and tools

| Component | Implementation |
|---|---|
| Historical reconstruction | Pure Python typed hourly energy balance using normalized HA Recorder sums |
| No usable history | `startup-v2`: a declared daily shape scaled by the estimated annual total household kWh |
| Historical baseline | `weekday-hour-v2`: local weekday/hour means, then same-hour/global residual means |
| ML candidate | `ridge-v2`: standardized linear regression with L2 regularization, fixed alpha 10, unpenalized intercept |
| Numerical runtime | Existing pinned NumPy. Augmented least squares using `numpy.linalg.lstsq`, no new dependency or user compilation |
| Weather | Replaceable aiohttp/Open-Meteo temperature adapter using the existing HA location |
| Planning | Existing HiGHS/highspy and independent plan validation. Forecasts do not control physical devices |
| Verification | pytest, actual HA Recorder/API tests and app browser tests, Ruff and the existing complete Linux/Playwright CI |

Ridge has 16 calendar/recent-load features, or 19 when complete temperature data
is usable. Features are local daily sine/cosine harmonics, weekday indicators,
annual seasonality and the preceding 24-hour residual mean. Temperature adds
Celsius, heating and cooling effects. Calendar features use HA's configured
timezone while intervals remain continuous in UTC across DST.

At least 28 days' worth of usable hourly training data and enough complete
24-hour lag windows are required for Ridge. Missing weather selects calendar-only
training. It is never interpreted as zero degrees. A temperature-enabled model
needs weather coverage for its entire predicted horizon. Missing prediction
features leave the simpler model active. No universal forecast accuracy is claimed.

The historical baseline needs at least 168 measured hours covering every local
hour. Sparse night-only measurements cannot turn unknown daytime consumption
into zero. Startup weights are 0.6 for 00–05, 1.2 for 06–08, 0.9 for 09–16,
1.5 for 17–21 and 0.9 for 22–23, normalized per day. These provisional defaults
are implementation assumptions, not a measured household profile. Annual scaling
uses 365 days. DST/leap/calendar variation is an estimated startup approximation.

## Measurement definition and exclusions

Use a direct cumulative whole-home AC energy sensor where available. Otherwise
declare disjoint signed AC energy channels:

```text
household = grid import - grid export + AC generation
            + standalone battery AC discharge - standalone battery AC charging
residual household = household - measured separately planned consumers
```

A hybrid inverter's measured net AC supply/absorption can replace its combined
PV/battery channels. Do not combine that aggregate meter with the channels it
already includes, or mix raw DC PV/battery counters into the AC balance. HA energy
units alone cannot establish the physical measurement boundary. The app wizard reuses the complete Energy dashboard balance and does not accept
an empty balance or a parallel direct-household-sensor setup. The internal typed
reconstruction contract retains explicit AC boundary semantics.

Only devices that the EMS will model and schedule separately are exclusions.
Their measured history is subtracted before training. Proposed plans are never
used as measured exclusions. Charging/storage flows already removed by the
household energy balance must not be subtracted a second time as a consumer.

Absent channels mean an incomplete sample, not zero consumption. Adjacent HA
statistics sums provide hourly energy and account for historical meter resets.
Missing differences are excluded. When a normalized statistics sum decreases,
both the decreasing difference and the preceding difference ending at its high
endpoint are unavailable, including when the correction follows missing hours.
This prevents a corrected spike from being learned as
device consumption. Other complete hours remain usable. Physical counter resets
whose normalized Recorder sums keep increasing retain their measured energy.
All required household channels are committed as one complete daily import cohort
before becoming training input. The last accepted cohort survives interrupted
refreshes and restart. Selected consumers complete independently.
If a changed definition has no accepted cohort yet, already imported source
checkpoints can activate it immediately. Every required source must contain
admissible history, match the current import target and have no continuity or
cache fault. Missing or partial sources do not establish a new cohort.

## Full available history

Demand and selected consumers use the same history-owned importer as Solar.
Recorder monthly discovery starts at 1970 and identifies the first available
month. All available years are then imported as bounded annual chunks. Every
hourly response remains limited to 367 days and 9000 endpoints. Closed years
are reused from the active database. The open year refreshes through the latest
requested boundary. Empty years, missing hours and source resets never become
zero measurements. Import checkpoints resume after restart.

A gap-spanning cumulative jump at the first returned endpoint is omitted.
The next contiguous one-hour difference is usable. A gap does not discard the
remaining year. Model fitting uses every usable supplied year. Temperature
history extends to the oldest loaded household or consumer measurement, using
bounded annual provider requests. Retrieved weather retains its actual receipt
as `known_at`. It cannot establish knowledge at an earlier decision time.

Training history and original forward accuracy have separate lifecycles. More
historical samples do not replace the required future accuracy evidence before
a learned model is selected. Solar's existing wire values and archive identities
remain unchanged. Financial physical-counter history retains its stricter
independent contract.

The importer reuses validated immutable annual chunks within the active runtime.
Each cache entry binds its content reference, source identity, range and learning
cohort. Only one version per sensor/year is retained. Explicit history restore
and process restart revalidate durable chunks. Failed transactions restore the
previous cache state with their source checkpoints.

Demand history parsing, archive work and configuration history restoration run
off the event loop. The async caller keeps the shared application lock through
admission, write and installation. Repeated cancellation drains the owned work
before that lock is released. It does not authorize concurrent conflicting saves.

### Training-only negative-balance policy

Reconstruction evaluates each complete UTC hour independently. It allows **1 Wh
(0.001 kWh) per distinct cumulative meter** in the balance. Two rounded endpoints
can contribute one rounding quantum to a difference. This is a declared internal
rounding budget, not a measured accuracy specification or a guarantee about
asynchronous meter sampling. Recorder does not supply meter-resolution metadata
in this contract. There is no user setting or relative allowance that grows with
consumption.

The household budget counts only household terms. The residual budget counts
household terms plus measured exclusions. A separate 0.0000001 kWh numerical
epsilon stabilizes the inclusive threshold after cumulative floating-point
subtraction. For example, two terms allow a reconstructed household balance of
-0.002 kWh to become zero for training. A balance of -0.002001 kWh is rejected.
Exclusion meters cannot enlarge the household budget.

If either balance exceeds its negative budget, omit that hour entirely. Otherwise,
clamp only negative reconstructed household/residual values to zero. Keep positive
values unchanged and compute residual from the original balance before clamping.
Never apply this allowance to a decreasing source sum, missing measurement or
incomplete hour. The existing counter-correction and gap rules still apply.

Retained hours feed the existing models. The historical profile still requires
168 usable hours covering every local hour of the day. Ridge retains its existing
sample and contiguous-history requirements. Filtering never fabricates evidence
to pass those requirements. Appliance components and residual reconstruction use
the same policy. Rejected hours are excluded individually. Their absolute count
does not block appliance separation or training when enough usable evidence
remains. A component without sufficient usable coverage remains unavailable.
Its consumption stays within the measured household remainder.

`GET /v1/demand` exposes disposable `history_quality` counts: `usable_hours`,
`corrected_hours`, `rejected_hours` and `rejected_percentage`. The percentage is
100 times rejected hours divided by usable plus rejected complete hours. It is
null when no complete hours were assessed. Corrected hours are included in usable
hours. Each corrected hour is counted once even if both balances were negative.
Missing channels and counter corrections do not count as rejected balances.
The field is null before a current worker result is installed, after settings
change, or when a replacement history cohort commits. It remains null if the
replacement worker fails. Partial history collection retains the previous
cohort and its counts. Superseded worker results cannot install their counts.

The `demand_boundary_inconsistent` notification requires at least **20 percent**
rejected balances and at least **24 complete assessed hours** in the current
training cohort. This operational policy is not a statistical test of sensor
accuracy. Missing channels, counter resets and unassessed hours do not inflate
its denominator. Small rejected shares remain diagnostic information, including
when the retained history requires the annual-consumption startup estimate.
Filtering and the separate conservative appliance-component admission safeguards
remain unchanged.

After a forecast worker result with usable training history is accepted, the
responsible demand logger reports usable hours, excluded hours and the excluded
percentage. This assesses the training history without claiming that a learned
model replaced the startup estimate. New or changed cohort quality is INFO.
Unchanged retraining summaries are DEBUG. Results with no usable hours, failed
results and superseded results cannot emit this assessment summary. A new history
cohort or configuration clears the previous quality before its accepted summary.
Counts and percentage remain available in `GET /v1/demand.history_quality` even
when the share is too small for a notification. They are disposable diagnostics,
not another archive of measured data.

This training-quality warning is not a blanket planning blocker. Direct household
planning retains its measured historical profile when the remaining samples
qualify, or the explicitly provisional annual-consumption startup estimate when
they do not. Configuration, freshness and interval-coverage admission checks
still apply. An unavailable appliance component cannot replace the incumbent
household forecast or silently remove device demand. Its separate composite
admission and activation checks remain unchanged.

The warning identifies the configured balance sources, explains training impact
and directs the user to overlapping Energy sources and exclusions. It does not
identify an individual faulty meter without evidence or recommend restoring an
archive for negative balances. Successful synchronization clears the warning
after a recovered reconstruction. Unreadable saved settings and failed updates
retain separate explanations.

Only newly reconstructed training samples change. No Recorder data, normalized
histories, saved settings, historical forecasts, model archives or plans are
rewritten. There is no database schema change or historical-data migration.

If exclusions have insufficient history, the startup prediction remains available
as `total_household`, with provisional quality and no claimed excluded devices.
It cannot be passed off as a residual forecast. Separately planned devices require their own forecast or schedule provider before the basis can exclude their demand. See [appliance architecture](APPLIANCE-ARCHITECTURE.md), [EV charging](EV-CHARGING.md) and [thermal storage](THERMAL-STORAGE.md) for supported scheduling boundaries. Physical control remains in user-owned HA automations.

## Activation and chronological evaluation

Production uses the shared local-day accuracy policy described below. The
financial scorer in this section remains a research and regression seam.

The startup/historical profile is the active baseline. Ridge is also trained and
published as a candidate after enough data is available. Training fit or a lower forecast objective cost does not authorize activation.
Production promotion requires preserved matching daily evidence.

`DecisionScorer` is the explicit port for historical planning and realized
settlement. The caller must retain identical prices, PV forecasts, installation
limits and initial states for the two models, validate each plan independently,
and settle it against the same actual consumption. Missing historical context
must raise, not be synthesized. No HTTP approval flag enables Ridge.

`compare` refits before each of at least three disjoint chronological 48-hour
windows. Future consumption is withheld from training. Weather is filtered by
its `known_at` time. Newly downloaded reanalysis is not an old forecast archive.
Every window must improve actual economic cost by more than the larger of
EUR 0.001 and 1% of the absolute baseline cost. This is an initial conservative
gate, not a universal statistical guarantee. Calendar/temperature feature modes
must match the candidate selected for activation.

Production activation uses the shared [local-day accuracy selection](MODEL-SELECTION.md).
The last available forecasts before local midnight are retained and compared
with matching later measurements. Each of three eligible recent days must show
a material accuracy gain before a learned model replaces its measured baseline.
The injected chronological financial scorer above remains an isolated research
seam. It is not the production selection criterion.

## App and HA interfaces

- The app wizard reuses the complete Energy dashboard balance. It infers the
  annual startup scale from sufficient history. Otherwise it asks for one estimate.
  Energy-owned source membership is read-only. HA supplies timezone/history
  through the existing authenticated integration connection.
- `PUT /v1/demand` accepts `timezone`, `annual_kwh`, signed `terms`, measured
  `exclusions` and `expected_version`. Concurrent/stale configuration writes fail.
- `GET /v1/demand` returns configuration, active/candidate predictions, quality,
  explicit model status, historical requests/issues and any decision comparison.
- `POST /v1/demand/history` accepts the existing normalized compact kWh history
  format for the exact requested source/version/range. It has a dedicated 1 MiB
  limit. Other endpoints retain their existing limits. HA reads through the same
  optional Recorder adapter, with a separate retry state from bonus initialization.

Example advanced configuration (all channels must describe disjoint AC flows):

```json
{
  "timezone": "Europe/Amsterdam",
  "annual_kwh": 3650,
  "terms": [
    {"entity_id": "sensor.grid_import_energy", "sign": 1},
    {"entity_id": "sensor.grid_export_energy", "sign": -1},
    {"entity_id": "sensor.hybrid_ac_supply_energy", "sign": 1},
    {"entity_id": "sensor.hybrid_ac_absorption_energy", "sign": -1}
  ],
  "exclusions": [],
  "expected_version": null
}
```

Forecasts cover 48 elapsed hours: first 24 hours in 96 quarters, then 24 hourly
intervals. Hourly model averages are split across quarters. This does not claim
measured or learned quarter-hour detail. Partially elapsed intervals are projected
by overlap when used by the planner. Forecast issue time is the actual capture
time, distinct from the quarter used to coalesce background work.

The [installation compiler](INSTALLATION-PLANNING.md) joins cached household
forecasts with per-source DC PV, physical limits and measured per-battery state.
Original plans retain `demand_evidence` with forecast/configuration/training
references, model and quality. Injected test bases are not modified by a separate
legacy demand projection. An explicitly empty energy-term list supplies no training
samples and requests no fabricated history. The confirmed annual estimate still
produces the provisional startup forecast. Measured exclusions require an actual
household energy boundary. Energy channels can be selected later for learning.

## Timing, storage and deployment

The app captures an immutable job under its state lock, trains/archives in a
background executor and installs only a still-current result. Forecast jobs are
coalesced per quarter/input version. No model is fitted on each sensor poll.
Cancellation drains an in-flight archive write before returning. Old forecast
references are never rewritten by later training or configuration changes.
Failed jobs retry after five minutes for unchanged inputs. Stale successful or
failed jobs cannot overwrite the state of a newer configuration. Unreadable saved
configuration is an explicit planning fault rather than permission to use another
load source silently. Actionable demand faults are also shown in HA notifications.

Selected HA history is read once per source/day over a bounded rolling annual
range. Raw per-source series stay in memory. They are not a copy of the Recorder
database. Immutable settings, predictions and plain model coefficients live in
the `demand` document namespace of the app's active SQLAlchemy database. Include
that database in the app backup. JSON model parameters avoid executable pickle.
The shared backend transfer includes these documents. See the
[database dictionary](DATABASE-SCHEMA.md) and [archive contract](PLAN-ARCHIVE.md).

Production `--live` wiring enables the default temperature source only after
demand configuration and an HA location report exist. Historical weather is cached
daily, future weather is refreshed hourly, and failures retry after 30 minutes.
Calendar/startup forecasts remain available during provider outages. External
data access terms remain applicable. This is not a promise of unrestricted hosted
commercial API access. Tests use fixtures and make no external weather calls.

One Linux development-container measurement on 8760 hourly samples and 16 features
took 353.10 ms to fit and 18.53 ms to produce/version 120 intervals. This is one
local measurement, not target-device latency, memory sizing or native ARM evidence.

References: [NumPy least squares](https://numpy.org/doc/stable/reference/generated/numpy.linalg.lstsq.html),
[Ridge/L2 model definition](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Ridge.html),
[Open-Meteo](https://open-meteo.com/en/docs),
[historical weather](https://open-meteo.com/en/docs/historical-weather-api).

## Quarter-hour timing

The v2 projection reconstructs a bounded linear rate within each hourly model
mean. Its slope is the smaller adjacent same-direction change, limited to keep
the rate nonnegative. Flat hours and local extrema stay flat. First/last weather
hours use a zero boundary slope, without asking the model for unavailable past
or future features. Integration preserves each complete hour's kWh exactly,
including UTC intervals across daylight-saving changes and the mixed-grid seam.
For means 1, 2 and 3 kWh/hour, the middle hour becomes 0.40625, 0.46875,
0.53125 and 0.59375 kWh: still exactly 2 kWh.

This estimates timing from hourly evidence. It does not claim measured or learned
quarter precision. Constant household profiles correctly remain constant. The
simulator already creates distinct quarter measurements and native five-minute
Recorder sums. Those observations are not changed merely to make charts look
different. No random jitter is added to predictions or charts.

Original v1 forecasts/plans remain unchanged and readable. New model identities
also isolate the forward-selection cohorts, so old projection results cannot
authorize a changed predictor. Published quarter market prices are unchanged.
Hourly price forecasts can legitimately retain four equal quarter rates.

## Individual-consumer forecasting

Phase 1 is forecast-only. Discover individual consumption from HA Energy `device_consumption`, including `included_in_stat` upstream relationships. Users select disjoint meters in **Settings > Appliances** and mark temperature sensitivity. There are no task requests, physical commands, readiness helpers or blueprint inputs in this phase. Phase 2 adds shiftable cycles and the separately specified mandatory feedback/blueprint chain. A simulated dishwasher is its future acceptance fixture, not a phase-1 control feature.

Individual-consumer forecasting reuses NumPy Ridge, weekday/hour fallback, Recorder hourly cumulative sums and Open-Meteo temperature history/forecasts. With fewer than seven complete days covering all hours, a component is unavailable: keep the existing whole-house forecast. Ridge needs the existing 28-day training threshold and complete recent-load windows. A weather-sensitive choice uses temperature features only with complete training/prediction weather. Otherwise its historical profile remains explicit. No zero-degree or zero-consumption substitution is used.

The active composition is **remaining household + each ready selected device**,
calculated once from matched historical channels. Reject parent/child overlap
and apply the training-only negative-balance policy above. Measurement separation
does not wait for ML admission. Every component retains its measured weekday
baseline until the shared daily accuracy policy admits an alternative. A missing
consumer stays in the remaining-household boundary while independently ready
consumers remain separated. Missing or inconsistent residual history withholds
separation. The public composed forecast carries household scope and no extra
exclusions because its components have already been added back.

The `/v1/consumers` API stores app-owned selection/weather flags with optimistic concurrency in the shared SQLAlchemy demand document namespace. Energy membership and upstream links remain read-only. Models, component forecasts and original parameters are archived. Source history is fetched through the integration rather than a direct Recorder database connection. Removed sources remain in historical settings, disappear from offered choices and prevent stale composition. Forecast-only HA devices publish current-interval mean forecast W, full-horizon forecast kWh and forecast status. These are estimates, not measured energy or commands.

Ready device forecasts remain available when another selected consumer has too
little history. The remaining-household baseline subtracts only ready components,
so unavailable devices stay inside measured household consumption. This permits
safe explicit boiler, EV or appliance scheduling without inventing missing
device demand. The active partial composition identifies unavailable sources explicitly. Missing
or inconsistent residual evidence continues to withhold controlled-load
replacement.
The app and HA expose each device's own forecast status. Ready predictions and
fitted parameters are frozen in the existing quality-component documents even
when no complete challenger exists. Later model selection changes the current
status without rewriting the original evidence.

The app provides an interactive component/residual power chart and clear actual planning-versus-shadow status. The designated laboratories include a labelled synthetic heat-pump meter as a disjoint fraction of their existing simulated household load. This fixture verifies discovery/history/UI/transport. It does not prove real heat-pump accuracy or realized model-selection benefit.
