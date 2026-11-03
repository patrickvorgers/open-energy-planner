# Courtyard theme

Courtyard changes the app's presentation and shared navigation. Planning, Savings,
Contract and Installation retain their existing functional sections. Appliance
settings are consolidated as described below. Stored data and planning calculations
remain unchanged. Editable configuration pages share one floating Save and
Discard bar. Chart legends move
to the right on desktop and below on mobile. Balance tooltips group the original
signed values under Production and Consumption, with SoC under Battery state.
Production means supply and includes grid import and battery discharge. No planning, evaluation or device-control behaviour is added.

The left sidebar keeps destinations visible in three labelled groups.
Energy contains Planning, Appliances, EV charging, Thermal storage and Savings. Insights contains
Forecast quality. Settings contains Installation, Appliances, Contract and Data storage, in that order. Page headings use Appliances settings, Contract settings and Installation settings.
Page headings use the matching task icon and navigation label. The shared
overview changes its heading between Planning and Savings. Links use
existing routes, including the trusted Home Assistant ingress prefix. Desktop groups
remain expanded. The shared workspace script owns only the outer page selection.

## Page navigation and authentication

The application entry at `/` and the explicit `/workspace` route keep the
sidebar mounted. Only a same-origin content iframe navigates. Each existing page
keeps its own browser document, module initialization, timers and unsaved-change
warnings. The browser suspends or disposes the old document and its scripts. No client
router, module reinjection or shared administration-token storage is needed.
This also works inside Home Assistant's own ingress iframe.

The workspace address records the selected page in the `page` query parameter.
Reload and ordinary browser back/forward restore it. Planning/Savings hash changes
reuse the same content document and its selected plans and charts. The outer menu
reflects the page that actually loaded. Cancelled beforeunload prompts retain both
the current selection and the draft. The shared SaveBar protects every editor's
dirty draft or pending persistence operation with the native unload guard.
Keyboard navigation focuses the new page
heading. Modified clicks and original direct page URLs retain ordinary navigation.

Content documents use `embedded=1` to hide their duplicate navigation before paint.
The parameter changes presentation only. It neither authenticates a request nor
carries a credential. Same-origin CSP restrictions remain in force. Standalone
HTML documents load without a token. Their protected API requests require the
administration token after each document change. The token is not shared with
the workspace or another content document.

For trusted Home Assistant ingress, the server hides the token connection form
in the initial HTML. A delayed or failed session request therefore cannot flash
an API-key prompt. Standalone pages still show their connection form immediately.
An untrusted forwarded ingress header cannot hide that form or grant API access.

## Visual identity

The shell uses navy `#1a3060`, sky `#cde9fd`, paper `#f5fbff`, ink `#19263f`,
muted text `#54657a` and action blue `#006da6`. Selection uses the paper surface.
Hover outlines do not change menu geometry. Keyboard focus has a distinct outline.

The original OEP icon family uses blue `#4387bf`, green `#21836d` and yellow
`#f9d544`. Navigation symbols are decorative beside visible labels. All thirteen
SVG assets are packaged under `src/open_energy_planner/web/public/icons`, including
currently unused Balance, Solar, Household and Battery symbols. The app mark is
also the favicon. Its 512-pixel transparent PNG rendering is the Home Assistant
integration's `brand/icon.png`.

Outfit and Space Grotesk are bundled locally under `web/public/fonts`. Their original
SIL Open Font License files, source URLs and SHA-256 hashes accompany them.
No CDN requests or runtime dependency installation is required.

The appliance cycle profile uses a smooth monotone line through the original
quarter-hour mean power values. It does not alter cycle energy or imply extra
measurements. Monotone interpolation avoids invented overshoot between samples.
A neighbouring bar chart shows kWh per quarter from that same learned profile.
The bars sum to the cycle energy and require no extra meter or backend field.
These are learned cycle expectations, not measurements of a new live run.

