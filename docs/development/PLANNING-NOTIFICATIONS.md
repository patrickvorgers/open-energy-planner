# Module-owned notifications

## Ownership

The shared contract, collector and authenticated status adapter live in
`open_energy_planner/notifications/`. They know no contract, sensor, forecast or
solver cause codes. A module registers a synchronous current-state reader through
`app[NOTIFICATIONS].register(...)` during its existing API setup. Sources read
existing state and must not refresh inputs, perform external requests or change
product data. An existing module can add a source without changing the collector,
renderer or shared app startup.

Contract explanations and export-statistics advisories belong to
`contracts/notifications.py`, registered by the Contract API. Planning availability
and recovery presentation belong to `runtime/diagnostics/planning_notifications.py`.
Loaded-build mismatch belongs to `runtime/diagnostics/version_notifications.py`.
Solar forecast availability belongs to `solar/notifications.py`, registered by
the Solar API. Its current-state reader shares the planning admission check and
confirms recovery only after a matching, current forecast is available.
The app composition connects the planning and version diagnostics once.
The Contract API registers its statistics advisory separately from its planning
blockers, so accepted statistics stay visible when coordinator status is unknown.
Missing measurement configuration, unreadable contract/counter state or invalid
statistics evidence also retain the advisory as stale. Only a successful read
can establish that the advisory is absent. An unavailable source with no retained
notices shows unknown status without claiming that earlier notices exist.
One module can register independent sources with different recovery evidence.
Planner logging asks registered owners to explain propagated codes. The Contract API uses
its own explanation directly. Neither copies the other module's rules.

Browser behavior belongs in `web/public/notifications.js`, with the shared styles
under `web/public/` and workspace document under `web/pages/`. The frontend asset
owner serves these through its existing directory-based registration and recursive
package data. Notification files do not restore the old public-file list.

An initialized embedded page announces its notification reader separately from
status results. Navigation keeps the existing center stable during that read,
without turning a loading placeholder into an unavailable-status warning. Only
the current iframe document can announce initialization or publish status.
Completed empty reads hide the center. Actual read failures show unavailable
status, and a replacement document that cannot initialize still clears previous
private notices.

App projection, HTTP and blocker-lifecycle tests belong in
`tests/app/notifications/` and run in `unit-app`. Browser interaction tests belong
in `tests/frontend/browser/` and use that owner's fixtures and helpers. Public
routing and packaging contracts remain in `tests/frontend/unit/`. Documentation
checks stay in `tests/documentation/`. Existing coordinator regression changes
stay with `tests/app/planning/`. Forecast HTTP helpers come from
`tests/app/forecast/support/`, and history setup comes from `tests/app/history/`.

The initial composition change and CI ownership declaration require `full` under
the immutable PR base policy. After that reviewed boundary is merged, changes in
`notifications/` select `unit-app`, `unit-frontend` and `browser`, plus normal `ci`
checks. The import-boundary test prevents this owner from absorbing domain,
database, solver or HA dependencies. Domain providers retain their existing
directory routes. Runtime diagnostics retain `unit-app` and `unit-supervisor`
and add `browser` for their notification payload consumers.
New domains and new composition changes remain conservative until reviewed.
Selection always compares the complete PR against the immutable base. There are
no filename-specific exclusions or changes to product safety checks.

## Shared contract and lifecycle

Every `Notification` has an opaque stable cause/resource `key`, a module cause
`code`, validated presentation text, severity, lifecycle, affected operation IDs
and an optional aware episode start. Serialization adds the source `owner`, fallback
role and freshness. `level` and `blocks` are independent. An informative waiting condition
can block planning while a warning about corrected statistics does not.

Sources may supply positive whole-minute `auto_clear_minutes` presentation metadata
up to 9007199254740991, matching JavaScript's `Number.MAX_SAFE_INTEGER` at the JSON
boundary. Larger integers, fractions, booleans and strings are rejected.
For temporary confirmations, both notification views show a clock badge with this
lifetime and a hover/focus explanation instead of repeating expiry prose. It is
not a countdown. The source still owns actual removal. The browser never clears
notices on a timer. Notices marked stale hide the badge until their source can be read.

