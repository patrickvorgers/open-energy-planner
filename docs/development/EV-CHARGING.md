# EV charging

## Ownership and setup

The app owns settings, charging-loss assumptions, departure calculation, durable
intent and joint optimization. The integration reads HA state and exposes virtual
controls. Physical actions belong to user-owned HA automations. The supplied
**Open Energy Planner EV charging** blueprint wires permission, power and stop
actions without helpers. Neither app nor integration calls a vehicle vendor.

The existing pinned-TLS HA connection carries EV observations, explicit vehicle
assignments and charging/departure intent. Its peer credential cannot read admin
archives or change vehicle/charge-point configuration. Those settings remain in
the authenticated app UI.

**Settings > Appliances > EV charging** separates vehicles from charge points.
This shares the Appliances category navigation with consumption/cycle devices
and electric boilers. Dirty settings block category changes. Existing versioned
configuration APIs, save safeguards, identities and stored values are retained.
Removing the only allowed vehicle of a charge point is blocked without changing
the draft. The message identifies dependent charge points. Edit their allowed
vehicles or remove those points first. Other vehicle removals delete the removed
vehicle's ID from each charge point's allowed-vehicle list. Charge-point IDs and
all other settings remain unchanged. Server-side validation remains authoritative.
**Energy > EV charging** separates physical sessions, complete charging requests
and retained request history. `POST /v1/ev/requests` creates or atomically edits
a request. `POST /v1/ev/cancel` cancels its current identity and recurrence.
The page prefills saved vehicle defaults but never changes them. Failed writes
retain the complete draft. Evidence refresh preserves it. An unchanged local
departure retains its original offset and seconds, including the second autumn
clock-fold occurrence. Only a changed value is converted through HA's timezone.
The bounded first implementation supports sixteen vehicle records and four points. Multiple cars
may use one point. Stable opaque IDs identify records. The HA device name names
a vehicle. Without a device name, the integration title supplies the name, with
the source name retained when no config entry exists. Charge points retain their
Energy source names. Vehicle selection starts with the integration type, then an
optional account/config-entry choice when several instances exist, then the
eligible device and percentage SoC source. A new vehicle always requires an
explicit integration type, even when only one is available. Below that level,
exactly one suitable account, device or SoC source is automatic. Ambiguity remains
explicit at every level. Service devices and devices
without a compatible SoC source are excluded. Sources without a registered device
use an explicit no-device group. Automatic choices are drafts until Save. Parent
changes clear incompatible draft sources, while matching choices, metadata refresh
and Cancel retain their existing boundaries. Charge points use the shared
Energy-device chooser before the typed configuration draft is created. The
Energy display name identifies the choice visually. Its `entity_id` and explicit
`power_entity` link supply the measurement pair, including meters without a
device registry entry. There are no device or integration prerequisites.
The selected Energy device's meter and power link are read-only in the editor.
Missing power links require correction in Home Assistant Energy before Save.
Refresh adopts that authoritative power binding while retaining other drafts.
The configuration boundary rejects missing links and manual power overrides.
Changing or removing the Energy power link withdraws scheduling eligibility until
the measurement pair is confirmed again. Physical connection and vehicle identity are
never inferred. Refresh also rereads current Energy consumer options without
replacing the configuration draft or saved version. Missing Energy meters remain
visible as unavailable and server-side membership validation rejects them.

The chooser excludes already assigned consumption devices, charge points and
boilers by meter identity. The shared source-ownership admission checks all
configuration writes under the settings lock before mutation. Edits retain an
existing assignment only when both the appliance ID and meter match. Cycles
cannot claim a specialized meter, and generic selection writes must retain the
forecast membership required by configured charge points and boilers.
Resolved vehicle filters remain pinned across refresh. Missing parents stay visible
as unavailable choices, preserving the original sensor until an explicit replacement.

