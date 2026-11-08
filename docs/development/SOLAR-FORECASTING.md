# Local solar forecasting

This reference defines the solar forecasting contracts for FORE-11–14 and FORE-16
in the app/integration chain. For setup and interpretation, start with the
[user forecast guide](../users/forecasts.md). Installation compilation and UI
ownership are described in [installation planning](INSTALLATION-PLANNING.md).

## Setup and source identity

Open the app's `/setup` wizard, directly or through the integration/device configuration link, and use the **Solar production** step.
HA Energy defines the solar source catalogue. Missing/incomplete Energy configuration
must be corrected in Home Assistant. Sources cannot be independently added or
removed in the OEP wizard.
Only sensor statistics with Wh/kWh/MWh energy sums are supported by the current
Recorder bridge. External statistic IDs are not silently treated as sensors.

Select disjoint meters: never select a total together with its component meters.
The app rejects repeated IDs/meters, but HA Energy does not establish arbitrary
meter-containment relationships. OEP retains those dashboard-defined identities.

For each source, OEP follows panel settings from Forecast.Solar entries explicitly
linked to that production meter in HA Energy. The configuration adapter reads Core
plane subentries, preserving Wp, azimuth clockwise from north and tilt in degrees.
Multiple selected entries and planes remain one measured production source.
Unsupported, incomplete or multiply assigned forecast entries require correction in HA.
Assignments are counted before normalizing individual provider groups. An
unsupported or malformed sibling cannot hide a forecast assigned to two meters.
Solcast is not supported. Missing metadata never removes part of a proposed array.

Linked panel groups are read-only in OEP. Fresh complete HA discovery updates saved
geometry automatically and refreshes forecast inputs. For a single plane, the imported
name is the Forecast.Solar entry title when present. Entries with multiple planes
use each plane's title. Empty titles use numbered group headings.
Each saved group keeps its name, capacity, azimuth and tilt together. Names do not
determine geometry. Temporary outages, stale discovery and missing or incomplete
provider metadata retain accepted geometry and HA ownership, including after restart.
OEP warns that settings cannot be refreshed. A first unreadable link has no assumed
capacity, azimuth or tilt. Unknown ownership also remains read-only before any
provider snapshot has been accepted. Only fresh complete discovery explicitly confirming that
the forecast link is absent enables manual editing. OEP preserves the last geometry
and shows an informational notice. Relinking automatically adopts HA geometry,
locks panel fields and explains replacement of previous manual values.
These informational change confirmations show a clock badge labelled **5 min**.
Hover, focus or tap the badge to read that the notification disappears automatically
after five minutes. This is the notification lifetime, not a countdown. Panel-setting
availability warnings have no clock badge and remain until OEP confirms recovery.
Before the first save, complete HA metadata fills previously blank linked geometry
and follows further provider updates without replacing physical inverter drafts.
Removing that still-unsaved link retains visible planes and physical drafts and
clears only the unaccepted provider association so manual setup can be saved.
Metadata refresh preserves unsaved drafts and their original version guard. If HA
changes saved geometry while editing, reload before saving. Cancel reloads accepted
settings. Immutable configuration versions retain original provider provenance.
New Energy solar sources remain an unsaved installation draft even when every
field is prefilled. Review the proposed physical ratings and save explicitly.
A failed solar or installation write retains that state and allows retrying
without editing a field. Confirmation follows successful installation persistence.
Adapters normalize to one shared contract in `solar_adapters.py`. A future provider
adds its reader to that registry rather than adding provider branches to setup,
catalogue validation or solar calculation. No credentials leave the adapter.

