# Annual export counters from Home Assistant statistics

The normal annual counter uses long-term Recorder `statistics.sum` for every
configured required grid export energy meter. Import and battery meters are
excluded. HA converts Wh and MWh values to kWh. Native unit, statistic ID,
Recorder metadata ID and source remain part of the saved identity.

## Reference and missing history

HA hourly rows contain the cumulative sum at the end of their hour. The row
starting one hour before the contractual annual boundary supplies that boundary's
reference. A complete series produces the automatic result:

`used_kwh = sum(max(0, latest_sum - reference_sum))`

When the boundary is absent, OEP selects the earliest completed endpoint shared
by all meters. Intermediate hourly rows are not needed for sum accounting.
The requested latest completed endpoint must still exist for every meter.
The UI displays the reference
date. The user enters only exported kWh from the annual boundary up to that
reference endpoint. Unknown is never treated as zero. The result becomes:

`used_kwh = confirmed_missing_prefix_kwh + sum(max(0, latest_sum - reference_sum))`

A zero prefix requires explicit confirmation unless the actual annual boundary
exists. The user never enters an absolute sum or physical meter reading.
An absent endpoint cannot be replaced by entering another prefix.

## Refresh, restart and continuity

The app requests a refreshed annual range once the next hourly endpoint is due.
Recorder receives one hour of compilation grace. The integration retries an
unchanged failed request after fifteen minutes. The budget reports its measured
through endpoint. It includes no unmeasured live tail, interpolated energy or
planned exports. Sums older than three hours are unavailable for current planning.

Each component uses only its fixed reference and the requested latest endpoint.
For example, reference 100 and latest 150 give 50 kWh. A corrected latest sum of
149 gives 49 kWh. Latest 99 contributes zero, and later latest 101 contributes
1 kWh against the same reference. OEP does not sum positive hourly increments or
move the reference after a negative difference. Confirmed missing-prefix volume
remains added. Intermediate gaps and corrections do not block this arithmetic.
Raw signed sums remain unchanged. Physical meter resets normally preserve the
statistics sum, but HA can also publish decreasing sums after corrections or
sensor dips. A missing physical `state` does not
prevent sum accounting. It does prevent using that sample as a physical endpoint
for historical settlement comparisons.
Current confirmations take precedence over older settlement archives for the
same source and period. An invalidated current confirmation blocks settlement
instead of restoring an older opening volume that could undo a correction.

The app revalidates reference time, value, metadata, units and series membership
on every refresh. Missing reference/latest endpoints, changed reference evidence
or a timestamp rewind block the counter. A new latest sum may decrease without
blocking it. The history digest binds the accepted endpoint pair, rather than
all intervening hours. Previous books remain immutable in the provenance archive.
Rejected reports retain their digest and predecessor identity. Restoring valid
reference evidence allows automatic revalidation. OEP never silently rebases a
confirmed prefix after a reference correction.

A usable budget with any negative component difference produces a warning in
the existing notification centre. It explains the zero floor, preserved prefix
and fixed reference. Further export can make the difference positive again.
The warning distinguishes a possible intentional correction from incorrect
meter data without claiming either cause is proven. It disappears when no
current component difference is negative. It does not block planning. Missing,
invalid or stale evidence retains its separate blocking behaviour.

An empty Recorder channel or unsupported statistics metadata also persists that
blocked state. It cannot leave an old counter or a missing-prefix confirmation
usable. Temporary Recorder exceptions and an unavailable Recorder component keep
the last checkpoint only within its existing freshness limit and retry normally.

Every annual period receives a separate reference. Previous books remain in the
archive. Restart reads the stored reference, prefix, validation state and
checkpoint from the active database. It does not depend on a JSON file or the
database used before a backend transfer. No conversion of development seed
schemas is provided. Former physical seeds cannot authorize annual planning or
suppress statistics acquisition. They remain available to historical settlement
and immutable archives. Annual initialization derives a new statistics reference
and asks for a missing prefix when necessary.