Sources return a complete tuple of current notices. An empty tuple is confirmed
resolution for that source. `None` means unknown. Exceptions and invalid source
results also mean unknown and retain the source's last-known notices as stale.
The collector's cache is disposable runtime evidence, not a persistent inbox.
The browser retains matching owner evidence when a restarted server has no cache
and reports an unknown source. Other successfully read owners can update normally.
Only authenticated data enters this path. Losing access removes private content
and leaves an explicit sign-in request.

`active` means the problem remains. `recovering` means its input cause has been
addressed but successful operation is not yet confirmed. A source confirms the
appropriate recovery condition before omitting its notice. For planning this is
successful usable-plan publication. Saving a setting, starting calculation,
finishing a rejected calculation, elapsed time or a failed read cannot prove it.
The existing coordinator remains the sole authority for that publication state.
Normal recalculation with a previously published plan stays quiet. A process
without established planning evidence reports unknown instead of recovery.

The stable key survives changes in wording and recovery state. It does not depend
on read time. Source ownership is separate from cause identity, so a dependent
fallback and authoritative domain notice can use the same key. The collector
keeps the authoritative notice once, independently of registration order.
If a source has no readable evidence or cache yet, planning still projects its
known blocker using the owning module's explanation. This fallback never confirms
recovery or replaces retained authoritative evidence.
Serialized source roles also let the browser retain an unavailable owner's
specific advice over a same-key fallback after a server restart. Fresh
authoritative evidence and newer fallback evidence still update normally.
Equal text on independent resources does not cause deduplication. An unknown cause is
not guessed to be a symptom of a superficially similar problem.

Recovery actions contain optional local page destinations, not executable writes.
Every explanation states the effect and appropriate recovery advice. Automatic
retry needs no invented action button. Without a destination, the popup includes
the full explanation. A popup can close and an explanation can collapse, but no
unresolved cause can be dismissed or marked healthy by the user or renderer.

This first conversion covers existing planning, contract/history and version
notices only. Storage-capacity, appliance-deadline, EV and thermal notices can
join through their own source registration when explicitly implemented. Normal
learning, disconnected inactive devices and routine save confirmations do not
automatically become notification-center problems.

## Behavior

### Household history in Home Assistant

The HA coordinator distinguishes negative reconstructed energy balances from
unreadable saved demand settings and failed forecast updates. Negative balances
never imply archive corruption or require restoring a backup. The warning names
the configured balance sources, explains that affected hours are omitted from
training, and asks the user to check overlapping Energy sources and exclusions.
Other usable hours remain available. Insufficient usable training retains the
annual-consumption startup estimate.

