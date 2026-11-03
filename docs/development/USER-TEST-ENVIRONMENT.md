# Interactive developer scenarios

## Balance-chart regression checks

In the persistent real-Supervisor Docker developer environment, open Planning
with the existing saved contract and retain all history. Around a daylight-bonus
edge such as 17:09 or 17:07, each balance chart must still show one 17:00–17:15
bar. The exact edge stays visible in original prices and interval inspection.
Check Without battery, Battery self-consumption and OEP together. Their kW bounds
and ticks must match for Next 6 hours, Next 24 hours and the full horizon, after
legend changes and on desktop/mobile. SoC retains its fixed separate axis.
Inspect a partial opening quarter to see its actual coverage in the tooltip.
Do not change saved contracts, counters, device settings or historical plans to
perform these read-only checks.

## Electric boiler

The simulated fixture includes a simulated 200-litre electric boiler with
a 2 kW element, 20 °C surroundings and a 1 W/°C standing-loss coefficient. Its
independent simulated thermostat limits heating at 65 °C. Configure the boiler
from **Settings > Appliances > Electric boilers**, using `sensor.oep_test_boiler_energy`,
`sensor.oep_test_boiler_temperature` and `sensor.oep_test_boiler_power`.
The declared hourly thermal draw is 1.4 kWh at 07:00 and 1.8 kWh at 19:00.
The fixture itself draws in the first two quarters of those hours, providing a
deliberately independent quarter-resolution realization of the hourly model.

For this scenario, configure the thermal blueprint to connect the virtual requested-heating entity
to `oep_test_fixture.boiler_heater`, with `enabled: true` for heat and
`enabled: false` for stop. These actions address only simulation. The grid's
cumulative meters include actual simulated boiler electricity once. The fixture
preserves its cumulative meter and water temperature while clearing heater
permission on restart. It adds only its own Energy consumer and simulated idle
history. Existing consumers, contracts, settings and history are retained.
See [thermal storage](THERMAL-STORAGE.md) for model limitations and authority.

This is a separate, persistent local laboratory, not a connection to your home installation. It runs the actual app, solver and HA integration against synthetic meters/history, live Open-Meteo weather and the normal market-price adapters for the configured contract area. Docker Desktop with Linux containers and Python 3 are required.

## Start, stop and credentials

Use the [real Supervisor developer environment](DEVELOPER-ENVIRONMENT.md):

```console
python -m script.supervisor.dev start
python -m script.supervisor.dev status
python -m script.supervisor.dev stop
```

Open `http://localhost:7123/app/local_open_energy_planner`. Home Assistant uses
the generated account in `.local/dev/developer.json`. No separate OEP API key,
pairing code or approval is required. Use Settings > Installation inside the app
for the wizard. HA test controls are at `http://localhost:7123/oep-test/installation`
and the Energy dashboard is at `http://localhost:7123/energy`.

The first start supplies simulated devices/history and fills only missing sample
settings. Later starts retain saved settings. Partial installations remain for
the wizard to complete rather than mixing sample defaults with existing choices.
The workspace keeps its sidebar while its content document changes.

Compose project `oep-dev` retains its private Docker, containerd and Supervisor
volumes when stopped. Supervisor owns the app data and HA Recorder/configuration.
The host Docker socket is never mounted. Only loopback UI/observer ports are
published. Existing retired `oep-user-test` volumes are preserved separately and
are not renamed, reset or silently adopted. No personal VM is provisioned or
updated. Generic product installation remains an independent user action.

## Included installation