An older reader may reject new negative differences or endpoint digests that
replace its full-history digest. A code-only downgrade can therefore block
even a positive checkpoint. It fails closed instead of changing
the annual volume. Keep a matching pre-update backup for an intentional
downgrade, and prefer a compatible forward fix. Restoring earlier saved state
can discard changes made after that backup. Never recover by deleting retained
evidence, changing raw statistics or inventing a zero prefix.

## Confirmation and concurrency

The budget page remains editable when history cannot establish a reference.
It explains why opening confirmation is unavailable instead of suggesting that
an arbitrary opening can repair a missing endpoint. Refresh budget reads
the latest result and reports loading, completion or failure. It does not repair
Recorder statistics or force an integration retry. Unsaved contract edits and
opening input for an unchanged reference survive refresh, including failed reads.
Budget confirmation and refresh are independent of price-rule editing. Completing
a contract save leaves budget actions usable, and superseded reads cannot replace
the feedback for a newer read.
Rejected confirmations reread budgets so a concurrently saved contract cannot
leave an old cap on screen. Opening drafts survive an unchanged reference.
Confirmation and read failures remain visible without replacing newer read feedback.
Empty or negative opening input is rejected locally without a network request,
and focus stays in the same input so the user can correct it.
Planning continues to require valid bonus evidence.

A confirmed opening requests planning immediately through the existing input
refresh. The page also refreshes notifications immediately after acknowledgement
and brings the fresh explanation into view. Background polling never moves focus.
While the planner is calculating, the notification says that a new energy plan
is being calculated and that this can take a few minutes. It clears only after
successful publication or changes to the actual remaining blocker. Notifications
show the user explanation without raw condition codes or a technical disclosure.

The authenticated `POST /v1/contracts/budgets` accepts `rule_id`, `used_kwh`,
`contract_version`, `config_revision` and `reference_version`. The server selects
the current period and authoritative saved statistics. Only an active rule with
an unknown prefix may be confirmed. The request never supplies meter provenance.
The separate manual counter endpoint rejects statistics references supplied by
clients. Only validated Home Assistant history can establish those references.

The application lock serializes confirmation with configuration, counter and
history writes. Configuration/contract/reference changes and a competing
confirmation reject the write. Routine physical updates and a newer validated
checkpoint with the same reference do not cause a conflict. Prefix, checkpoint,
immutable provenance and current pointer commit together before memory changes.

## Bounds and ownership

The authenticated bonus-history route and its TLS gateway permit up to 16 MiB
for at most sixteen components with 9000 hourly rows each. This accommodates
annual reports with full-precision JSON numbers. Other history consumers retain their
existing transport and physical endpoint semantics. The HA integration only
reads metadata and normalized rows through Recorder's public statistics API.
Annual arithmetic, continuity decisions, persistence and concurrency belong to
the app. Physical equipment and production Home Assistant are not touched.


## Historical settlement checkpoints

A valid current statistics checkpoint can be newer than the first interval being
re-evaluated. Settlement first uses exact retained physical measurements when
available. If the stored sequence ends before the checkpoint, it requests the
completed hourly endpoint at or immediately after the historical interval.
At the exact period start, the preceding reference row proves a zero sum
difference and the physical boundary reading. The statistics request and cache
therefore keep that exact endpoint instead of advancing to the following hour.
The separate hourly-delta path still requires a following completed hour.
The existing statistics transport supplies each meter's identity, sum and
physical endpoint. The app reuses endpoint derivation and verifies the same
reference date, metadata identities, units and reference sums as the current
validated counter. The confirmed missing-period prefix and its confirmation
remain authoritative. Missing physical endpoints or changed references fail
closed. Hourly gaps do not invent quarter measurements or imply zero export.

The historical result belongs to `savings-bonus-counters`, with immutable
statistics provenance. Current planning counters and original frames remain
unchanged. Historical cache reuse is bound to the current counter-book version
in process memory. Restart or a new live book requests fresh historical evidence
before reuse, including when a historical endpoint may have been corrected.
The existing historical ingress binds reports to the contract, configuration,
meters and counter version under the settlement mutation lock. Its TLS gateway
route uses the same bounded 16 MiB annual-statistics allowance. Other peer routes
retain their existing body limits.