With no linked forecast, enter a name, panel kWp, tilt and azimuth for each
panel group in the same editor. Azimuth is clockwise from north, from 0 to 360
degrees. Enter exact degrees or choose one of eight compass shortcuts. A shortcut
sets the numeric angle. An arbitrary imported or typed angle remains exact and
uses Custom angle in the selector. The saved summary and input explanation pair
degrees with the nearest sixteen-point compass label, such as 205°: South-southwest.
Legacy manual wind directions expand into equivalent planes for editing.
East-west retains its equal split at 90° and 270°. Opening or saving unrelated
settings does not replace unchanged legacy geometry. Editing those groups saves
explicit planes, allowing different ratings and angles for each group.
Exact imported planes replace the
approximate orientation in PVWatts. A suggested inverter identity represents a separate converter.
Sources may share an identity and must agree on its rating/properties. The app collects the AC rating, hybrid flag and curtailment permission. Existing
advanced measurement-side settings are retained when editing.
Confirm the AC converter rating separately from panel kWp. The wizard does not
claim to discover physical nameplate ratings. Forecast.Solar inverter limits describe
its forecast model only. They never prefill or overwrite the physical inverter's
identity, AC rating, efficiency or hybrid settings.
HA Energy is never modified. All OEP settings persist in the app. No solar sources in HA Energy means no solar installations in OEP. Historical archives remain.

## Model and responsibility boundaries

| Layer | Responsibility |
|---|---|
| Thin HA integration | Forward Energy sources, normalized Recorder history and HA location. App owns settings |
| Solar weather adapter | Fetch Open-Meteo GHI/DNI/DHI, air temperature and wind, validate units, normalize interval labels |
| Physical model adapter | pvlib-python 0.16.1 solar geometry, irradiance transposition, Faiman temperature and PVWatts DC output |
| Solar learning | Independent per-source CPU XGBoost production model, independent of PVWatts output and HTTP/HA objects |
| App runtime/storage | Coalesced model jobs, current weather/history, stale-result protection and immutable evidence |
| Planning-basis adapter | Replace PV energy while retaining upstream physical installation/load/price settings |
| Existing planner | Optimize and independently validate energy flows and costs. Never operate physical equipment |

Startup assumptions are tilt 30 degrees, isotropic sky, albedo 0.2, temperature
coefficient -0.004/C and Faiman defaults u0=25/u1=6.84. Wind at 10 m is used as an
approximation for module-level wind. No claim of a measured module temperature is
made. East-west initially splits panel capacity equally. No panel catalogue,
precise roof geometry or mandatory DC measurement sensor is required.

`pdc0` is entered panel Wp, divided equally for the two east-west orientations.
It has no fabricated capacity default. Transposed POA is used as effective
irradiance with IAM=1 and no spectral correction. Transposition includes geometric
incidence, but additional AOI reflection losses are intentionally not estimated
in this provisional model. The input/default convention is `pvwatts-v2` and must
be versioned if changed.

The physical model produces available DC energy. Each standalone inverter's AC
estimate applies its declared constant efficiency (default 0.96) and shared AC
rating once. This uses the existing installation efficiency convention, rather
than claiming a measured part-load efficiency curve. Multiple arrays sharing an
inverter share its cap. Different inverters retain independent caps. Night is zero
from solar geometry. Missing radiation is not zero sunlight.

The [configured installation path](INSTALLATION-PLANNING.md) uses canonical
per-source DC, local converter/battery links and per-device schema-2 commands.
It applies conversion losses and shared ratings at each declared local bus,
including hybrid PV/battery connections. No physical driver is implied.
Original source IDs, DC/AC forecasts and configuration remain archived evidence.

The installation compiler indexes source rows once, excludes elapsed cached
intervals and clips the current interval at now. It shortens only the future
tail to the common available solar/demand coverage. Missing coverage inside that
horizon remains an explicit fault. No future PV is extrapolated and historical
forecast evidence remains unchanged. Injected test bases are no longer wrapped
in a separate aggregate AC projection. Live forecasting uses the installation
compiler directly.

## Horizon, weather and lifecycle

