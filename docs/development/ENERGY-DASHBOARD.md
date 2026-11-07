# Home Assistant Energy mapping

Settings > Dashboards > Energy is authoritative. OEP's wizard displays its meters,
SoC, normalized power direction and battery usable capacity read-only. Save APIs
enforce the same contract. Source discovery does not depend on current readings or
Recorder history: a configured source remains visible before it has measured data.
Missing required settings instruct the user to complete Energy, not create a
second installation definition in OEP.

Read the Energy manager independently of whether the Energy UI component has
loaded. Refresh the early catalogue once Core reports startup complete so entity
metadata placeholders do not require a manual refresh. Invalid or oversized
battery links and nonfinite/nonpositive/out-of-range capacity become missing
fields with correction guidance. A ready wire catalogue must explicitly carry
grid power channels and every battery capacity/charge-link field. Omitting keys
cannot bypass the app's ownership checks.

| Energy setting | OEP use |
| --- | --- |
| Every grid `stat_energy_from` | Separate cumulative import channel for household demand, measured settlement and period counters. |
| Every grid `stat_energy_to` | Separate cumulative export channel for demand subtraction, settlement and bonus budgets. Export is optional for a connection. A PV/storage installation needs configured export coverage. |
| Every grid `stat_rate` | Instantaneous normalized power. Import positive, export negative. Sum all distinct configured channels for deviation detection. At least one is required. Tariff rows can share one physical meter, so they do not each require a separate power sensor. |
| Solar `stat_energy_from` | Separate production identity, measured production and per-source forecasting/training. |
| Battery `stat_energy_from` | Cumulative discharge, added to whole-house energy. This preserves the existing device identity. |
| Battery `stat_energy_to` | Cumulative charge, subtracted from whole-house energy and shown separately on the virtual device. |
| Battery `stat_soc` | Measured percentage is required to calculate current internal stored kWh. |
| Battery `stat_rate` | Required normalized terminal power. Discharge positive. HA owns power inversion/combination through `power_config`. |
| Battery `capacity` | Usable kWh, reused without another capacity question. SoC scales against this frozen capacity in each plan. |
| Explicit Energy names, device/sensor names and IDs | Energy's nonblank name takes precedence, followed by HA device and sensor names and a generic source label. IDs remain stable. |
| HA location/timezone | Weather, solar/daylight windows and local forecast/calendar boundaries. |

Energy counters use cumulative `total_increasing` kWh meters. Existing normalization
also accepts compatible Wh/MWh and HA `total` energy statistics. OEP converts to
kWh before arithmetic. Instantaneous W/kW are not substituted for cumulative
settlement data. Missing measurements remain unknown, never zero.

## Power measurement modes

Grid and battery use the same adapter: read Energy's normalized `stat_rate`,
not the raw `power_config` inputs. Home Assistant creates and maintains the
inverted/combined entities. OEP must not invert or subtract their values again.

| Energy selection | HA normalization | OEP behaviour |
| --- | --- | --- |
| Standard | Original signed sensor | Convert supported W/kW to W without changing the sign. |
| Inverted | Negate the original sensor | Read HA's generated entity without a second inversion. |
| Two sensors | `stat_rate_from - stat_rate_to`, normalizing source units | Read HA's generated entity. Both constituent sensors must be available. |
| No power sensor | No configured normalized entity | Preserve Energy device discovery, but block setup/planning until the required power is configured in Energy. |

Positive grid power means import. Negative means export. Positive battery power
means discharge. Negative means charge. These are measurements, not commands.
For example, 2 kW from minus 500 W to is +1500 W for either source type.

If either constituent sensor is absent, removed, `unknown` or `unavailable`,
HA marks the combined entity unavailable. OEP reports a required-source issue
and admits no replacement reading: neither zero, the remaining half nor the
previous combined value is used. Restoring both sensors restores measurement
readiness through ordinary state events. No reconfiguration is needed.
Temporary loss of a required measurement revokes active commands. Recovery
requires usable readings and acceptance of a newer plan. The original archived
plan remains available for history. Removing the configured required power link
is a configuration mismatch and blocks planning against the old definition.

`tests/custom_component/integration/contracts/test_catalogue.py` exercises all four modes for both grid and battery
using real HA Energy entities and OEP's in-process reader. It checks both
directions, mixed kW/W inputs and loss/recovery of either half. The app's
separate no-power membership regression lives in `tests/ha_contract/test_energy_authority.py`.

Whole-house demand is the sum of all imports, solar production and battery
discharge, minus all exports and battery charging. The existing history adapter
requests all channels. Settlement keeps gross import and export distinct even
when several connections operate in opposite directions. The installation planner
uses their aggregate grid boundary and configured total connection limits.

