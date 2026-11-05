# Find your next step

Begin with the visible symptom and retain the original error. Do not reset databases, replace saved limits or change dependency versions just to make a warning disappear.

## Cannot access the repository

The repository and documentation are public. Check that your clone URL is `https://github.com/patrickvorgers/open-energy-planner.git`. Verification of an original CI installation bundle still requires GitHub read access through your existing host credentials. Public HACS installation is not available yet. Keep tokens out of the documentation site and issue reports.

## Demo opens the setup wizard

The demo is for inspecting synthetic planning, while setup requires Home Assistant mode. Open `http://127.0.0.1:18765/overview#planning` directly. Use the port chosen in your own start command. For the real wizard, follow [Connect Home Assistant](home-assistant.md).

## Cannot connect the browser

For the standalone demo, confirm that the app is running, the URL uses its actual port and the token belongs to its data directory. The token file is `administration-token` in the directory selected with `--data`, unless `OEP_API_TOKEN` overrides it. Do not send tokens with a bug report.

For Supervisor ingress, authentication should use the Home Assistant session. Check the original package identity, app status and automatic pairing. Follow the [pairing reference](../development/AUTOMATIC-PAIRING.md). Do not put the app administration token in the HA integration.

## No plan available

In the synthetic demo, wait for startup and the first calculation, then choose **Refresh overview**. Inspect the terminal for an actual calculation error. A native package failure is not a missing Home Assistant sensor.

In Home Assistant mode, open the app's diagnostics and check the source named by the warning. Review saved installation settings, measurement freshness, price coverage and forecast availability. A valid Energy entity alone does not establish complete inputs. Missing inputs do not receive a synthetic fallback.

Keep an original valid plan separate from a failed new round. A visible older plan does not prove its command is still applicable. The [planning notification guide](../development/PLANNING-NOTIFICATIONS.md) explains recovery and action-required states.

## No vehicle or connection sensor appears

In Settings > Appliances > EV charging, choose the Home Assistant integration
before choosing its sensors. The car's state-of-charge sensor must report `%`.
A sensor with an unrelated device class, such as humidity, is not a vehicle SoC
source.

For a charge point, search its integration's binary sensors by name or entity ID.
Use a sensor that stays On while plugged in, including when charging is paused.
A charging-active sensor is not equivalent. Check the sensor in Home Assistant
if its purpose is unclear. Choose the charge point's Energy consumption meter
separately. After correcting HA configuration, save or cancel pending edits and
use Check saved settings to reload available metadata. The app's catalogue is
bounded. Related sensors on Energy devices receive priority, while unrelated
sensors may not all fit.

## Household energy training excludes hours

OEP excludes unreliable negative household balances from forecast training. Diagnostics and history-assessment logs report usable hours, excluded hours and the excluded percentage. Recorder data remains unchanged. Small excluded shares do not create a notification.

A notification appears when at least 20.0% of complete assessed hours are excluded, after at least 24 complete hours have been assessed. Check the Home Assistant Energy balance for overlapping sources and separately measured devices. OEP retains usable hours and uses the annual-consumption startup estimate when too little history remains.

## Installation or dependency failure

Keep the first error and record the operating system, Python version and command used. Use the pinned dependencies. If native packages are unavailable on your platform, use the supported isolated developer path. Do not claim platform support from an unverified package workaround.

If an accepted bundle does not match the checkout, use the original artifact and actual verified merge. Do not rebuild its ZIP or overlay source files. See [developer environment recovery](../development/DEVELOPER-ENVIRONMENT.md).

## Ask for help safely

Provide the app and integration version/build, the page and action, expected and observed behavior, and relevant sanitized errors. Distinguish synthetic data from Home Assistant measurements. Remove tokens, passwords, location details and private entity information. With repository access, use the project's GitHub issue tracker or the maintainer's existing support channel.

## What remains unproven

A working local demo does not establish a complete product release, compatibility with your equipment or physical execution. A technical walkthrough also does not prove that a newcomer can follow the guide without help. Those need separate user and platform acceptance.