Each source gets 48 elapsed UTC hours: 96 quarters followed by 24 hourly intervals.
Four calendar days of hourly forecast weather provide sufficient coverage even
late in a day. Actual coverage is checked, not inferred from the requested range.
PVWatts v2 evaluates solar geometry at each quarter's midpoint while retaining
the original hourly mean weather. Each source's DC and shared inverter AC cap are
computed per quarter, then integrated by overlap onto the mixed planning grid.
Hourly calibration output is the sum of those same quarters. This is physical
within-hour geometry, not native quarter weather or predicted cloud changes. API radiation labels represent the preceding
hour. Normalization subtracts one hour. End-sampled temperature/wind approximate
conditions over that radiation interval. DST does not add/remove elapsed hours.
The Open-Meteo adapter floors finite negative GHI, DNI and DHI values at zero
before constructing solar weather hours. Temperature and wind are unchanged.
Missing radiation remains absent. Nonfinite, nonnumeric and excessive quantities
remain invalid. This provider normalization does not alter measured energy or
previously archived forecasts.

Weather is fetched once per site, hourly, independently of model/solver polling.
Failures retry after 30 minutes. The optional historical archive is cached daily.
Its outage cannot discard a usable current forecast. Site changes clear old-site
weather. Requests are bounded to 15 seconds, 4 MiB and 9000 hours, with no redirects.
HTTP failures retain only forecasts within normal validity. There is no forecast
API key setting. External API access/distribution terms still apply separately
from pvlib's BSD-3-Clause licence and require production distribution assessment.

Background model jobs are coalesced by configuration, site, weather/history
versions and quarter. A five-second poll does not imply a five-second model fit.
Failed unchanged work backs off five minutes. Model work and archive writes run
off the event loop. Cancellation drains outstanding immutable writes. Delayed
results cannot replace newer inputs. Planning requires a matching-site prediction
less than 30 minutes old. Weather older than two hours cannot create a new one.
These are implementation defaults to validate in practice, not field reliability
guarantees. Existing planner cadence remains separate.

The solar module owns its forecast availability notification. It names the solar
forecast explicitly and explains automatic weather retries and forecast refresh.
The warning clears only when a forecast matches the current configuration and HA
location and satisfies the same issuance/expiry boundaries as planning admission.
Receiving new weather alone does not confirm recovery. Unknown source state keeps
the last warning explicitly stale. An installation without configured PV has no
solar forecast warning. Planning may separately show calculation recovery until
a new plan is successfully published. Notification views display the explanation
without technical-detail disclosures. Authenticated API diagnostics remain available.

## Learning from existing production history

The integration reads bounded annual cumulative Recorder sums per source, daily.
Counter resets normalized by Recorder remain valid. Nonconsecutive/decreasing
statistics are not converted into invented energy. Missing/incompatible history
produces a deduplicated HA notification naming the affected sensors. A failed
source advances to the next source and retries after 15 minutes, so it cannot
block all other inverters. The physical model remains available without history.

The production candidate is an independent CPU XGBoost model per independently
metered source. Its target is the measured hourly kWh at the declared AC or DC
boundary. Its features are UTC calendar cycles, GHI/DNI/DHI, temperature, wind
and solar position. Neither PVWatts output
nor a measured-to-PVWatts ratio enters the learned features or target. The fixed
CPU model uses 96 trees of depth three and one native thread. The shared CPU
runtime does not share model policies with other learning domains.

Daylight is determined from solar position, independently of either model's
predicted yield. Reliable daylight zeros, low production and production greater
than PVWatts remain admitted. Gaps, decreasing cumulative sums, incomplete hours,
missing or not-yet-received weather and values beyond the declared physical
boundary are excluded with owned reasons. Nighttime output is zero. Original
cumulative readings and weather remain unchanged.

The irradiance/yield diagnostic compares measured kWh per GHI within the same UTC
hour and 45-day seasonal group. At least eight training samples are required,
and very low irradiance is not divided into a ratio. Its median/IQR fence marks
unusual training hours without removing them. A separate diagnostic can mark
zero production under high irradiance on a curtailable source. Neither a ratio,
a statistical anomaly nor a price signal proves executed curtailment. Reliable
held-out production is never removed merely because either model predicts it
poorly. Training and validation share measurement-quality rules, not a minimum
PVWatts production threshold or a maximum correction factor.

