# Forecast quality

The Forecast quality page compares one fixed midnight prediction with the actual energy of the same Home Assistant calendar day. Select a day rather than a planning round. Household consumption, individual consumer components and each PV source retain their original forecast identities. Interactive hourly graphs expose timing errors that can cancel in a daily total. The expandable table retains gaps, eligibility and provenance.

Each graph shows predicted and measured hourly kWh as bars with matching straight
lines drawn over them. Each line point sits on its own bar's top centre, including
after resizing or hiding another series. Lines connect the same original hourly values and stop at
missing measurements. A measured zero remains zero. No smoothing or missing-value
interpolation changes the evidence. The accessible legend reuses Planning's
layout, to the right on desktop and below the graph on mobile. Its two controls
toggle the bars and line for each series together. Tooltips list each series once.

Refresh evidence updates measurements and current model readiness for the selected day. It never switches to a newer day or replaces the original prediction with an intraday plan. Load older days pages the retained day index. Forecast creation, automatic model selection and ordinary operational replanning remain unchanged.

## Midnight capture and completion

The app observes forecast references every five seconds independently of the solver. On the local midnight transition it freezes the last pre-midnight observation for the new calendar day. The observation gap must be at most ten seconds. Only forecasts issued at or before midnight are retained. Source identities and forecast references are captured together under the app state lock. Persistence runs off the event loop. A failed database write retries the same captured context rather than substituting a later prediction.

Startup after midnight does not invent a missing baseline, and an unobserved transition is explicitly unavailable. Old intraday plans are not backfilled into daily forecasts. A restart after successful capture restores the original database record. No successful midnight solve or new prediction model is required to retain the already available forecasts.

Day boundaries use the Home Assistant timezone. Amsterdam's clock-change days contain 23 or 25 hours. A day is eligible only after its exclusive end plus the existing ten-minute Recorder delay, and only when every hourly forecast and measurement is complete and comparable. This normally means evaluation begins after 00:10 on the following day. Late statistics can postpone it further. Refreshing corrected measurements recomputes the evaluation without changing the baseline. A good daily total alone does not prove accurate timing.

If a timezone's midnight falls inside a UTC measurement hour, this hourly transport cannot establish exact calendar-day edge energy. The day remains explicitly unscored rather than distributing that hour into invented measurements.

## Evidence and ownership

Daily baselines reference the existing immutable household, same-issue individual-component and PV archives. Metering bindings and display names are frozen as well. Controlled-consumer eligibility reuses the existing atomic plan-activation history and original plan fields. Publication time, rather than calculation start time, assigns controls across midnight. The plan already active at the day boundary is included conservatively. Failed publication leaves no activation, and archive read failures withhold the comparison. There is no separate post-publication write to lose. Intraday plans cannot replace the prediction. Original plans and solver input hashes remain unchanged. Current model readiness is shown separately from the model that produced the midnight forecast.

The authenticated `/v1/forecast-quality/days` index and `/v1/forecast-quality/days/{day}` view serve the daily page. The historical `/v1/forecast-quality/{plan_id}` endpoint remains read-only for existing consumers and original-plan investigation. Its hourly output is not used for daily scoring.

Appliance scheduling and charger-baseline exclusion preserve the original `demand_evidence` archive references. Copies of their input baseline forecasts remain in `appliance_evidence`. The derived residual interval loads are stored in the physical problem. A digest of those inputs is not a separately archived household prediction. Controlled forecasts stay visible through their original archives, with accuracy withheld where control changes consumption timing.

The integration reuses its existing Recorder transport. It sends cumulative hourly endpoints for the latest three days, with a ten-minute recording delay. The app refreshes each source once per hour and retries failures after fifteen minutes. A failed source does not block the remaining sources. Startup refresh recovers recent missed hours. Longer outages leave gaps. Data is retained after receipt without an automatic retention cutoff. This is an accuracy archive, not a new HA connection or direct Recorder database reader. Current control decisions are accepted and projected before optional quality-history capture and transfer. A slow or failed Recorder read cannot postpone a received pause or revocation, and completing it cannot reapply an older control response.

The app stores immutable measurement blocks and receipts through the shared SQLAlchemy document table. Daily pointers can advance when Recorder corrects recent statistics. Earlier blocks and receipts remain available as evidence. Changing a metering entity's registered device or unit creates a new binding. An entity without a registered device cannot identify an unregistered hardware replacement automatically.

