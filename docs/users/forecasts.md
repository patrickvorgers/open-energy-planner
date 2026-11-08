# Understand forecasts

OEP needs an expectation of household use, solar production and prices before it can plan. A forecast is an input to a decision. It is not a measurement, and a more detailed model is not automatically a better one.

## Household consumption

The household forecast starts from the configured energy boundary. Without sufficient usable history, an annual consumption estimate scales a provisional daily pattern. As suitable history becomes available, OEP can use weekday/hour patterns and evaluate a learned candidate.

The startup estimate includes the household's normal background use. There is no separate baseload value to add again. Selecting individual consumers can explain parts of the household total, but their consumption must not be counted a second time.

If a heat pump or another consumer is weather-sensitive, select that option under **Settings > Appliances** when appropriate. Weather sensitivity affects prediction. It does not authorize temperature control or cycle scheduling.

For these selected devices, a nonlinear weather candidate learns from completed
hourly consumption, outdoor temperature, local time, weekday, season and the
preceding day's measured consumption. The measured weekday profile and Ridge
regression remain available as simpler alternatives. Devices without weather
sensitivity retain their existing forecasting behavior.

After at least 28 days of usable paired consumption and temperature history,
the candidate saves predictions for a future day. Later measurements score those
original predictions. Three nonoverlapping complete days must each improve mean
absolute hourly error against both simpler alternatives by more than 5.0% and
0.001 kWh per hour. These are forward comparisons, not training fit or a random
split. A tie retains the simpler model. The household composition still needs
its existing independent decision comparison before activation.

Under **Insights > Forecast quality > Devices**, expand **Current model and
learning** to see the component's model, pending day, comparison errors and why
the candidate is waiting or rejected. Training coverage lists omitted periods
and explains missing temperature or incomplete preceding-day measurements.
Missing measurements, missing weather,
temperatures outside the observed training range or unusable model evidence
keep the simpler forecast. Real cold spells and defrost peaks are not removed
as statistical outliers. Weather-sensitive prediction does not introduce
operating-mode, indoor-temperature or setpoint inputs that the existing history
transport does not supply. Home Assistant retains comfort and safety control.

Unreliable history can be excluded from training without changing Home Assistant Recorder. Too little usable history keeps a simpler model available. A notification about substantial excluded history calls for checking overlapping or incorrectly signed Energy sources, not editing recorded values to match a prediction.

## Solar production

Solar forecasts combine the configured arrays, their orientation and tilt, inverter limits and weather inputs. Review these under **Settings > Installation > Solar production**.

When Home Assistant Energy links supported Forecast.Solar panel metadata to the production meter, those panel groups are read-only in OEP. The link supplies geometry, not automatic permission to change the physical inverter's rating. With a confirmed absent link, manual geometry can be edited. A temporary discovery failure must not be mistaken for removal of that ownership.

Available solar DC energy and measured inverter AC production are different quantities. Conversion losses, clipping, curtailment and hybrid battery flows can prevent a direct comparison. Do not infer forecast failure from two values that use different measurement boundaries.

Each solar source can use **PVWatts**, **ML model** or **Home Assistant**. These
are independent production predictions. PVWatts uses the installation geometry
and weather. The XGBoost ML model learns the metered production directly from
historical weather forecasts, sun position and time. It does not scale a PVWatts prediction. The
Home Assistant provider supplies its own forecast. **Automatic** chooses among
eligible sources using forecast accuracy. ML can establish eligibility from a
historical forecast backtest or from predictions saved before later measurements.
Insufficient or unproven learning keeps PVWatts available.

Solar learning imports the available production years from Home Assistant and
pairs usable measurements with archived ECMWF IFS weather forecasts. The live
PVWatts and ML service also uses ECMWF IFS, choosing the newest available run.
The individual transfers are bounded,
but the combined training history is not restricted to one year. Missing
readings or weather still reduce the usable coverage. Existing history can
already satisfy training and historical accuracy requirements. Importing several years does not require
waiting those years again.

The archived forecasts currently start in 2024, so older production readings
remain stored without being paired with reanalysis for training. Reanalysis
describes reconstructed weather rather than a forecast. It cannot substitute
for missing historical forecasts.