Training needs at least 100 admitted daylight hours on 28 dates before a separate
final seven-day validation week. Validation needs at least 28 admitted daylight
hours spanning all seven dates. A dark day with valid zero readings contributes
normally. The public status distinguishes insufficient training history from
insufficient validation history and retains the actual hours, days and UTC
validation bounds. The chronological split precedes fitting. Held-out outcomes
never enter the learned model or diagnostic thresholds. An independent candidate
must improve historical hourly MAE over PVWatts by at least 0.005 kWh and 5%.
A tie retains PVWatts. These historical errors use the same retrieved weather for
both models. They do not establish weather-source accuracy or forecasts actually
issued before the measured outcomes.

The model predicts production directly within its measurement boundary and
physical power limits. Standalone AC production may be represented as equivalent
DC using declared conversion efficiency for planning. This does not reconstruct
unobserved clipped panel DC. Hybrid AC still needs independent route evidence.
Changing algorithm, feature policy, geometry or site starts an independent
comparison family. Earlier scalar or correction-factor evidence cannot promote
the new direct-production model. The scalar fitting utility does not provide a
runtime candidate or a fourth selectable solar source.

Weights use bounded, validated native JSON, never pickle. Training data and native
prediction objects have bounded in-memory caches. Immutable weights are stored
once, separately from training reports. Model metadata binds the measurement
boundary and maximum power as well as source, configuration, location, library,
training and weight identities. Public status exposes readiness, training and
validation counts, exclusion reasons and included diagnostic counts without native
model blobs.

Curtailable sources can declare an explicit unrestricted historical period with
`unrestricted_history_start` and `unrestricted_history_end`. Both aware UTC
endpoints are required together, ordered and not in the future. The end is
exclusive. The range is a user's statement that production was not limited,
not automatic detection or an inferred fact about all history. Training and
completed forward comparisons use only the confirmed unrestricted periods.
Unknown later periods remain excluded with `control_history_unconfirmed`.
Configuration versioning, database persistence and existing settings concurrency
checks bind this declaration to the source. Extending a confirmed past period
does not rewrite original forward captures or change their physical comparison
family. Current confirmed coverage determines which actual hours may score those
original predictions. No hardware operation is issued.

Even a historically better model remains in shadow mode until three complete,
matched HA-local days of predictions captured before their outcomes show material
improvement over PVWatts with the same absolute and relative gain requirements.
Manual **ML model** selection cannot bypass this forward gate. Automatic excludes
models that fail the gain gate and ranks the remaining eligible forecasts,
including Home Assistant. Missing or rejected ML falls back to PVWatts without
silently applying a scalar correction. Learning failures retain the physical
forecast and log safe, deduplicated recovery advice.

Forecast quality separates two comparisons:

- **Model comparison** controls weather input. Independent PVWatts and XGBoost
  use the same known weather and identical held-out measured daylight hours.
  Historical fitting and validation are clearly separate from forward evidence.
- **Complete forecast comparison** controls the evaluated production hours.
  It compares previously saved PVWatts, XGBoost and HA forecasts with the same
  later measurements. PVWatts and XGBoost share captured Open-Meteo weather.
  The HA provider retains its own weather and algorithm. This evaluates the
  complete forecast and determines Automatic selection. The provider's weather
  error cannot be isolated from its product forecast without separate evidence.

Older weather comes from the Open-Meteo archive. Recent weather and future
predictions come from the forecast endpoint with its recent past window. Neither
is a roof-mounted irradiance measurement. The independent DWD/EUMETSAT satellite
view remains descriptive evidence and does not silently replace training weather.
Receipt times are retained. Reanalysis never becomes an old issued forecast.

Automatic solar selection uses forecast accuracy rather than simulated decision
costs. Household economic selection in [model selection](MODEL-SELECTION.md)
remains separate. A smaller solar error does not guarantee a cheaper energy plan.
Baseline, independent candidate and selected forecasts remain archived separately.

## HA forecast adapters and source selection