- Two independently metered AC solar sources: a 3 kWp south-facing roof on a hybrid converter and a 1.5 kWp west-facing garage on a separate converter.
- One 10 kWh battery, 3 kW charge/discharge limits, 10% minimum SoC and an initial measured SoC of 50%.
- An 11 kW import/export planning limit and a complete Energy dashboard balance.
- 32 days of synthetic hourly cumulative energy statistics. The wizard derives its startup consumption estimate from all available common complete days within the last year. This short fixture is extrapolated to 365 days and is not a measured annual total.
- Live market inputs from the ordinary configured adapters: published Nord Pool prices and available forecast sources. External availability can shorten the horizon or prevent new plans. No synthetic price fallback is used. Existing archived plans retain their original test prices.
- The documented 2026 Zonneplan contract example: import `(market + 0.09161) * 1.21 + 0.02` and base export `market + 0.02`, in EUR/kWh. An extra `0.10 * (market + 0.02)` applies during daylight when `market + 0.02 > 0`, within a 7,500 kWh calendar-year export cap. The fixture contract starts 28 HA-local days before first provisioning so automatic budget reconstruction can use available history. All configured grid export counts toward the cap, including battery export. OEP does not reproduce supplier-specific source exclusions. This is an illustrative lab contract, not a quotation or supplier preset. Existing custom contracts remain unchanged.
- A weather-sensitive heat-pump consumer, three configured shiftable appliances with explicitly synthetic quarter-resolution profiles, two cars, two shared charge points and a 200-litre electric boiler. See [one-time example ownership](DEVELOPER-ENVIRONMENT.md#ready-to-use-appliance-examples).
- Open Energy Planner in the HA sidebar and six shipped-blueprint instances bound exclusively to simulated appliances. No task is submitted automatically. Later edits, removals and sidebar preferences survive provisioning.
- Live Open-Meteo irradiance, temperature and wind forecasts at the configured HA location, without an API key.

Live readings update every 15 seconds. **The battery is stationary:** changing its SoC slider changes the measurement. Proposed planner commands do not physically charge/discharge it or update that slider. This is an interactive integration lab, not a closed-loop battery simulator. HA may load its normal onboarding defaults. The app uses live configured market and Open-Meteo adapters, while meter readings/history remain simulated. The Docker network is a regular bridge, not an egress firewall.

## Test sequence

### Price forecast sources

The real-Supervisor developer environment uses the ordinary price
adapters for their configured market. NL adds Jeroen.nl to EnergyPriceForecast
and EpexPredictor. Nord Pool published prices remain authoritative. Do not replace
saved contract rules, source preferences or archived prices to demonstrate a source.
Source choices follow the configured market. For example, BE excludes Jeroen.nl,
NO1 offers only EnergyPriceForecast EU, and HU has no configured forecast provider.
The single-source 48–72-hour window offers Automatic only as an enabled choice.
Do not switch persistent lab contracts to demonstrate these cases. The isolated
market-composition and desktop/tablet/mobile tests cover them without touching
saved lab settings. A transient outage retains the same eligible source choices.

Open Installation settings, Price sources. Verify the three lead periods,
actual source use, independent automatic recommendation, pairwise quality and
model issue/receipt times. A new source starts with missing comparative evidence.
It needs seven settled days and 168 matched original delivery hours before an
automatic change can qualify. Local deterministic tests provide that evidence
without fabricating history in the persistent developer environment.

Use Edit to choose a manual source for one period. Polling must preserve that
draft and keyboard focus. Save writes only price-source preferences. Cancel
restores those choices without discarding installation or strategy drafts.
Published prices still win and missing preferred coverage uses another available
forecast. Preserve the user's saved choices after any interactive demonstration.

### Simulated EV charging

The fixture adds **Test car A**, **Test car B**, **Test wallbox A** and
**Test wallbox B** without replacing existing Energy consumers. Their capacity,
SoC and cumulative AC consumption survive fixture restarts. New wallbox history
starts with synthetic zero usage. Existing grid counters incorporate subsequent
measured wallbox consumption once.

Open **Settings > Appliances > EV charging** to inspect the provisioned SoC,
Energy, power and connection sources. Both cars are allowed on both points.
The cars use 60/40 usable kWh and 11/7.4 kW respectively, with matching point
limits, a 1.38 kW minimum and variable-power capability. Existing EV configuration
is preserved rather than replaced with these defaults.

Use the test-only `oep_test_fixture.ev_connect`, `ev_soc` and `ev_power` actions
to change simulated facts. Indices 0 and 1 identify the two cars/points. Connect
a car, then select its identity using the point's native **Connected vehicle**
control or an automation based on the fixture's explicit vehicle identity.
Presence alone never identifies a car. Set target SoC and daily/one-off departure
through the native vehicle controls. No helper is required.

The provisioned **Open Energy Planner EV charging** blueprint instances forward
each virtual permission/power pair to that point's test-only `ev_power` action.
Their stop actions set the same point to zero. Never select a physical charger in this laboratory.
Verify charge progress, target stop, disconnect/reconnect withdrawal, different
vehicle limits, impossible-departure shortfall and persisted intent after restart.
See [the EV guide](EV-CHARGING.md) for defaults and modeled control boundaries.

### Actual Supervisor route

The default environment runs the actual Supervisor and Core in the official apps development image. Supervisor authenticates bootstrap and HA enforces login before opening real ingress. Only measurement/history fixtures are simulated. This proves those Supervisor interfaces without requiring a personal VM. It does not establish physical hardware or host-specific HAOS kernel behavior.

1. Open `http://localhost:7123/app/local_open_energy_planner` and choose Settings > Installation. HA ingress opens the wizard without an app token, verification code or approval click. Grid power, timezone, Energy sources and available history are supplied automatically. Use **Review detected settings** to inspect values. Existing saved physical limits remain. New unknown limits still need installation configuration.
2. Open **OEP Test Lab** in HA. With the EMS enabled and measurements ready, status becomes `active` and proposed battery/solar commands are available. Battery positive W means discharge. Negative W means charge. The displayed command is the current instruction, not a complete visual plan.
3. Turn **Open Energy Planner Enabled** off. Status becomes `paused` and commands become `unavailable`. Turn it on again. Commands return after a newly validated plan. Allow time for the solver and data refresh.
4. Turn **Test grid measurement available** off. Within a fixture tick, grid power becomes `unavailable`. The EMS cannot produce a valid new plan from this required measurement. Commands become unavailable when the current plan is invalidated. Turn the measurement back on and allow recovery to a newly validated plan. Do not treat missing data as zero.
5. Move **Test battery state of charge** from 50 to 60. The measured SoC changes within 15 seconds and is used by the next planning round. Replanning is deliberately rate limited. It is not instantaneous for every slider movement. Return it to 50 when finished.
6. Open **Energy** and select yesterday or an earlier day. The roof and garage have separate production totals. Import/export and battery channels are configured. The idle battery has zero throughput. These statistics are synthetic, not measurements from your home.
7. Stop and start the lab. The same login, Energy history and saved installation should remain. Commands become available again only after the app receives current measurements and validates a plan.

The plan/savings overview is available. Public release publication remains separate work. The lab preinstalls the integration in its isolated HA configuration. New lab setup waits for automatic bootstrap. It does not submit a legacy URL/token connection. Existing entries migrate in place, preserving identity/history. Nothing is installed in production HA. App data and HA Recorder volumes survive recreation.

HA ingress uses actual HA authentication. Direct unauthenticated app requests and forged ingress headers must not grant authority. The app has no separately published host port. Do not expose the privileged development container or its Docker socket on a LAN.

## Diagnostics

```console
docker compose -p oep-dev -f compose.dev.yaml exec dev ha apps logs local_open_energy_planner
```

A solver round may fail or time out without replacing the previous valid plan. Inspect the status and logs. An old plan is never prolonged to conceal failure. Sample proposed values depend on the time of day and the current solver result. Do not expect a particular wattage from a screenshot.

The authenticated `GET /v1/snapshot` endpoint provides the complete current plan and input provenance for technical inspection. Avoid posting raw snapshots or credential files publicly. Deterministic unit-source tests live in `tests/test_environment/unit/test_development_fixtures.py`, using the unchanged offline sources in `tests/test_environment/support/sources.py`. The ordinary app/integration/browser suite remains the regression baseline.

## Plan and savings acceptance

Use **Planning** and **Savings** in the real-Supervisor Docker developer app.
Existing HA ingress authentication is reused. Check a newly
published original plan, its expected grid/battery trajectories, and the separate
self-consumption reference. Opening old plans must not change active commands.

After a completed quarter and Recorder's ten-minute delay, use the date selector
to inspect actual grid cash, simulated reference, optional wear and separate
battery value. The first run starts at the next full quarter after new publication.
No earlier activation history is invented. Refresh for new evaluations. Changing
the selected period must not reset battery state or bonus counters. Coverage,
estimates, negative benefit and missing data remain visible. These environments
are simulations and do not prove physical execution or actual household savings.

Updates preserve existing app/Recorder volumes, credentials, Energy membership
and installation settings. No synthetic backfill of activation times is part of
verification. Compare the actual deployment and observed coverage in the PR.
Unit/browser fixtures do not establish that live capture has completed.

Planning graph axes start at the containing quarter: 22:10 displays from 22:00.
Current plans follow the current quarter. Explicit historical plans retain their
original period. Partial first intervals remain partial. No pre-plan energy is
invented. All time labels fall on quarter boundaries and may be skipped for
readability. Source-native price segments are preserved, including quarter-to-quarter
variation. An hourly forecast legitimately repeats its rate across quarters.

The persistent real-Supervisor developer environment uses the normal Open-Meteo solar and temperature adapters at
the configured Home Assistant site. PVWatts converts real irradiance, temperature
and wind forecasts into available solar production. Historical meter data remains
simulated, so model calibration is not evidence of real installation accuracy.
Open-Meteo chooses its default weather-model blend for the location. OEP does not
invent a separate weather model. Weather failures use normal diagnostics and
readiness rules, never silently switch to fake weather. Old archived plans retain
the original weather/forecast versions they used. Unit/CI tests remain deterministic
and use isolated fake providers rather than depending on live cloud availability.

## Quarter-resolution simulated energy history

The simulator varies import/export and each solar channel every 15 minutes. Four
quarter weights preserve the existing hourly profile and AC energy conservation.
Live cumulative meters integrate the current quarter. Power is its mean kW and
still refreshes every 15 seconds. The default battery remains stationary unless its simulated measurements are explicitly changed.

The dedicated fixture queues 32 recent days of native five-minute cumulative
statistics through Recorder, together with matching hourly statistics. Values are
end-labelled, as expected by the HA bridge: the row at :10 holds the :15 endpoint.
Every three native rows form one quarter. There is no direct database write and
no fictional HA quarter-statistics table. The fine-history import uses the pinned
2026.10 Recorder queue/table contract and is covered by actual HA read-back tests.
It is isolated from the production app and HA integration. Normal HA short-term
retention still applies. Hourly long-term history and OEP's retained settlement
records follow their existing policies.

Existing entity IDs, configured Energy sources, pairing and OEP plan archives
remain intact. The test-only seed marker upgrades fine history once and supports
interrupted retry. Repeat startup does not reset counters or keep reseeding.
Only this disposable test dataset is regenerated. Production price/weather
adapters retain native input resolution. No fake variation is added to forecasts.

## Appliance settings and daily operation

Apply this acceptance flow in the persistent real-Supervisor Docker developer environment.
Keep every existing source, device ID, model setting and archived plan. Open
Settings > Appliances and switch between Consumption and cycles, Electric
boilers and EV charging. Verify that the correct Settings navigation stays
active. Edit one type, try another category, and confirm the draft is retained
until Save or Discard. Verify saved values after a reload.

Open Energy > Thermal storage and Energy > EV charging. These pages must not
offer device creation, source bindings, capacity or hard-limit editors. Verify
their original charts/current evidence and the separate daily comfort, pause,
target-SoC and departure actions. Read back the single-field intent and unchanged
technical settings. Rejected requests must retain input. Evidence refresh must
not overwrite an unapplied request. Do not actuate production equipment.

## Default page

A completed installation opens **Planning** at the app root, including a Home
Assistant app-page refresh. An incomplete installation still opens the wizard.
Explicit **Installation settings** links continue to open `/setup`.

## Original forecast quality acceptance

Open **Forecast quality** in the real-Supervisor developer app. Keep existing settings and history. Newly published plans freeze prediction references and metering identities. Select an earlier plan after at least one complete forecast hour and the Recorder delay. Verify measured and original predicted energy, gaps for unavailable hours, HA-local times, per-source model provenance and current model readiness. Toggle chart series or use the hourly evidence table. A newly installed page cannot invent archived bindings for old plans. Keep simulated decision costs and measured savings separate. See [the evidence contract](../development/FORECAST-QUALITY.md).