The [training-only filtering policy](DEMAND-FORECASTING.md#training-only-negative-balance-policy)
defines the meter-count rounding allowance and substantial-loss threshold.
Ordinary rounding corrections and isolated rejected hours with sufficient
remaining history do not produce persistent HA warnings. The app's demand API
retains filtering counts for diagnostics. A successful subsequent sync with no
actionable issue dismisses the existing HA notification. Normal learning without
enough history is not itself a persistent fault. Recorder, archive and update
failures retain their existing independent checks.

HA persistent notifications deduplicate identical messages. Changed recovery
advice or a changed balance-source list updates the existing notification even
when the connection-issue reason stays the same. Confirmed recovery clears the
message cache so recurrence can notify again.

Every authenticated content page exposes active notices through a notification
center. Its bell and count appear in the left sidebar on desktop and in the
header on mobile. The center stays hidden after a successful empty status read.
The native popup lists active notices. Selecting one opens its recovery page and
focuses the large explanation above that page's normal content. Other pages show
the compact center without repeating the large explanation. Expected startup
waiting is informational. Unusable
required measurements, forecasts, contract or bonus data and failed calculations
have an actionable warning. A mismatched app/integration build has its own warning.
Structured codes and details remain available to authenticated API readers. They
are not rendered as technical disclosures. Actionable sensor IDs and safe source
failure explanations belong in the visible message in both views. A module must
not discard identities when simplifying its wording. Current measurement advice
distinguishes a missing/unavailable/invalid sensor from an expired integration
capture. Unchanged available HA values do not produce an age warning. The popup supports keyboard access, Escape, outside
clicks and an explicit Close action. Navigation retains the existing unsaved-draft
confirmation. Standalone page navigation retains its existing sign-in boundary.

The first authoritative planning reason is shown. Fixing it may expose another
missing input. A calculation restarting after a blocker remains visibly in
progress on the same recovery page until a plan is actually published. Both the
center entry and page explanation disappear after confirmed recovery. The bell
also disappears when no active notices or status failures remain. Explicit pause is not classified as
a fault. It retains its existing control state and insight-only behavior.

`GET /v1/notifications` uses the existing Bearer/ingress authentication. It is a
read-only, uncached projection of existing runtime state. Public setup metadata
retains the existing recent build-contact metadata. Planning blockers and the
retained mismatch flag require authentication. The existing shared session refresh reads
notifications every 15 seconds. Standalone tokens remain only in page memory and
are never stored in browser storage. Recovery links preserve trusted ingress and
embedded-page routing. Embedded content projects display data to the shell's
single center through its own iframe element. The shell accepts only its current
content document and receives no credentials. Unauthenticated replacement content
clears private notices. Notification text never becomes HTML.

An unchanged response does not rebuild the live region or repeatedly announce the
same warning. Notices are not dismissible while the problem remains. Network or
server errors retain the last known notice with an explicit stale-data warning.
Unauthorized responses remove private notice content and request sign-in again.
An initial failed read shows an unknown-status indicator instead of silently
claiming that no notices exist. Only a successful current response can establish
recovery. No notification archive, email, push channel or acknowledgement state
is introduced.

The coordinator owns the current blocked episode and its UTC start time. Repeated
identical invalidations do not log again. Reason changes retain the episode start.
Successful publication logs recovery and elapsed seconds. Reaching calculating
state is not recovery. Start times reset with the process because they are runtime
diagnostics, not durable financial or planning evidence. App/integration mismatch
and history validation failures are logged by their existing responsible owners.
An observed version mismatch remains visible after contact expires and clears only
when an identified matching integration contacts the app.
Expected blockers use INFO. Unexpected planning failures retain ERROR tracebacks.
History rejection details are limited to 256 characters and contain no submitted
report payload or credentials. All output uses the shared UTC logging formatter.

Notifications do not change planner admission, device control, plans, contract
rules, saved counters or Recorder data. Missing annual export remains unknown.
Manual earlier export is appropriate only after a validated reference establishes
which earlier period is missing. A warning never authorizes assuming a zero value.

Tests cover real HTTP authentication and planner recovery, deduplicated transition
logs, bonus-history rejection/recovery, and version matching. Browser tests cover
all content routes, direct/ingress access, desktop/mobile placement, popup keyboard
controls, empty and unknown status, multiple recovery destinations, stale reads,
authorization loss, inert provider text and embedded recovery navigation. Browser
semantic exposure is tested. Actual speech with individual screen readers is not.

## Acceptance evidence

Shared model/collection tests cover active and recovering states, confirmed empty
reads, independent owners, propagated-cause deduplication, malformed output,
unknown initial reads and failed-source retention. Failure diagnostics retain a
stack trace but redact provider exception payloads and log only transitions.
The import guard and CI policy tests protect the reviewed responsibility boundary.

Module tests cover contract cause recognition, nonblocking negative statistics,
normal recalculation, uninitialized restart and retained version mismatch until
a fresh identified match. Existing HTTP/planner tests retain actual storage,
authentication and successful-publication regressions. The arbitrary-source HTTP
test proves registration without app-factory changes and unauthorized reads
cannot invoke the source.

Browser tests cover arbitrary source codes, owner-aware uncertainty after a server
restart, malformed source/lifecycle/scope metadata, severity independent of impact,
automatic recovery without a fake link, confirmed disappearance in both views,
older network responses, focus, drafts and actual direct/ingress navigation on
desktop/mobile. Recovery navigation tests measure the heading and message against
the sticky mobile header after same-page anchor jumps and cross-page navigation.
New renderer scenarios use synthetic fixtures and are labelled as
such in PR screenshots. Existing failed-save, remaining-blocker and publication
rejection tests stay with their responsible application modules.