Training and validation measurements all count equally. When several archived
weather forecasts predict the same production hour, their samples share that
hour's total weight; repeated forecasts do not give it extra influence. No fixed
age discount is applied. The ML candidate can learn a positive or negative change in yield
over time, but uses it only after separate chronological comparisons show
consistent improvement across two complete seasonal validation years. It chooses
between a learned trend and no trend automatically. Insufficient or inconsistent
evidence keeps ordinary XGBoost available. A change in yield can include shading,
soiling or weather bias. It is not a measurement of physical panel degradation.
The trend needs about four years of comparable production and forecast history. Complete calendar-quarter
alignment, the separate final test week and missing data can require more. The
ordinary ML model can become usable much earlier without a trend.
With the current forecast archive, there will often be too little history for
this optional trend even when ordinary ML can train and pass its accuracy test.

After replacing panels, changing the meter or another fundamental installation
change, optionally set **Learning data from** for that solar source under
**Settings > Installation > Solar production**. The selected date starts at
midnight in Home Assistant time. Earlier history remains stored but no longer
trains or validates that source. Future dates are not accepted. Leave it empty to
use all otherwise eligible history. This is separate from confirming that a
curtailable source produced without restriction during a historical period.
Saving a different date starts a separate history import for that production
regime. A replacement meter can establish its own statistics identity without
rewriting earlier evidence. Clearing the date restores the original history
selection, including any unresolved continuity issue. The first learning hour
must start at or after the boundary.

Under **Current model and learning**, the Recorder availability month, imported
years and received reading count explain import progress. A monthly availability
date does not identify the first complete measured hour. The separate training
period and usable hour/day counts show what the model actually used. Historical
forecast runs are cached in the active database and reused for retraining and
after restart. Each run retains its model, site, initialization, query and actual
download time. A historical run's publication time can be unknown; the download
time is never presented as proof of past publication.

Unavailable archived forecast runs retry in the background, with increasing
intervals up to one day for a persistent gap. Other available runs keep importing,
and a gap does not stop a usable current forecast. Changing unrelated solar
settings reuses already validated meter history rather than reloading every
stored year.

The same disclosure explains whether a time trend was selected within the ML
candidate, its annual change and the seasonal comparison evidence. That internal
choice does not mean ML is the current forecast. Read **Active forecast model**
and the complete forecast comparison separately.

Reliable daylight measurements remain useful when production is zero, unusually
low or higher than PVWatts predicted. Daylight is determined from the sun's
position, independently of the predicted output. Missing or incomplete meter
hours, decreasing cumulative readings and physically invalid values cannot
establish production. Unusual relationships between irradiance and yield are
marked for review without discarding the measurements. A statistical anomaly or
low production alone does not prove that panels were switched off.

For a source that allows proposed solar on/off commands, **Historical production
for learning** offers **Confirmed unrestricted production from (UTC)** and
**Confirmed unrestricted production until (UTC)**. Set both only when you know
the source was not limited in that period. The end is exclusive and neither
endpoint can be in the future. This is your explicit historical declaration,
not an automatically detected state. Unconfirmed periods remain excluded for
that source. Extending a confirmed historical range does not confirm future
production or authorize a hardware command.

If you turn off **Allow proposed solar on/off commands**, the historical
confirmation fields become inactive. An unfinished draft no longer blocks
Continue or Save. Turning the option back on before saving restores that draft.
Saving with the option off keeps any previously saved confirmation; it does not
save an unfinished hidden replacement. **Cancel** restores the saved settings
and confirmation.

## Read Forecast quality

Open **Insights > Forecast quality** and choose a day above the **Household**,
**Solar** and **Devices** tabs. The same day applies to all three views. Solar and
Devices each show one selected source at a time. The page compares the original
midnight forecast with received hourly measurements for that same Home Assistant
calendar day. Measured energy uses fill-only bars. The originally selected model
uses a solid line and retained same-issue alternatives use dashed or dotted lines.
The comparison table keeps the original choice separate from later model status.

[![A populated forecast-quality chart comparing a fixed prediction with measured hourly energy.](../assets/forecast-quality.png)](../assets/forecast-quality.png)

*The prediction and measurement can have similar daily totals while differing around an evening peak. The chart makes that timing difference visible. These are synthetic teaching values, not measured forecast performance.*

