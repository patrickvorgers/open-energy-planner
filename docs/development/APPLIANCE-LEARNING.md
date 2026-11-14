# Automatic appliance cycle learning

Home Assistant Energy owns device membership and sensor bindings. OEP reuses
`device_consumption[].stat_consumption` as the cumulative energy meter and its
optional `stat_rate` as the power sensor. There is no second OEP sensor picker,
manual start/end entry, detection threshold or learning wizard. Settings remain
under Settings > Appliances. Readiness and finish-before requests stay under
Energy > Appliances. Historical use never grants readiness or start permission.

## Learning and manual fallback

The HA adapter independently refreshes recent hourly and five-minute Recorder
statistics through public APIs. It normalizes energy to kWh and power to W.
Each row contains interval start, mean power and cumulative energy at its end.
Missing, negative-power and nonfinite readings remain unknown. Gaps, resets and
invalid numeric measurements break a candidate without blocking later valid runs.
Malformed types, chronology or source identities reject the upload.

Learning has its own cancellable background timer, independent of successful
forecast or command synchronization. Membership comes from the small authenticated
consumer settings projection. EV, thermal and weather-sensitive consumers are
excluded. A source refreshes recent evidence every five minutes. Each pass also
handles at most one seven-day historical batch per device and resolution. Pending
backlogs and failed imports retry after one minute. This keeps other devices and
recent measurements eligible while older data loads. Unload cancels the worker.
Fine failures do not block hourly processing. Connection and release failures
retain the existing recovery and unload safeguards.

The authenticated peer gateway explicitly permits profile/membership reads and
validated history uploads. Complete appliance projections use the existing bounded 4-MiB snapshot budget in both the HTTP reader and native receiver, allowing long five-minute profiles across many devices. Oversized declared or streamed responses are still rejected. Ordinary small API routes retain their 256-KiB limit. It continues to reject configuration and permission
writes. The app owns cycle segmentation and planning. HA only transports evidence.

The application recognizes activity above the larger of 10 W and the observed
low-decile power plus 5 W. Thirty quiet minutes bracket a complete cycle. At
least fifteen active minutes and a duration between fifteen minutes and twelve
hours are required. Shorter quiet pauses remain inside the cycle. Missing rows,
null readings, counter resets and unfinished cycles break the candidate. Recorder sums may
have negative arbitrary origins. Only their increments determine cycle energy.
A candidate with unusable energy resolution is skipped without blocking later
valid cycles. Power
finds boundaries. Cumulative energy supplies the actual kWh and native five-minute
profile. The trailing quiet confirmation is not part of the profile.

The first complete cycle admits a usable provisional profile. Additional cycles
refine it with the existing 90-day recency half-life. Aligned source phases are
averaged by their energy and shorter runs contribute zero after their end. Five-minute source profiles retain their measured phase duration and final partial phase and longest measured duration,
without rounding a 35-minute run to 45 minutes. Different appliance programs are not automatically identified.
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

The first usable hourly cycle supplies a provisional planning profile immediately,
even while older history is still loading. A saved manual estimate or explicitly
recorded measured profile takes precedence over coarse evidence. The first fine
cycle refines the timing. Overlapping coarse/fine observations count once in the
selected model. Fine timing supplies the shape and distinct coarse/fine runs
supply recency-weighted energy. If coarse energy would force the finer shape beyond the physical power bound, the fine model remains selected and coarse evidence stays archived. Raw archives and separate resolution summaries
remain available. Hourly estimates cannot establish subhour peaks or program identity.

Fine backfill scans backwards in bounded weekly batches from the recent horizon
to the paired history's discovered start. It continues through empty weeks instead
of treating the configured Recorder retention or one empty week as proof that
older fine data is absent. The source-bound `fine_import` checkpoint and
`fine_history_request` survive restart. Fine evidence older than seven days remains
admissible. Recent fine refreshes continue while the backlog is pending.

Both cycle charts always display five-minute intervals. Hourly estimates are spread uniformly across their twelve display intervals. Average power stays flat within an hour and each energy bar contains one twelfth of that hour's kWh. Legacy quarter-hour profiles are similarly spread uniformly for display. These are explicitly labelled estimates. Actual five-minute observations retain their phase detail in the model and charts. Regridding preserves total energy and never relabels estimated detail as measured. Planning start choices remain UTC quarters, while task windows preserve finer measured power peaks.

Energy > Appliances shows source-bound progress, usable coverage, detected cycles,
provisional profiles and measured activity. Settings retains the short planning
summary and manual fallback. Both pages refresh read-only evidence every ten
seconds without discarding drafts or replacing unchanged charts. A newly learned
model can reject a stale configuration save and preserves its draft for review.

## Manually started cycles

A recent measured start with at least fifteen active minutes can identify an
ongoing run after its quiet predecessor. Its learned profile is frozen separately
from task feedback. The planner fixes it to the measured start and includes only
remaining phases. The existing component composition removes at most the matching
expected energy inside the remaining execution window. Other expected use stays
in the forecast. No second household or appliance contribution is added.

Missing or stale measurements retain ordinary forecasts and block duplicate
starts. Exhausting the expected profile does not confirm physical completion.
A complete quiet-bracketed run closes measured evidence. Fresh quiet measurements
can establish idle after uncertain data without inventing a completed cycle or
execution feedback. A pending task intercepted by a manual start becomes
`execution_observed` and requires explicit cancellation before new readiness. This interception remains latched even if an old attempt expires or later authenticated feedback reports nonexecution, start or completion. Actual attempt outcomes and event times remain recorded independently. A normally confirmed running task without earlier interception retains its ordinary lifecycle.
Already issued attempts retain their frozen profile and authenticated feedback.
Measured activity never issues a start, grants readiness, reports task completion
or makes an unknown attempt safe to retry. Physical actions remain in user-owned
HA automations.

## Evidence and persistence

The app validates the uploaded power identity against the current Energy binding.
Detected cycles receive an identity based on source pair and UTC boundaries.
Original five-minute and hourly energy points, detected times, resolution quality
and synthetic provenance are archived in the shared planning document store. Duplicate uploads do not add
samples. Resolution-qualified identities deduplicate exact repeats. Overlapping fine views are reconciled and fine evidence supersedes overlapping hourly ranges without deleting either archive.
The last twelve cycle summaries are shown. Full archives remain retained.
Writes use the existing SQL transaction and application lock. Fine coverage and
both backfill checkpoints use one mutable `appliance-progress.json` document in the planning namespace, joined
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
The legacy completed-cycle API remains for existing clients. Its energy-only records retain their original source identity and points. Distinct explicit completions contribute once using the existing arithmetic-mean contract alongside the recency-weighted automatic aggregate. Empty history uploads, restart and later learning reconstruct the same contributions. Power-source relinking excludes old automatic pair evidence without inventing power provenance for energy-only records. Changing the energy source excludes its explicit records. The supplied
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
The dryer has one cycle and a saved manual 180-minute, 1.40-kWh fallback. Its first detected cycle now supplies a usable provisional learned profile. The selected heat-pump source demonstrates an absent Energy
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
