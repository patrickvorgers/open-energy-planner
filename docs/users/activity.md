# Background activities

Open **Insights > Activity** to see what OEP has done and which background work
is expected next. Activity is the last item in the Insights group.

## Find an activity

The page opens on today in the Home Assistant timezone. Choose **Day** to inspect
another date or **Activity type** to filter the list. History covers seven rolling
days. **Load earlier activities** loads the next 100 executions for that day and
type. **Today** returns to the current day.

New activities appear automatically while the page is connected. Running work
updates in the same row. Open a row for its explanation, responsible module and
finish time. Keyboard focus and open details remain in place during live updates.
**Refresh** retries a failed history read without restarting background work.
Refresh and earlier-page actions become available again when the current read
finishes. Live updates retain the loaded amount of history. Older records remain
available through **Load earlier activities** when new work pushes them out of view.

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

## Check what happens next

**Next six hours** shows known scheduled activities within the next six hours.
Configuration or missing inputs can change those times. **Module status**
shows the module's current assessment. An earlier completed activity does not
prove that its data is still usable.

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