Value axes follow each chart's own visible data with ten percent of total axis
height at each unrestricted end. Bounds stay independent of tick placement. Tick intervals use multiples of
1, 2, 2.5 or 5 without expanding the bounds. Signed stack totals determine bar
extrema. Partial interval bars contribute whenever they overlap the window.
Lines contribute interpolated boundary values, while missing samples break lines.
Without battery, Battery self-consumption and OEP balance charts share their time
window and a common kW scale based on their joint visible signed stacks. Keep
identical bounds and ticks so the three scenarios are directly comparable.
Converter-specific charts retain independent scales. Changing the time
window, plan or series visibility recalculates the domain. Consumption, PV, appliance power/energy and forecast-quality charts fix their
lower bound at zero and add clearance only above. Signed balances, net power,
prices and savings retain negative values. Time axes retain
their selected window and SoC stays fixed at 0 to 100 percent. Empty data use a
zero-to-one fallback before padding. Constant data use a finite span proportional
to their magnitude, or one for all-zero data.

Visible dates and times use the shared formatter with minute precision. Seconds
and milliseconds remain intact in storage, API payloads and date conversion.

## Verification boundary

Browser tests cover all nine destinations on desktop and mobile, standalone
and ingress URLs, active states, native keyboard operation and
stable hover geometry. Asset tests cover the allowlist, font identities, licenses,
packaging patterns and integration PNG format. The existing chart and functional
tests continue to exercise the original page implementations.

## Simulated laundry fixtures

The persistent laboratory also exposes Test washing machine and Test dryer beside
Test dishwasher. Their example profiles span 150 and 180 minutes and use 0.92 kWh and
1.4 kWh. The original dishwasher spans 120 minutes. Repeated completed-cycle
observations use the normal learner. It averages power by phase and retains the
longest observed duration in quarter-hour blocks, rather than averaging durations. Seeded app profiles are explicitly synthetic evidence. No historical
whole-home measurements or archived plans are rewritten to create these examples.
Only subsequent simulated runs advance their energy counters and the conserved
live grid balance. Start, cancel and accelerated progress actions remain lab-only.

Configured EV charge-point meters remain selected for forecasting but are omitted
from the Appliances cards and cycle-profile picker. Their configuration and
commands remain on EV charging.

## Central appliance settings

Settings > Appliances owns the existing Energy-device selection and weather-sensitivity
settings as well as cycle-shifting permission. Use in OEP selects or removes an
Energy device without deleting its HA meter, history or archived OEP profiles.
Unresolved cycles disable removal. Hidden EV charge-point selections are retained
when saving. Forecast quality is read-only daily analysis with a link to these settings.
Home Assistant Energy remains the authority for meter registration and upstream
relationships. Existing configuration APIs and version checks are retained.

Energy > Appliances contains only readiness, deadlines, schedules, execution
evidence and the learned cycle charts. Setup and learning instructions live with
the configuration. Both pages link to one another and retain ingress prefixes.

## Consistent configuration actions

Installation and strategy, contracts, EV settings and centralized appliance settings share the same floating save bar. It inherits the
Courtyard surface, border, text, action colours and typography. One live status
shows unsaved changes, writes in progress, confirmed persistence and failures.
The bar stays beside the desktop sidebar and inside the mobile viewport. Its
measured height reserves room below the final fields.

Discard reloads confirmed settings. Read-only refresh retains pending edits.
Editing is locked during saves and explicit reloads. Independently accepted
writes remain accepted after partial failure, while remaining edits can be
retried. Energy Dashboard synchronization remains automatic. Read-only evidence
filters and immediate task or counter commands do not require a configuration
save.

Balance tooltips attach the exact start-of-interval SoC when hovering flow bars.
Missing and hidden state series remain absent. No state is interpolated.

Removing a selected appliance first revokes its cycle-shifting permission. A
failed source-selection write retains the remaining draft for retry. Simulator
heat-pump history subtracts complete overlapping laundry readings. Older hours
before fixture creation remain available. Missing active readings and counter
resets remain unknown instead of being attributed to the heat pump.

The shared consumer read response includes `charging_meters` from persisted EV
configuration. This metadata is independent of EV live status and the current HA
timezone catalogue. Both appliance pages use it to exclude charge points, while
the settings page retains their source selections. It is read-only response
metadata and adds no stored configuration.

The app mark uses rounded roof tips and wall ends hidden beneath the roof.
The EV symbol connects its green cable to the blue car below the windscreen.
The outward cable loop stays separate from the car outline at small sizes.
The web mark and transparent 512-pixel Home Assistant PNG share the same source.