## Comparable evidence

Four complete quarter predictions are summed into one UTC hour. The actual is the difference between contiguous cumulative Recorder endpoints for the same source. Missing endpoints, counter resets, incomplete forecasts and overlapping forecast steps do not become zero. All hours must be eligible before any daily accuracy metrics are reported. Hourly measured values can remain visible while the day is still in progress. Missing measured hours never become a partial total presented as a full day.

Household actuals use the original signed metering terms. Controlled EV or appliance timing can change consumption, so those household hours are shown without a descriptive accuracy score. The same restriction applies to an individually controlled consumer. Uncontrolled device and PV evidence can remain independently eligible.

PV comparisons use the frozen AC or DC measurement boundary. Curtailed, hybrid and shared-inverter boundaries are not treated as directly comparable generation evidence. AC intervals near the configured inverter limit also withhold the accuracy score. Values remain visible with the reason for exclusion. Original individual-device components indicate whether weather features were used and whether the component was active or a shadow prediction.

Older plans without frozen source bindings and days without a recorded midnight baseline remain explicitly unavailable. They are not reconstructed from today's configuration.

## Metrics and model selection

The primary daily error is total predicted minus total measured kWh. Absolute daily error is its magnitude. Relative daily error divides that magnitude by the measured day total. A complete zero-energy day remains measured zero, with no relative percentage. MAE is the average absolute hourly error in kWh. Bias is predicted minus measured kWh per hour. RMSE is the square root of the mean squared hourly error. WAPE is the sum of absolute hourly errors divided by total measured energy. It exposes timing errors that cancel in the daily total. All accuracy metrics are withheld until the entire day is eligible. Coverage separately reports measured hours and comparable hourly pairs.

These descriptive errors do not change the existing automatic model-selection policy. The page reuses its current model, candidate status, completed windows and rejection or waiting reasons. Candidate results show simulated decision costs and grid import with the existing terminal-stock correction. They are not measured savings and are labeled accordingly. The Savings page remains responsible for actually measured savings. Controlled EV and appliance workloads keep the existing financial replay guard because that replay cannot reproduce their changed consumption boundary.

## Verification

Forecast quality presents current model status beside the corresponding device's
fixed daily graph, inside Current model and learning. Current observations never
replace the midnight model or prediction. Matching uses household, solar source
and consumer entity identities, never display names. The day index also returns
cached current model readiness so a first installation can inspect models before
its first midnight capture. Current devices without a graph remain visible as
explicit current-only rows and follow the same device filter. Historical devices
without a current match retain their archived graph without inferred current status.
Selection preference and completed comparisons remain separate from the active
model and descriptive accuracy. Failed reads clear stale evidence. Installation
contains Price sources and no longer requests or renders model status.

Independent fixtures prove midnight immutability, intraday replacement rejection, missing midnight observations, restart, atomic retry, 23/24/25-hour calendar days, whole-day completion and zero/missing actuals. Existing fixtures retain hourly integration, signed balances, corrections, counter resets, source replacement, control and PV exclusions. Storage contracts and cross-backend transfer include daily baselines, the day index and control evidence on SQLite, PostgreSQL, MariaDB and MySQL. Browser tests cover desktop, mobile and ingress with a different browser timezone. Native HA tests retain the existing Recorder transport. Synthetic fixtures are not evidence of forecast accuracy in a physical installation.

## One forecast workspace

Device forecasts live on Forecast quality. The shared navigation has one
forecast destination. The retained `/consumers` link opens this same page with
the Forecast quality navigation item active. Device selection and weather
sensitivity remain under Settings > Appliances. The consumer API and forecasting
models do not change.

Choose a day and optionally one device. Each chart keeps that day's frozen
midnight prediction beside the hourly measurements received so far. The default
shows the latest retained day and all devices. A running day shows its measurement
coverage without claiming a complete-day score. A missing midnight prediction is
not replaced by a current forecast. Device filtering does not fetch or recalculate
a forecast and stays selected across days when that device exists on both.

Daily forecast, measured total and difference lead each chart. Model details,
metrics, hourly provenance and automatic selection remain available on demand
below the comparison. The page uses the existing responsive theme, keyboard
controls and Home Assistant timezone on standalone and ingress routes.
