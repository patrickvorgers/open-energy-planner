# Automatic appliance cycle learning

Home Assistant Energy owns device membership and sensor bindings. OEP reuses
`device_consumption[].stat_consumption` as the cumulative energy meter and its
optional `stat_rate` as the power sensor. There is no second OEP sensor picker,
manual start/end entry, detection threshold or learning wizard. Settings remain
under Settings > Appliances. Readiness and finish-before requests stay under
Energy > Appliances. Historical use never grants readiness or start permission.

## Learning and manual fallback

The HA adapter reads the last seven days of Recorder five-minute statistics using
public APIs. It normalizes energy to kWh and power to W. Each upload contains
interval start, mean power, and cumulative energy at the interval end. Missing
values stay null. Negative Recorder power means also become null because no
signed-power conversion is defined for appliance consumption. They break a
candidate cycle instead of rejecting other valid history. Nonfinite values also
become null in the transport so uploads remain valid JSON. The shared app-side
validator treats numeric power/counter values outside its physical bounds as
unknown for both fine and hourly ingestion. Such faults break their run and
reduce usable coverage, while later valid cycles and batch progress continue.
Malformed types, schemas, chronology and source identities still reject the
upload. The app owns the bounds, avoiding separate HA copies of planning limits. Fine measured profiles never upsample hourly statistics. A separate hourly
estimate can support provisional planning as described below. After commands are
published, the coordinator schedules one cancellable background history worker.
It does not wait for Recorder or historical learning before handling commands.
Selected cycle-device pairs are processed sequentially. Meters owned by EV
charging or thermal storage are excluded using the app's membership projection.
Each pair starts a new refresh at most
every 30 minutes. A refresh may contain several bounded historical batches. This
cadence is separate from price updates and planner rounds. Failed pairs back off
too, while later pairs remain eligible. Unload cancels the worker and prevents
new scheduling. A failed fine read or upload does not block hourly processing
or mark its independent scan as failed. Authentication and release failures
still stop both paths for connection recovery. They are consumed once by the next
command synchronization through its existing recovery handlers. They cannot
poison every later attempt after the connection recovers. Recorder must actually retain five-minute
statistics for the selected sensors. Connecting a sensor cannot invent history.

The application recognizes activity above the larger of 10 W and the observed
low-decile power plus 5 W. Thirty quiet minutes bracket a complete cycle. At
least fifteen active minutes and a duration between fifteen minutes and twelve
hours are required. Shorter quiet pauses remain inside the cycle. Missing rows,
null readings, counter resets and unfinished cycles break the candidate. Recorder sums may
have negative arbitrary origins. Only their increments determine cycle energy.
A candidate with unusable energy resolution is skipped without blocking later
valid cycles. Power
finds boundaries. Cumulative energy supplies the actual kWh and quarter-hour
profile. The trailing quiet confirmation is not part of the profile.

Two complete fine detected cycles admit the learned average. Profiles average
aligned quarters with exponential time weights and pad shorter runs with zero. The longest observed rounded duration
is retained. Different appliance programs are not automatically identified.
Long internal pauses can resemble separate cycles. Continuous loads, anti-crease
runs, coarse sensors and changing standby behavior may remain unsuitable for
automatic learning. This statistical detector is not execution confirmation.
Actual started/completed feedback and safe start/cancellation actions remain
independent in the existing HA execution blueprint.

Until learning is ready, the user may enter only duration in minutes and total
energy in kWh. Both fields are required together. The manual profile spreads
energy uniformly, conservatively rounds the planning duration up to a quarter,
and preserves the entered kWh in the partial final phase. It does not reproduce
heater peaks. Its original values remain saved after learned evidence takes
over. The UI distinguishes Manual, Learned, Historical estimate and explicitly simulated
evidence. A ready fine model wins over both estimates. An explicit manual fallback
wins over an hourly estimate until the fine model becomes ready. Existing
explicitly confirmed fine cycle profiles also keep precedence over hourly
estimates. They are not silently downgraded when a backlog completes.
Missing power bindings point the user to Home Assistant Energy. Insufficient
history remains a waiting state. No extra detection configuration is requested.

