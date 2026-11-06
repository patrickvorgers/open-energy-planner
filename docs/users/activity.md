# Background activities

Open **Insights > Activity** to see what has run and which background work
is expected next. Activity is the last item in the Insights group.

## Find an activity

The page opens on today in the Home Assistant timezone. Choose **Day** to inspect
another date or **Activity type** to filter the list. History covers seven rolling
days. **Load earlier activities** loads the next 100 executions for that day and
type. **Today** returns to the current day.

New activities appear automatically while the page is connected. Running work
updates in the same row. Open a row for its explanation, responsible module and
finish time. Keyboard focus and open details remain in place during live updates.
Names include useful context, such as **Fetch price forecast: Jeroen.nl**, the
appliance being learned or the solar sources being updated. Calculations and
daily archives show their captured period in the Home Assistant timezone.
**Evaluate measured savings** shows the measurement period being investigated.
It does not mean every measurement in that period has already been settled.
History names stay on one line. Long names are shortened visually to fit the screen.
Open the activity to read the full name. Activities without a known source retain
their general name. Historical records are not rewritten to add missing context.
**Refresh** retries a failed history read without restarting background work.
Refresh and earlier-page actions become available again when the current read
finishes. Live updates retain the loaded amount of history. Older records remain
available through **Load earlier activities** when new work pushes them out of view.

## Open related data

Open an activity to find its related-data link when a relevant screen exists.
**View this plan** opens the exact saved plan, including an older version outside
the first page of the plan selector. Viewing it does not activate it or change
device commands. An unavailable plan remains visibly unavailable instead of
silently opening the newest advice.

Other links name their destination explicitly: **View savings**, **View forecast
quality**, **View appliance profiles**, **View planning and prices** or
**View contract**. These open the relevant page. They do not imply that a
particular source, appliance or date has been selected. A standalone page may
ask for the administration token again. Home Assistant ingress keeps its existing
automatic authentication. Activities without a useful destination have no link.

## Understand the result

| Status | Meaning |
|---|---|
| Running | Work has started and has no definitive outcome yet. |
| Completed | The module accepted a usable result. |
| Failed | The attempted work did not deliver an accepted usable result. Read the explanation for its effect on planning. |
| Interrupted | OEP stopped or restarted before an outcome was recorded. This does not prove the calculation or source was wrong. |
| Cancelled | Work was deliberately stopped. |
| Skipped | The result was not applied, for example because evidence is still pending or a history batch was already processed. |
| Recovered | The first accepted usable result after a failure of that module and activity type. |

Price activity details identify the adapter, market and usable coverage. A failed
fetch can leave earlier data usable until its existing expiry. Empty or rejected
data never counts as recovery. A comparison is Failed when its evidence-wait
limit expires and OEP abandons that window. Savings corrections that need another
batch remain Skipped while the previous successful version stays visible. Ad-hoc recalculations appear after they run,
without a predicted time in the upcoming list.

### Understand repeated savings checks

**Evaluate measured savings** processes a chronological batch of captured
measurements. Its details state how many intervals this attempt processed and
the interval where processing stopped, shown in the Home Assistant timezone.
They identify the admitted waiting cause, such as missing published prices,
unfinished export statistics or unavailable Recorder history, and the relevant
action. If the exact cause is unknown, the explanation says only what is known.

Some intervals can be stored before a later interval waits. Earlier published
financial results remain visible while the correction is incomplete. A skipped
check can also mean that a correction needs another batch to catch up. That
explanation differs from waiting for external evidence. The next background
check rechecks available evidence or continues the batch. Refreshing Activity
only reloads history. Each attempt retains its own explanation, so repeated
rows can be compared without treating every skipped check as a new error.

### Several failed plan valuations at once

**Recalculate plan value** updates the financial estimate of one archived plan
after contract or counter changes. Its name includes the plan's creation time
and strategy, matching the selector under **Planning**, followed by the original
plan period. The creation time identifies the advice being valued, rather than
an individual measurement hour. A batch examines up to twenty archived plans,
so several rows at the same time can represent different plans. Each execution
keeps its own result. This does not mean the current device commands failed.

If this calculation fails, its last successful estimate remains available. Open
the corresponding plan under **Planning**, check its cost information and review
**Contract** settings. If the cause remains unclear, report the activity and its
period for investigation. A failed activity alone does not identify the cause.
The queue advances past handled failures. A new contract or counter version
starts another pass, but refreshing Activity does not retry the calculation.

## Check what happens next

**Next six hours** shows known scheduled activities within the next six hours.
Configuration or missing inputs can change those times. **Module status**
shows the module's current assessment. An earlier completed activity does not
prove that its data is still usable.

Modules with related parts appear as a hierarchy. **Prices** groups adapters
without repeating the installation's configured market area. **Weather** groups
solar and temperature weather. Top-level groups start expanded and can be
collapsed. Live updates preserve that choice while the page stays open.
Each part retains its own status and explanation. An **Overview** row
shows a producer's own status when it also has child modules. It is not a combined
assessment of those children. One unavailable adapter does not mean that other
price sources are unavailable. Empty groups disappear automatically.

Activity outcomes and actionable notifications are separate. A failed activity
does not automatically mean equipment needs attention. Follow a notification's
own explanation and action. Activity is not proof that physical equipment ran.

## Connection or history warnings

After a temporary connection failure, live updates resume automatically and OEP
reloads the current state when required. Temporary warnings disappear after
recovery. A failed history read stays visible until it succeeds.

If some updates could not be saved, the page warns that history may be incomplete.
That warning remains because missing activity records cannot be reconstructed.
Energy planning continues independently. After a restart, unfinished retained
activities appear as Interrupted rather than incorrectly reporting success.

For connection problems, follow [Troubleshooting](troubleshooting.md). Database
maintenance keeps its separate [storage recovery screen](data-storage.md).
