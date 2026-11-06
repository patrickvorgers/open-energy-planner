# Troubleshooting

Start with the exact page status, affected source and requested action. A connection, a forecast, a plan and physical execution have different readiness conditions. Keep the original error and avoid changing unrelated settings.

## Find the right check

| What you see | Start here |
|---|---|
| Token prompt or connection failure | [Browser and integration connection](#cannot-connect-the-browser) |
| Wizard cannot continue | [Installation setup](#installation-setup-is-blocked) |
| No plan, or only an older plan | [Planning inputs and calculation](#no-plan-available) |
| A cost or benefit is unavailable | [Contract and comparison](#cost-or-benefit-is-unavailable) |
| Missing historical savings or a forecast score | [Coverage](#savings-or-quality-is-incomplete) |
| A ready appliance did not start | [Execution feedback](#equipment-did-not-follow-the-plan) |
| A storage operation was interrupted | [Storage recovery](#database-migration-was-interrupted) |

## Cannot connect the browser

**Standalone demo:** confirm the running process, its port and the selected data directory. Read `administration-token` in that directory unless `OEP_API_TOKEN` overrides it. Use the token from this instance, not another laboratory. Never put it in a URL, screenshot or issue.

**Supervisor ingress:** the Home Assistant session should authenticate the browser. Check that the app and integration are running, their version/build match the intended package and automatic connection completed. Do not paste an unrelated administration token to hide a broken ingress route. Use [pairing diagnostics](../development/AUTOMATIC-PAIRING.md#readiness-diagnostics-and-manual-fallback) for the actual connection state.

**Success looks like:** the intended page loads its authenticated data and the connection status is ready. A loaded static page alone does not establish that its APIs are authenticated or the integration is connected.

## Installation setup is blocked

Check the named missing item in **Home Assistant Settings > Dashboards > Energy**. Required power, capacity, SoC or cumulative-energy links belong there. Discovery can identify a configured device before its current measurements or history are usable.

For a combined power sensor, inspect both constituents in HA. One missing half does not become zero or leave a valid half-sum. Check W/kW versus Wh/kWh and the normalized direction.

If history is insufficient, enter a confirmed annual whole-home estimate where offered. If linked solar panel metadata cannot be refreshed, repair that link or discovery rather than editing unrelated inverter values. A temporary outage must not silently transfer ownership of linked geometry.

**Success looks like:** the wizard preview accepts the complete definition and saved settings show the intended sources and ratings. See [Installation settings](installation.md).

## No plan available

**Synthetic demo:** wait for startup and the first calculation, then use **Refresh overview**. Read the first actual calculation error in the terminal. An unsupported native package is not a missing Home Assistant sensor.

**Home Assistant mode:** check the source named by the warning, saved installation, required measurement freshness, forecast availability, contract dates and price coverage. Missing inputs do not receive an unlabelled synthetic replacement. The [configuration sequence](configuration.md) helps identify the missing owner.

An unsuccessful new round does not overwrite the original accepted plan or prolong its commands. A historical chart is not a promise of an active target. Once inputs recover, confirm that a new accepted plan has the expected generation time and strategy.

**Success looks like:** a coherent new plan is published and, separately, accepted by HA where applicable. Physical behavior still needs its own evidence.

## Cost or benefit is unavailable

Check whether the relevant scenario comparison exists and covers the same original horizon. Do not substitute the self-consumption comparison for the without-battery baseline. Missing costs are not zero savings.

Review effective tariff dates, formula preview, bonus opening evidence and the distinction between original and repriced flows. A current price preview is not the selected historical plan's original input. Read [Contract and prices](contract.md) and [Savings](savings.md).

**Success looks like:** the comparison is available with explicit period and basis, or remains unavailable with an accurate reason. Changing a label does not establish missing financial evidence.

## Savings or quality is incomplete

Check the reported coverage and first blocked source or interval. Recorder needs time to finalize statistics. A missing published price can delay settlement. Current readings do not replace missing historical endpoints, and a counter reset requires valid new evidence.

Forecast quality needs an actually captured midnight baseline and complete comparable evidence before it scores a full day. Startup after midnight cannot invent that baseline. Controlled consumption and some PV boundaries may remain unscored for a stated reason.

**Success looks like:** gaps are filled by real retained evidence or remain explicitly visible. Do not reset the reference, rewrite the old forecast or fill unknown hours with zero. See [forecast interpretation](forecasts.md) and [savings coverage](savings.md#inspect-coverage-before-totals).

## Household history excludes hours

OEP can exclude unreliable negative household balances from forecast training while leaving Recorder unchanged. Diagnostics report usable/excluded hours and the excluded percentage. A notification appears when at least 20.0% of complete assessed hours are excluded after at least 24 complete hours have been assessed.

Check the named Energy sources for overlap or wrong direction. You do not need to edit training records manually. OEP retains usable hours and can keep the startup estimate when too little usable history remains. The [training policy](../development/DEMAND-FORECASTING.md#training-only-negative-balance-policy) describes the small-roundoff tolerance and exclusion boundary.

## Equipment did not follow the plan

First distinguish a planned time, a current virtual command, HA receipt and an actual action. The app does not call a device vendor or guarantee that a user automation ran.

Check mode, applicability, expiry, pause state and the automation's actual execution evidence. For cycle appliances, **Start not confirmed** is uncertain execution, not permission to issue a duplicate start. For an EV, connection alone does not identify the vehicle.

**Success looks like:** the supported action and feedback establish what happened, while the next plan uses fresh measurements. Do not infer physical completion from publication or acknowledgement. Use the relevant [appliance contract](appliances.md) and [virtual-device reference](../development/VIRTUAL-DEVICES.md).

## Database migration was interrupted

Reopen **Settings > Data storage** and read the recorded phase and recovery options. Closing the tab or losing HTTP did not cancel the server operation. Before target release, source restoration may be available. After release, repair the target rather than reverting to an old source that can hide newer writes.

Follow [Data storage and preservation](data-storage.md#respect-the-recovery-boundary) and the administrator recovery instructions offered by the app. Never delete private migration journals or select an empty database to bypass recovery.

## Installation or dependency failure

Record the operating system, Python version, command and first error. Use pinned dependencies. If native wheels are unavailable for that platform, use the supported [isolated developer route](../development/README.md#first-start), rather than changing versions without compatibility evidence.

A bundle/check-out mismatch needs the original matching artifact and its actual Verified merge. Do not rebuild the ZIP or overlay loose source onto a numbered installation.

## Ask for help safely

Include app/integration version and build, the page and action, expected versus observed behavior, input mode and relevant sanitized error. State whether the data is synthetic. Do not attach tokens, passwords, exact location, private bootstrap files or a production database.

Use the [issue tracker](https://github.com/patrickvorgers/open-energy-planner/issues) for ordinary problems and the [security policy](../../SECURITY.md) for vulnerabilities. A working demo or a green test is not physical equipment acceptance.