## Historical backlog and provisional planning

On the first scan for an Energy energy/power pair, the HA adapter queries public
Recorder daily aggregates from the Unix epoch to the completed-hour boundary.
The first day with both power and energy establishes the examined range. Daily
rows only discover that range. They do not detect cycles or create a model.
Discovery has its own initial 60-second timeout. Fine reads and hourly batches
retain their 20-second deadline. These are bounded failure budgets, not measured
Raspberry Pi performance guarantees. Discovery runtime for eight years on the
owner's hardware has not been measured. A timeout retains the failed/retry state
rather than admitting partial discovery as complete.
No configured history limit truncates the hourly backlog, including eight years
when Recorder actually retains paired statistics that far back.

The app checkpoints an hourly cursor and projects `history_request` with exact
`start`/`end` core bounds and `read_start`/`read_end` query bounds. The adapter
returns these unchanged with hourly rows through `/v1/appliances/hourly_history`.
Discovery, failure and target bounds use `/v1/appliances/history_scan`. Both
operations validate the current source pair and exact fields. The app requests
seven-day core batches in
chronological order. Reads include thirteen preceding hours for the maximum
twelve-hour cycle plus its quiet predecessor and one following hour for quiet
confirmation. Each run belongs to the core containing its completion. The final
core ends one hour before the current UTC hour, reserving a completed closing
hour. Progress and admitted cycle archives commit atomically. Replayed batches
cannot increase coverage or cycle counts. Restart resumes the saved cursor.
Later refreshes extend the target and process only new core hours plus overlap.
Changing the power source withdraws its model and starts new discovery. Discovery
is not repeated to search for subsequently imported older prefixes of the same
source. Such a rescan is outside this increment.

Hourly detection uses the same activity threshold but requires one complete quiet
hour on each side and at least one active hour. Gaps, missing pairs, counter resets
and runs exceeding twelve hours are rejected. All occupied hours stay in the
estimated duration. Each measured hourly kWh delta is spread uniformly across
four estimated planning quarters. This preserves kWh, but cannot recover exact
start/end times, short power peaks or multiple runs within a single occupied
period. An `hourly_estimate` is never relabelled as fine measured evidence.

After the initial target is completely examined and at least two usable hourly
runs exist, OEP automatically uses the historical estimate when neither a ready
fine model nor an explicit manual fallback exists. This is the user-approved
provisional planning policy. It grants no shift permission, readiness or physical
authority. Two complete fine cycles replace the provisional model. Hourly and fine
models stay separate, so overlapping observations never count twice in one model.
Seven-day fine refreshes continue alongside the incremental hourly scan.

## Time weighting and visible evidence

Both automatic models use a fixed initial half-life of 90 days. A cycle exactly
90 days older has half the weight of a new cycle. For each chronological new run,
OEP decays the retained weight by `2 ** (-elapsed_days / 90)`, computes the weighted
mean with new weight one, and retains the combined weight. Calendar time, rather
than the number of later uses, determines ageing. This is an initial policy, not
an empirically proven optimum or a user setting. The longest observed duration
remains conservative and does not shrink with ageing. Programme classification,
seasonal programme models and adaptive half-life selection are outside this change.

Raw accepted cycle counts are distinct from the sum of recency weights at the
latest accepted cycle. For example, a .5-kWh run ninety days before a 1-kWh run
produces `(0.5 * 0.5 + 1) / 1.5 = 0.833333...` kWh, raw count two and combined
weight 1.5. The UI shows both counts and weights, including separate fine/hourly
models. A manual profile uses zero detected cycles. Available fine candidates
remain listed separately until they replace the estimate. An existing automatic aggregate without weights is reconstructed once
from its retained automatic cycle archives before accepting new evidence. Missing
archives refuse that update and leave the previous model usable.

Energy > Appliances shows received fine history, usable versus expected fine
intervals, examined hourly range, usable hourly intervals, accepted estimated
cycles, model counts and weights. A usable interval requires paired current power
and kWh plus an adjacent, nondecreasing previous kWh. The initial row is a baseline.
Missing core hours count against hourly coverage. Overlap hours do not inflate
coverage. Fine coverage only describes the received range, not all seven requested
days or all HA history. An examined range is never presented as continuous usable
history. Empty reads advance examination but add no usable hours or cycles.