Bonus budgets likewise use all configured export meters. The history adapter
reads their common completed hours and normalizes/sums kWh while retaining each
physical meter endpoint. Current counters must all be fresh. A meter reset or
changed member set requires renewed baseline evidence. Growth in another meter
cannot hide a reset. The internal aggregate identity is not a published helper
sensor. Frozen-flow contract repricing uses all original export readings too.

The integration reads these settings through HA's Energy manager and statistics
APIs, never direct Recorder SQL. The app validates the bounded metadata contract,
archives valid changes and installs a matching required measurement generation.
Original plans and configuration versions remain unchanged. New batteries still
need OEP-only ratings/relationships. Missing SoC, power or capacity must be fixed
in Energy first. A removed required field blocks planning against the old settings.

## Event-driven source discovery

The integration subscribes before its initial capture. Active metadata contains
explicit Energy bindings and additional sources actually configured in the app.
These include EV state of charge, charge-point connection and power, and boiler
temperature, power and consumption. Disabled configured appliances retain their
bindings. Missing configured sources retain metadata placeholders. The integration
does not keep a permanent copy of every HA entity.

Entity and device registry events refresh affected active records. Value-only
state updates do not trigger metadata work. Energy preference updates rebuild its
dependency set. This includes the selected Forecast.Solar entries and all their
panel subentries. Only Forecast.Solar is supported. An unsupported or unreadable
selected provider remains explicitly incomplete and does not replace retained
panel settings with partial geometry. Core configuration events refresh HA-owned
timezone, country and temperature units. Recorder statistics events update compact
history independently from entity metadata.

There is no periodic full metadata audit. Initialisation, explicit refresh, failed
capture recovery, changes during capture or a rejected revision confirmation can
require a complete active snapshot. Failed metadata attempts use a five-minute
backoff. Failed publication retains a complete repair request, so the next attempt
after backoff does not depend on another metadata event. Unacknowledged data cannot
authorize freshness confirmation or solar forecast uploads. A new explicit request
bypasses the backoff. While the watches are healthy and the
accepted snapshot is unchanged, the integration confirms its exact revision at
most once per minute without reading source metadata. The app expires freshness
after fifteen minutes without confirmation. A pending partial transfer cannot be
confirmed. App restart or revision mismatch requires complete publication again.
This is connection/watch confirmation, not a sensor-value heartbeat or proof that
an upstream provider generated new measurements.

Explicit active refresh requests coalesce and expire after sixty seconds if the
peer does not report an outcome. Expiry preserves the accepted data and permits
a fresh explicit nonce. A delayed failure for an old nonce cannot cancel a newer
request. No timeout schedules a periodic full scan.

Forecast capture remains independent. Provider updates and the existing
five-minute forecast fallback use the last accepted normalized solar context,
only while discovery is healthy. An unchanged metadata catalogue does not stop
forecast refreshes. Forecast uploads retain the complete independent meter set.

### On-demand appliance choices

Opening Add or Edit in EV or electric-boiler settings requests the corresponding
finite chooser scope. The refresh button repeats that explicit request without
discarding inputs. EV candidates include compatible percentage and power sensors
and binary sensors. Boiler candidates include compatible temperature and power
sensors and water heaters. Energy-owned consumption choices remain authoritative.
All matching sources are collected without a count or alphabetical cutoff.

The admin requests `/v1/ha/source-choices/{scope}`. The authenticated integration
receives only the finite scope and nonce in its snapshot and submits complete
bounded pages to `/v1/ha/source-choices`. The peer cannot initiate chooser requests.
Each scope has one pending request, expires after sixty seconds and retains an
accepted result for five minutes. A failed or expired request requires a new
explicit request. Old nonces cannot publish into a newer request.

Candidates use a separate assembler and validation path. Partial results are
private. Complete results augment editor choices and write-time source validation
only. They never replace active membership, reconcile Energy, refresh active
freshness or supply forecasts. After a configuration is saved, its explicit source
bindings join the active discovery set on the next synchronization. Temporary
candidate results expire without changing the saved configuration.
An overlapping active record always takes precedence over a temporary candidate.
Older chooser metadata cannot restore a source whose current active record is
unavailable or has incompatible units.
Ordinary EV and boiler status endpoints still return only active metadata. Pending
chooser status omits candidate bodies. A large candidate list is returned only
through the explicit completed chooser read, rather than on each monitoring poll.

## Complete discovery transfers

The integration captures all active source metadata, Energy suggestions and
compact history endpoints as one snapshot. A small snapshot remains compatible
with the single-request catalogue contract. Larger snapshots use ordered HTTP
pages, each at most 192 KiB including JSON escaping and protocol overhead.
The authenticated request limit remains 1 MiB. No entity-count or alphabetical
cutoff decides which sources can appear in settings.