Read the daily prediction, measured total and difference first. Then inspect the hourly chart. Missing measurements remain gaps. A running or incomplete day must not be interpreted as a scored full day. Clock-change days have their actual 23 or 25 hours.

Expand the metrics when you need them:

| Metric | What it helps you notice |
|---|---|
| Daily difference | Whether the predicted total was above or below the measured total |
| MAE | The average size of an hourly error, without positive and negative errors cancelling |
| Bias | Whether hourly predictions tend to be too high or too low |
| WAPE | Total absolute hourly error relative to measured energy, exposing timing errors hidden by daily totals |

A zero measured total has no meaningful relative percentage. Controlled appliance timing and some solar measurement boundaries can make an accuracy comparison ineligible even when values remain visible. Read the exclusion reason before comparing models.

## Compare irradiance separately

For Solar, optionally enable **Forecast irradiance** and **Satellite estimate**.
These use the right-hand W/m² axis, separate from production in kWh. Forecast
irradiance comes from the weather retained with the original prediction.
Satellite irradiance estimates sunlight from satellite observations. It is not
a sensor on your roof. The named DWD / EUMETSAT MTG source has approximately
20 minutes of provider delay and historical coverage from February 2026.
Actual availability depends on the response. Older or missing data stays unavailable.

**Satellite data complete through** identifies the last uninterrupted hour from
the selected day's start. A gap stops that marker even if later hours exist.
**Last fetched** only indicates when the app retrieved the observations.
Missing satellite hours do not affect the separate evaluation against measured
solar production. **Refresh evidence** retrieves newer observations without
replacing the original forecast. A failed satellite refresh retains available
observations with an explicit warning.

## Separate historical accuracy from the current model

**Current model and learning** describes the model available now. The chart retains the model that produced the selected day's midnight prediction. Refreshing evidence updates measurements and current readiness, not that original forecast.

For Solar, **Current forecast weather** names the current service and newest-run
selection. Actual download time is distinct from run initialization and provider
publication; unavailable publication metadata remains unknown.

The disclosure separates these comparisons:

| Comparison | What it tells you |
|---|---|
| **Complete forecast comparison** | How well independently saved PVWatts, XGBoost and Home Assistant predictions matched the same later production hours. PVWatts and XGBoost share captured ECMWF IFS forecasts. The Home Assistant provider uses its own weather. This assesses the complete forecast, including its weather source, and determines Automatic selection. |
| **Model comparison** | How PVWatts and XGBoost performed with the same archived weather forecasts and held-out measured production. The historical backtest simulates 48-hour plans at two origins per weather run. It does not claim that the app or provider actually published a forecast at those simulated origins. |
| **Historical forecast backtest** | Whether four predetermined weather runs met both 48-hour planning-origin accuracy requirements. Counts, per-origin errors and expandable simulated windows explain the evidence that can admit ML without waiting for new days. |

The model comparison reports training and validation hours and days separately.
Missing validation coverage is different from insufficient training history.
Excluded hours have their reasons. Hours marked for review remain included.
A complete, successful historical forecast backtest can make ML eligible without
waiting for three new days. It uses four predetermined weekly weather runs and
tests a 48-hour plan from each run's initialization and again 24 hours later.
Each origin group must improve accuracy. The two plans overlap: the assessment
has 384 forecast/measurement pairs across 288 different production hours, not
384 independent measured hours. Missing or insufficient evidence keeps the
physical forecast available. Independently captured real forecasts remain
visible as separate evidence and continue to support comparisons with Home Assistant.
The test schedule leaves at least 24 hours after its final measured hour for
Recorder data to arrive. The newest incomplete hours therefore do not force an
otherwise usable historical comparison to wait. A damaged saved comparison
keeps the physical forecast while learning reports the problem.

Household model selection separately compares simulated decisions. Simulated
decision costs are not measured savings. For actual metering and retrospective
evaluation, use [Savings](savings.md).

If a source or day is unavailable, check its specific reason in the page and [troubleshooting](troubleshooting.md). Developer details remain in [demand forecasting](../development/DEMAND-FORECASTING.md), [solar forecasting](../development/SOLAR-FORECASTING.md), [model selection](../development/MODEL-SELECTION.md) and [forecast quality contracts](../development/FORECAST-QUALITY.md).

**Next:** [Read your plan](read-your-plan.md).