The thin HA catalogue adapter projects `integration_domain` and the loaded
manifest's `integration_display_name` separately from `integration_id` and its
configured `integration_name`. A registry-only platform can supply its explicit
type when no manifest is loaded. `device_entry_type: service` carries HA's actual
service classification. These optional, bounded string fields are validated at
catalogue admission and transferred through the existing complete chooser path.
Provider options, credentials and current measurements are not forwarded. The
catalogue is temporary memory. This adds no storage format or migration layer.
Device, type and account filters remain editor state, never saved source identities.
Each vehicle links its own percentage SoC sensor, usable capacity
in kWh and maximum AC power in W. Capacity is usable energy, not gross pack size.
It also has default target SoC and departure clock, used only to prefill an explicitly created request.

Each point links its Energy Dashboard consumption meter, measured AC power and
vehicle-connected binary sensor. Its minimum/maximum active power and capability
are explicit: **Measurement only**, **On/off at fixed power** or **Variable power**.
The lower of vehicle/point maxima applies. For on/off control, this is the fixed
active rate for that assigned car. For variable power, a vehicle is excluded from
planning when its maximum is below the point's minimum. An 11-kW on/off point
therefore admits both a 3.68-kW one-phase car
and an 11-kW three-phase car with independently configured vehicle limits.
This is aggregate AC power modeling, not phase balancing or hardware protection.
Off remains available. The meter cannot
also be a shiftable cycle appliance or overlap another selected Energy consumer.
The app selects each charger's individual forecast automatically. Missing
component forecasts block replacement of its household contribution. They never
cause double counting or silently remove expected load.
Removing a point's consumer from Energy withdraws its EV scheduling eligibility
and marks the missing dashboard source. Restore or correct that source in Home
Assistant. Saved intent and historical evidence remain available.

## Identity and departures

A connected sensor proves presence, not identity. OEP never chooses the first car,
even if only one is allowed. Select **Connected vehicle** on the virtual point,
manually or using a HA automation that reliably knows the car. The selection is
bound to that connection nonce. Disconnect, unknown connection or an integration
restart creates a new nonce. An old assignment never authorizes another session.
Missing identity/SoC and ambiguous assignment block EV scheduling. Measurement-only
points remain ordinary measured household consumption.

Native vehicle buttons are **Plan charging request** and **Cancel charging request**.
The former creates a one-off request from defaults. Target, departure date and
time controls edit an existing request and are unavailable without one.
A connected, identified car still requires an explicit request before planning.
Each new plan recalculates useful energy in kWh from measured SoC:

`max(0, request.target_soc - actual_soc) / 100 * usable_capacity_kwh`

### Request lifecycle and automation actions

The app owns one active request per vehicle. A stable caller-supplied UUID
`request_id` makes creation retries idempotent, even after cancellation or restart.
Editing uses `expected_version` from the projected `charging_request.version`.
Cancelling requires that version and `vehicle_id`. Reusing a cancelled creation
ID never revives it. All outcomes remain in history. Removing a vehicle cancels
its active request and retains history. No defaults, historical demand, startup,
refresh or connection event implicitly creates a request.

`repeat_weekdays` is optional and defaults to `[]`, meaning one-off.
Values are distinct integers, Monday `0` through Sunday `6`. Each completed or
expired repeating occurrence creates at most one next occurrence on an explicitly
selected day. The next occurrence cannot start before the previous deadline and
cannot complete from the earlier occurrence's reading. Missed occurrences are
not replayed. Cancelling the current request stops this chain.

HA's installation timezone is authoritative. There is no OEP timezone setting.
Manual departures must be aware timestamps in the next thirty days. The browser
rejects nonexistent wall times. Default and recurring clocks skip nonexistent
spring-forward occurrences and use the first autumn occurrence once. Stored
unchanged timestamps preserve their exact offset and seconds.

The integration exposes `open_energy_planner.ev_schedule` and `ev_cancel` actions.
Both target exactly one virtual OEP vehicle using `device_id`. Schedule accepts
`request_id`, optional `target_soc`, aware `departure`, `repeat_weekdays` and
`expected_version`. Cancel requires `request_id` and `expected_version`. These
forward the same app lifecycle, never physical charging actions. For automation
retries retain the same UUID. For edits/cancel use the current request version.