Each source offers **Automatic**, **PVWatts**, **Home Assistant** and **ML model**.
ML remains unavailable until sufficient usable production history exists. Manual
Home Assistant selection never bypasses measurement-boundary or freshness
validation. ML choices require their own forward accuracy evidence. Unavailable choices explicitly fall back to
PVWatts. Forecast quality shows the requested and actual source, comparison coverage and MAE in kWh per hour.

Core Forecast.Solar is the first forecast adapter. The integration reads the Core
coordinator's cached native library estimate within this provider adapter. The library returns
period Wh at the interval end. Core's service description says start, but forwards
the raw values without that transformation. The official library's
[immutable fixture](https://github.com/home-assistant-libs/forecast_solar/blob/4ffa970d1da516d84da0065a3414d17b14a55a3a/tests/fixtures/forecast.json)
establishes the meaning independently: cumulative 465 minus 89 is the 376 Wh
ending at 08:00. The adapter assigns it to the interval beginning at 07:00.
It preserves native quarter intervals and partial sunrise/sunset periods rather
than using Core's shifted hourly bucket. Reading the original cache also avoids
silently changing semantics if Core fixes its service in a later version. Wh
converts to kWh once. No provider request or manual HA refresh is triggered. The registry in
`solar_forecasts.py` is the provider-specific boundary. Solcast is unsupported.

Missing daytime rows remain missing. Night is padded only outside explicit
zero-power sunrise/sunset endpoints from a complete provider day. Local dates and
DST use the provider timezone for that normalization. OEP never extends the last
provider day or one provider's horizon using another provider's values. All selected
entries behind a meter must cover an interval before their energy is summed.
Projection onto OEP's grid integrates overlap with the original provider interval,
assuming constant estimated energy density within that interval. It does not
claim finer cloud forecasts or measured quarter production. The archived native
start, duration and energy remain unchanged.

Provider issuance is unknown for this adapter and stays null. Initial observation
of HA's cache is retained separately. Only an observed successful HA coordinator
refresh establishes freshness for planning. Refreshes of unchanged values do not
rewrite the first observation. A reload or OEP restart waits for a newly observed
refresh rather than pretending the cache was just issued. Two-hour provider
freshness and the existing thirty-minute OEP forecast expiry are both enforced
at planning admission. HA unload rejects queued synchronization, waits for the
active exchange and then removes provider listeners. Deferred catalogue or cache
reads cannot start a new forecast upload after closure begins.

HA forecasts describe metered AC production. Eligible standalone sources use
AC divided by their declared converter efficiency as standalone-equivalent DC.
The planner then applies that conversion exactly once. This does not recover
clipped available panel DC. Hybrid routes, potential curtailment, DC meters and
independently metered sources sharing an inverter are ineligible for HA planning.
Their imported geometry remains usable by PVWatts. They retain an explicit reason
and baseline fallback. Explicit all-HA standalone installations do not require
the independent PVWatts weather service to produce a fresh HA forecast.
As with other installation forecasts, a shorter complete HA horizon clips the
future planning tail to its real end. It does not have to cover all 48 elapsed
hours. Missing intervals inside that horizon remain an error. No omitted tail
is interpreted as zero production.

Automatic captures each available candidate for the next HA local day before that
day begins. Capture waits until all currently usable candidates cover that day.
A short cached HA horizon cannot freeze a PVWatts-only comparison before the next
refresh extends it. Each source/day is frozen once. Later revisions do not replace it.
Selection requires three complete matched daily windows from the last thirty days,
using exactly the same hours and measurements for every currently available
candidate. DST contributes 23 or 25 hours. For fractional-offset timezones only
whole UTC Recorder hours inside the local day are compared. Boundary fractions
are not reconstructed. A missing cumulative endpoint, negative correction or
missing forecast excludes that day from the common cohort. MAE averages absolute
hourly kWh errors over the common hours. Ties retain PVWatts before ML before HA.
No prices, bonuses or simulated savings enter this ranking. Until a complete
cohort exists, Automatic retains PVWatts. A newly available ML or HA candidate
must build a common forward cohort before participating. Existing meter history
cannot recreate past forecasts.

Configuration, source binding and location changes separate evidence families.
Changing the requested source alone does not discard comparable evidence. Original
predictions, normalized HA observations, refresh receipts and source-choice evidence
remain in the active database. Recorder history corrections can change a later
derived ranking. They cannot overwrite the original forecasts or earlier choices.
HA entry/subentry revisions also scope the HA candidate cohort. The revision
hash uses the public entry identity and modification timestamp, never provider
credentials. A setting change invalidates the old coordinator's freshness until
Core reloads it and OEP observes a successful refresh. Previously good forecasts
from a different provider configuration cannot promote the new configuration.
Unreadable optional accuracy evidence retains the physical forecast with an
explicit comparison issue. It does not overwrite damaged evidence or block an
otherwise valid baseline. Manual selection does not depend on old accuracy data.

Per-source `calibration_status` distinguishes missing/insufficient history,
missing curtailment feedback, missing hybrid route history and a fitted shadow
candidate. It does not silently present all of these cases as a trained model.

## API and retained evidence

- `GET /v1/solar`: bounded settings/status and forecast/candidate metadata.
- `GET /v1/solar?series=1`: complete per-source interval DC/AC prediction detail.
- `PUT /v1/solar`: sources/inverters with `expected_version`. Unchanged saves keep
  forecasts/history and never activate a contract adapter.
- `POST /v1/solar/history`: one current versioned history response or explicit
  `recorder_unavailable`, `history_unavailable` or `unsupported_statistics` error.
- `POST /v1/solar/ha-forecasts`: bounded normalized provider groups against current
  exact HA Energy associations. No credentials or physical meter revision enter
  this contract. Repeated unchanged cache reads do not refresh or retrigger it.

The existing installation token protects every endpoint. The normal HA snapshot
contains metadata rather than 16 full forecast series, preserving bounded sync
messages. Full prediction details are available separately to future app UI work.

The `solar` document namespace in the active SQLAlchemy database archives
immutable settings, source history, weather,
physical/candidate forecasts and calibration parameters/references. Unchanged
daily value blocks are content-addressed and reused across overlapping annual
history/weather versions. Compact manifests retain receipt-time runs, so weather
values can be reused without losing knowledge-time evidence. No routine cleanup
is introduced. Lifetime disk usage is deliberately unbounded: quarter-hourly
physical/candidate forecasts and new receipt manifests remain archived, even
when daily values are reused. There is no guaranteed MB/day budget. It depends
on source count, available history and changing weather. Final storage must add
capacity monitoring, deployment sizing and lossless compaction before SD-card
deployment. Automatic retention/pruning would contradict the agreed permanent
historical evidence requirement.
SQLite, PostgreSQL, MariaDB and MySQL use the shared database contract and backend
transfer. See the [database dictionary](DATABASE-SCHEMA.md). Production volume
sizing and complete deployment backup/restore verification remain separate gates.

## Sources

- [pvlib PVWatts DC](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.pvsystem.pvwatts_dc.html)
- [pvlib Faiman temperature](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.temperature.faiman.html)
- [Open-Meteo weather variables and interval semantics](https://open-meteo.com/en/docs)
- [HA Energy source API implementation](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/energy/data.py)

Original v1 model archives retain their labels and values. New `pvwatts-v2` and
`pvwatts-calibrated-v2` identities separate the changed geometry from prior forward
model-selection evidence. A constant horizontal, diffuse-light forecast may
remain flat. No variation is invented for display.

Diffuse irradiance also needs within-hour timing: sun geometry alone leaves
overcast hours flat. The shared bounded hourly-slope helper reconstructs
GHI/DNI/DHI at quarter midpoints while preserving each hourly irradiance mean.
Temperature and wind retain their hourly means. This estimates a trend between
hour means. It does not predict measured quarter cloud changes. Missing adjacent
weather uses a zero boundary slope. No extra provider request is made.

The physical historical comparison aggregates quarter power arrays directly into hourly energy rows. It does not construct temporary quarter `SolarEnergy` objects for the complete annual archive. Independent XGBoost fitting retains its own measured target and solar-position features. All required historical hours and shared clipping calculations are retained.