Discovery has no invented percentage. Processing progress is the checkpoint's
elapsed UTC span divided by its target span, not an estimate of remaining runtime.
The UI distinguishes processing, complete, no paired history and failed/resumable
states. Settings > Appliances keeps a short planning-readiness summary and a
link to that evidence. Both pages poll read-only evidence every ten seconds while visible. Refresh
failure retains the previous evidence. Returning through browser history restarts
one timer. Evidence remains available before a profile or shift permission
exists. Refreshing Energy evidence preserves a drafted deadline and keeps
unchanged charts intact. Evidence formatting is refreshed too, so newly received HA timezone
metadata is not hidden behind an unchanged-data cache. Polling never replaces controls, drafts,
selection or the saved revision guarding configuration writes. A newly active
model can therefore legitimately reject a stale settings save, retaining its draft.

## Evidence and persistence

The app validates the uploaded power identity against the current Energy binding.
Detected cycles receive an identity based on source pair and UTC boundaries.
Original five-minute and hourly energy points, detected times, resolution quality
and synthetic provenance are archived in the shared planning document store. Duplicate uploads do not add
samples. A per-source completion watermark rejects overlapping revised ranges.
The last twelve cycle summaries are shown. Full archives remain retained.
Writes use the existing SQL transaction and application lock. Fine coverage and
hourly checkpoints use one mutable `appliance-progress.json` document in the planning namespace, joined
with the current model/task state at startup. Progress-only refreshes do not
create complete immutable state copies. Model and task transitions still retain
their immutable versions. Progress, accepted cycles and changed model/task
pointers commit atomically. Backend transfer retains all of them. Browser permission
and manual-profile writes carry the saved appliance revision, so one write
cannot silently mask a concurrent change before the next. The sixteen-device
limit and live projection count currently selected devices, excluding retained
records for removed appliances. Accepted tasks keep
their own frozen profiles. New learning cannot rewrite an already accepted run.

Changing/removing the power binding withdraws that learned profile, retains its
archives, and restores a saved manual fallback. Learning needs new evidence for
the changed source. Ordinary restarts retain profiles and deduplication evidence.
The legacy completed-cycle API remains for existing clients. The supplied
blueprint no longer submits cycles through it, avoiding duplicate learning.

## Development examples and proof

The fixture links the dishwasher, washing machine and dryer power sensors in
Home Assistant Energy. Its existing energy history and identities remain intact.
Provisioning generates labelled synthetic five-minute input histories and sends
them through `/v1/appliances/history`, the same application detector used by the
real HA adapter. It does not insert a learned profile or predetermined cycle
boundaries. These bootstrap histories are explicitly simulated inputs, not claims
about Recorder history before installation. The live fixture's subsequent sensor
statistics use the normal Recorder transport.

Dishwasher and washing machine have two cycles. Their detected active spans are
105 and 135 minutes, with 0.83 and 0.92 kWh. The simulator's final zero-power
quarter is outside that detected span. Internal zero-power pauses remain.
The dryer has one cycle and a manual 180-minute, 1.40-kWh fallback, demonstrating
the waiting state. The selected heat-pump source demonstrates an absent Energy
power binding. Provisioning receipts preserve user changes and removed examples
on warm starts. A one-time upgrade recognizes only the exact former synthetic
bootstrap defaults without tasks or manual edits. Other existing profiles stay
unchanged.

Tests cover weighted numerical profiles, hourly energy conservation, bounded
backlog progress, checkpoint rollback/retry, real Recorder conversion,
cancellation and missing/gapped/reset history,
unfinished cycles, duplicate uploads, restart persistence, manual-to-learned
transition, source changes, stale writes, retained accepted tasks and simulated
provisioning. Browser checks cover the shared editor, messages, save failures and
desktop/mobile layout. Simulation is not proof of recognition quality for every
physical appliance or automatic program classification.