```yaml
action: open_energy_planner.ev_schedule
data:
  device_id: YOUR_VIRTUAL_OEP_VEHICLE_DEVICE
  request_id: "01234567-89ab-4cde-8fab-0123456789ab"
  target_soc: 80
  departure: "2026-12-01T07:00:00+01:00"
  repeat_weekdays: [0, 1, 2, 3, 4]
```

The admin `GET /v1/ev/requests` returns at most 25 history records and `next_cursor`.
Continue with `?cursor=<next_cursor>`. History is not exposed through peer reads.
Peer writes permit schedule/cancel, never vehicle or charge-point configuration.
Any request edit or cancellation invalidates old plan commands through the EV
revision. Native command admission also checks the current request window and
published revision, independently of local SoC/session safety.

## Joint model and execution

EV AC intake joins the existing bus exactly once, jointly with PV, stationary
storage and cycle appliances. V2G is excluded. Negative prices do not authorize
charging beyond the requested target. Existing reserve recovery and admitted
cycle service retain priority. Next, earliest-departure cohorts maximize useful
EV charge. Equal-departure cars maximize combined useful kWh without a separate
fairness preference. The configured consumption/cost strategy follows. All stages
share the existing solver budget. An unproved primary stage publishes no new plan.

EV starts use UTC quarters throughout the horizon, independently of coarser
stationary control groups. Arrival/departure clip edge windows. Full variable-power
windows use continuous AC-watt setpoints. The existing whole-watt stationary
battery contract is unchanged. HA displays watts without decimals, while the
automation receives calculation precision. Physical charger rounding remains in
the user's action and measured SoC corrects the next plan. On/off uses the
applicable maximum. One final partial
window may stop early at a lower- or upper-limit power from a finite stop
catalogue. Other device commands keep their original groups when billing segments
split. Every segment checks actual active power. A short high-power pulse is
never averaged into a below-minimum setpoint or hidden grid peak. Optimality and
shortfall refer to these modeled controls, not arbitrary continuous-time switching.

Live work reserves up to 130 seconds for calculation and handover, derived from the
125-second worker deadline plus five seconds for HA. The worker deadline includes
the shared 120-second solver budget plus five seconds for process overhead.
When departure is sooner, readiness is clamped to one microsecond before departure.
A late result cannot publish after its first usable segment. New ordinary valid SoC readings wait for
the next throttled round. Settings, session, identity, departure and measurement
availability changes block publication. New SoC also blocks a candidate exceeding
physical capacity. The quarter/five-minute planning cadence is unchanged.

Independent validation checks identity, timing, no discharge, physical power,
AC energy, conversion, target bounds and bus conservation. Original plans retain
their own inputs and per-interval useful/AC energy. New readings do not rewrite
them. The app and native sensors show predicted departure SoC and shortfall
separately from measured SoC. These are expectations, not proof of physical charging.

Native extraction rounds each binary off-control selection to zero power and
energy. This removes numerical leakage from integer tolerances, which can
otherwise appear as a few microwatts of forbidden below-minimum charging.
Selected controls retain their actual values. Independent bus, power and energy
validation still rejects inconsistent candidates before publication.

**Charging requested** and **Requested charging power** are virtual permissions.
Local unknown identity, unplugging, reached measured target, pause or expiry
withdraw them even during an app outage. The blueprint uses the stop action for
unavailable permission/power. Choose immediate idempotent actions, never queued
delayed starts. Relevant capacities and limits have separate native sensors.
The blueprint also rejects permission and power entities from different virtual
points. A five-percentage-point vehicle SoC deviation from the published trajectory
requests a new round behind the normal five-minute throttle.

The balance chart includes EV AC consumption and predicted vehicle SoC. Charging
losses are already in AC intake, not added as stationary converter losses. Both
stationary-battery references retain identical planned EV service/timing. Their
savings do not claim a separately measured EV scheduling benefit.

## Plan-only demand and execution feedback