The serializer prepares and validates all pages before sending the first one.
Optional solar configuration groups are measured with their actual transfer
identity, timestamp, page index and JSON envelope. If a complete group cannot
fit, only that group's provider geometry becomes explicitly incomplete. Its
production meter and all other source metadata remain available. The serializer
never imports only some planes from a group. A group that fits, including one
exactly at the page budget, retains every plane. The final digest covers the
normalized snapshot. The captured input is not mutated. An oversized mandatory
record, header or incomplete-group marker fails before any page is sent.
The prepared result keeps this normalized snapshot together with its wire pages.
After the app accepts the final catalogue message, the coordinator captures
forecasts from that same snapshot. Incomplete groups do not enter forecast
capture, while independent complete meters can still upload their predictions.
Partial, rejected or mixed transfers never supply a new forecast context.
The paired TLS gateway admits forecast submission only through the exact
`POST /v1/solar/ha-forecasts` operation. Current Energy provider associations are
validated before persistence. Solar configuration and other HTTP methods remain
outside this peer permission.

Each paged message carries a transfer identity, capture timestamp, content digest,
zero-based index and completion flag. The first message supplies the document
header. Subsequent pages append to explicitly allowed collection paths. The final
message must reproduce the SHA-256 digest of the complete canonical JSON document.
The receiver admits one pending transfer, with a 32 MiB aggregate byte budget and
a five-minute lifetime. It rejects older starts, repeated or out-of-order pages,
mixed identities, unsupported paths and corrupt completion. A future capture may
lead the app clock by at most thirty seconds.

Pending pages do not update the public catalogue, its freshness timestamp,
consumer membership or installation reconciliation. Only complete, validated
discovery enters the existing publication and reconciliation path. A failure or
interruption before publication retains the last complete snapshot. Publication
precedes reconciliation and the final reply. An error after publication or a lost
final reply can therefore leave the new complete catalogue visible even though
the sender did not receive acceptance. Optional discovery
failures remain isolated from current-plan synchronization. The release identity,
authentication, request deadline and malformed-input boundaries remain in force.

Configured device and energy-channel counts are not product limits. Recorder
queries process at most sixty-four channels per call while retaining all members.
Existing history windows, query deadlines, response budgets and physical-value
validation remain active. The HA response reader retains its 256 KiB ordinary
response budget and 4 MiB snapshot budget. Catalogue acceptance replies use the
snapshot budget because they return the complete accepted device membership.
This does not widen ordinary settings responses or the bounded request pages.
Decoded device projections enforce the
same byte budgets. A normalized settlement frame is bounded by 192 KiB rather
than by an arbitrary number of channels.
The integration separately limits each serialized settlement report to 192 KiB,
including request identity, imports, exports and frames. Capture stops before
adding a frame that would exceed this limit. If no frame fits, it returns no report.

## Source display names

The explicit name of a source in Energy is authoritative for its presentation.
Use the HA device name, sensor name and a generic label such as Battery or Solar
only as successive fallbacks. Preserve user/provider spelling and punctuation.
Sensor identities remain available alongside labels when identifying a particular
measurement, including the shared notification banner and drawer. The current
Energy label is followed by the exact sensor ID and its specific failure cause.
Aggregates such as Grid and Household consumption retain their
aggregate labels instead of inheriting one contributing meter's name.

The integration includes bounded optional `energy_name` metadata in its existing
catalogue upload. It refreshes every five minutes. Open Installation pages read
that metadata during their existing 30-second status poll without reloading
configuration, changing the selected device or discarding drafts. Read-only plan
and forecast-quality pages check a name revision every 30 seconds and refresh
their selected plan or day when it changes. A connected visible page therefore
normally shows a change after the next integration refresh plus its next browser
poll. A restart, manual save or relinking is not required. Read failures retain
the last displayed labels and retry. Hidden pages can defer their refresh.

Historical views resolve current names through their frozen source identities.
Removed or unavailable sources retain the name captured with the original plan
or daily forecast. Original archive documents, input hashes, forecast versions,
measurements, calculations and physical commands remain unchanged. Current names
are a response projection, not a rewrite of historical evidence. Display-only
consumer renames do not invalidate forecasts or their configuration versions.
Explicit HA user overrides on OEP virtual devices remain HA-owned overrides.

Not every Energy field belongs to optimization input. HA's fixed prices, cost
statistics and cost adjustments do not override OEP's effective-dated contract
formulas/bonuses. HA's forecast integration choice does not replace OEP's local
48-hour solar model. Gas, water and dashboard presentation settings are outside
the electricity installation model. Equipment ratings, converter relationships,
efficiencies, reserves and control permissions remain OEP settings because Energy
does not define these planner constraints.