Every configured charge-point meter is excluded from the future household
component forecast over the full horizon, independent of connection, identity,
SoC availability, target completion and control capability. Only eligible live
`EVWork` contributes scheduled charging. There is no historical EV fallback or
inferred future arrival. Original component forecasts and actual meter histories
remain immutable evidence. The existing separation proof is still required.
`LiveInputs` blocks publication with `ev_forecast_boundary_unavailable` when
the EV service cannot establish the forecast boundary. The fixed log-safe recovery
notice links to the authenticated EV page, which names the configured meters.
The EV domain owns both projections. Thermal fallback may retain a boiler forecast but cannot
silently restore an EV forecast.

`EVService.execution_deviated` compares fresh point power with the active
published command, including zero when no EV command exists. A difference of at
least 500 W must span at least 60 seconds of advancing, chronologically accepted
EV report capture timestamps. Re-reading one retained report as wall time passes
does not confirm a deviation, acknowledge it or display execution advice. The
confirmation uses power report capture time, never the SoC sensor's last change.
Missing readings reset the episode
rather than proving a stopped charge. Session or requested-power changes and a
material measured-power change restart confirmation. The existing five-minute
early-round throttle remains authoritative. Submission acknowledges mature
episodes so an unchanged refusal does not repeatedly request early rounds.
Normal quarter-hour planning is unchanged. The advisory session explanation does
not claim to distinguish vehicle refusal from EVSE or automation blocking.
This episode state is process-local and is not restored as live evidence.

Execution advice is shown only for a still-usable published plan with the current
EV revision. Missing, expired or invalidated plans cannot retain stale advice.
Replacing configuration clears obsolete forecast-boundary diagnostics.

## Default charging losses

No mandatory loss setting or manufacturer catalogue is introduced. Versioned
planning defaults assume 20% AC loss at 2.3 kW, 15% at 4.1 kW and 10% at 11 kW and
above. Delivered watts are interpolated between anchors. Below 2.3 kW the first
segment assumes 80% efficiency. These are planning assumptions, not accuracy or
manufacturer guarantees. Temperature and battery conditioning can change losses.

The basis is [ADAC's measurements published on 6 August 2026](https://www.adac.de/rund-ums-fahrzeug/elektromobilitaet/laden/ladeverluste-elektroauto-studie/).
Observed ranges across five cars were 12.7-24.2% at 2.3 kW, 8.0-12.8% at 4.1 kW
and 5.1-7.0% at 11 kW in warm conditions. Higher-power defaults are conservatively
rounded. Automatic efficiency learning and paid vehicle APIs are outside this step.

## Storage and laboratories

EV settings/intent use the shared SQLAlchemy `planning` document namespace. State
versions and current pointer commit atomically and join full cross-backend
transfer. No separate database/configuration file is created. Fresh HA telemetry
is mandatory after app restart. Credentials remain outside these documents.

SoC reports retain separate sample/report timestamps and timestamp provenance.
The app accepts up to five seconds of future clock skew between hosts. The
120-second report-capture expiry is independent of the original sample age.
An optional **SoC timestamp attribute** preserves the original device-sample
timestamp as provenance. Without that attribute, the original HA report time is
labelled `ha_report`. Neither timestamp proves that the vehicle is communicating.
A valid current capture is not rejected solely because its sample timestamp is
old. Expired captures are unavailable, including in native sensors.
A local connection listener
rotates nonces independently of HTTP, including off/on transitions between polls.

The native execution response omits archived physics/financial arrays. Adjacent
identical command segments coalesce, while the original fine physical intervals
remain in the immutable plan archive and browser evidence. This keeps HA packets
bounded without averaging away EV stops or losing validation evidence.

New financial model-selection comparisons pause when the original plan contains
controlled EV or cycle work. The current replay evaluator lacks the matching
realized controlled-load boundary, so using it would fabricate comparable scores.
Existing valid completed evidence and preferred models remain available. The
model overview exposes this reason instead of silently promoting a candidate.

Both laboratories have two simulated cars and two wallboxes. Their new Energy
meters receive clearly synthetic zero-use history before their first connection,
not invented charging sessions. Subsequent AC energy and SoC persist. Fixture
actions affect simulated devices only. The main simulated grid includes new EV
AC consumption once, preserving monotonic import/export counters. Existing
installation, contract, history and pairing configuration remain intact.

Persistent lab startup runs Recorder-dependent initialization after HA has started.
Restored unavailable placeholders for fixture-owned energy meters recover the
fixture's known units and classes. External Energy meters still require their own
original units. Reload retains numeric counters, imported history and Energy
preferences. Independent native tests cover absent and restored placeholders with
both grid-power configuration forms and reject missing external meter units.

### Repairing inconsistent laboratory evidence

The simulated heat pump uses a share of uncontrolled household demand. Its
historical reconstruction subtracts measured dishwasher and EV consumption
before taking that share. Its live quarter profile excludes the seeded historic
dishwasher cycle. If existing grid evidence cannot support that separation, the
fixture retains the entire previous consumer archive until the evidence is repaired.

The laboratory-only admin action `oep_test_fixture.repair_energy_balance` previews
complete hours where measured controlled consumers exceed reconstructed household
energy, and detects discontinuities in the simulated grid sum origins. The Energy
source list must exactly match the channels actively claimed by this fixture's
setup. Newly added or foreign Energy sources stop repair before reading or writing
statistics. Existing-energy mode explicitly claims the inherited test-VM entities
that it simulates. Their original entity registry platforms remain unchanged.
The derived
heat-pump archive is rebuilt after repairing its raw inputs. It is not treated as
independent evidence for grid repair. The action requires
complete five-minute evidence for each affected hour. A missing non-grid meter
point or span can establish zero energy only when its two bounding cumulative readings
are equal within counter precision. The preview identifies these proven constant
spans. The missing source row remains missing. Other gaps stop the repair.
Each step uses the known
simulated baseload plus measured controlled consumption, PV and battery flows.
Import/export is then derived from that step's signed balance. Hourly totals are
the sum of the twelve resulting five-minute flows. Unrelated deltas remain intact.
Recorder normalizes source units to kWh. Replacement imports convert back to each
original unit and preserve counter states, entity
identities, Energy preferences and app configuration. Controlled-device resets and missing
fine evidence stop the repair instead of inventing source measurements. The fine
grid sum origin is aligned with the last common completed hour. Home Assistant
copies the last five-minute sum when compiling an hour. Keeping those origins
aligned prevents a new discontinuity during the next compilation.

The earlier simulator upgrade could also leave a PV or stationary-battery sum
origin drop within a retained five-minute hour. These test sources have a known
canonical profile. At an adjacent negative transition the repair reconstructs
the source's complete affected hour from this simulator profile, shifts subsequent
sums and aligns the fine origin with the hourly origin. Reconstructing the complete
hour removes both sides of a false positive-then-negative spike. Correcting only
the negative side would invent production. The preview identifies each source,
affected hour and expected energy. Recorded counter states are retained.
This reconstruction is only for the lab's synthetic PV and battery channels.
It must never be applied to production measurements or controlled EV/appliance
meters, whose recorded consumption remains the independent evidence.
The owning fixture supplies its original quarter-kWh sampler. Default mode keeps
the roof and garage profiles distinct. Existing-energy mode uses the same share
allocation as its original seed. Recorder reads and reconstruction are normalized
to kWh. Only replacement serialization converts back to the original Wh/kWh/MWh
unit. The sampler must not apply that unit conversion a second time.

Read the preview before applying it. Supply `apply: true` and its exact
`expected_revision`. A durable journal is written before queued corrections.
Original affected rows are retained in that journal. Apply avoids the twenty
seconds around a Recorder compilation boundary. Ambiguous interrupted operations
require inspection and are never replayed automatically. Successful readback must
match every queued replacement and find no remaining impossible hours or negative
fine grid deltas.
Repeating a completed repair does not apply the adjustment again. No physical
household measurements or production integrations should use this synthetic
baseline reconstruction. Original plan and forecast archives remain unchanged.

All payload conversions are checked before marking the journal as applying. To
resolve an interrupted operation, call the same admin action with
`resolve_ambiguous_revision` set to the journal's exact proposal revision. Do not
combine this with `apply`. Recovery only closes the journal when every affected
row still equals its journalled original, or every row equals its intended
replacement. Mixed, expired or unverifiable outcomes remain blocked. This action
never requeues replacements or edits Home Assistant storage files manually.
