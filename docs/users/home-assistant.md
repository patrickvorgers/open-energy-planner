# Connect Home Assistant

OEP needs two matching components: the calculation app and the Home Assistant integration. If both are already installed, start at [Open the app](#open-the-app). New installation remains an administrator-managed preview procedure rather than a public one-click HACS flow.

## Install a matched pair

Use the original app/integration artifacts from the same successful numbered candidate or release. Verify their expected version, build and source together. A successful Quick check or an arbitrary current source checkout is not an accepted installation package.

The [installation acceptance guide](../development/HAOS-ACCEPTANCE.md#install-a-matched-product-release) owns administrator installation and update checks. Preserve Home Assistant Energy preferences, device/entity identities and existing OEP data. The app does not copy integration files into HA or request a general HA user token.

For a populated isolated laboratory instead of a user-managed installation, follow [developer environment preparation](../development/README.md#first-start). That route runs actual Supervisor and HA with simulated devices. It is a development tool, not a prerequisite for operating an existing OEP installation.

## Open the app

In the Supervisor route, open OEP through its Home Assistant app/ingress entry. Supervisor handles authenticated discovery and connection. Ingress uses the existing HA browser session. No app administration token, copied address or pairing approval should be needed.

For a standalone container, administration uses the app's own token and the integration uses its separate explicit pairing flow. Follow [connection discovery and pairing](../development/AUTOMATIC-PAIRING.md). Do not paste the app administration token into an unrelated HA integration field.

**Check:** the app opens, the connection is ready and both loaded component identities match the intended package. A token prompt in trusted ingress calls for connection diagnosis rather than copying another installation's credentials.

## Review installation settings

### Dates and times

When opened inside Home Assistant, date labels follow your Home Assistant profile's
language and date-format preference. Times retain the installation timezone.
After changing your profile preference, reopen or refresh the page to update
existing labels. A standalone page without access to your HA profile uses the
browser locale. Native date and time picker controls use the browser's own
presentation. Formatting never changes saved dates, deadlines or timestamps.

### Find virtual devices in Home Assistant

Open **Settings > Devices & services > Open Energy Planner**. One connection
contains the configured groups **Solar**, **Batteries**, **Consumption and cycles**,
**Electric boilers** and **EV charging**. Empty groups are hidden. General EMS
and Grid devices remain directly under the connection.

Each group can contain multiple virtual devices. A weather-sensitive heat pump
or air conditioner belongs to **Consumption and cycles**. Names do not determine
membership, so renaming a device does not move it. Your existing device and entity
IDs, custom names, areas and labels remain intact when groups are introduced.

Add or remove installation devices and appliances in the app. The HA groups
follow that configuration automatically. A temporary connection failure keeps
the existing groups. Groups are not extra hub devices and do not change physical
device relationships.

Do not use HA's group **Delete** action to hide or ungroup devices. It deletes
their virtual registrations and can discard customizations. The integration
automatically reconnects to restore groups and devices still configured in the
app. If the app is unreachable, restoration finishes when it is available again.
To remove a device permanently, remove it in the app.

### Configure the installation

Open **Settings > Installation**. A new or incomplete definition opens the five-stage wizard. A complete definition opens saved summaries and an area selector.

Check source ownership in Energy, then confirm connection limits, household scale, solar properties and batteries. Zero solar sources and zero batteries are valid. Do not overwrite retained settings with a sample merely to reproduce a guide.

Choose **Save installation** only after inspecting the review. Wait for confirmation and read the saved summary. A refresh does not save a draft. The [installation guide](installation.md) explains the fields and the strategy choice.

## How sources stay current

After startup, the integration listens for relevant Home Assistant configuration
changes. It keeps the Energy sources and additional sensors you have configured,
including EV and boiler measurements. Energy-selected Forecast.Solar installations
and their panel settings belong to that same selection. Other forecast providers
are currently unsupported.

Ordinary measurement updates do not reload the source list. There is no periodic
full scan of every entity. Add/Edit in the appliance settings fetches matching
choices on demand. Use **Refresh Home Assistant sources** when a new sensor is
missing. Your unsaved inputs remain intact. If refreshing fails, check that the
integration is connected and try that action again. A refresh does not save edits
or change the configured Energy membership.

## Confirm prices and a plan

Open **Settings > Contract** to check the effective tariff rules. Inspect **Settings > Installation > Price sources** for availability and source preference. The [contract guide](contract.md) explains the distinction between market prices, effective rates and bonuses.

Then open **Energy > Planning**. A usable plan requires coherent source configuration, fresh required measurements, forecasts, contract rules and sufficient price coverage. External provider availability can delay or shorten the horizon. Missing Home Assistant inputs are not replaced by an unlabelled synthetic fallback.

**Check:** the original plan has a version and generated time, reflects the intended strategy and contains the expected installation sources. Use [Read your plan](read-your-plan.md) before interpreting its virtual commands. If it is unavailable, use [troubleshooting](troubleshooting.md).

## Keep control separate

OEP publishes virtual advice. Your own HA automation/controller must implement physical actions and preserve manufacturer and electrical safeguards. Integration receipt is not execution confirmation.

Use the [virtual-device sensor reference](virtual-device-sensors.md) for every
non-diagnostic sensor by device type, its values and units, and how to use it in
an automation. It also explains command attributes and the differences between
measurements, forecasts, zero, unknown and unavailable.

Battery fixed-power targets use whole DC watts, positive for discharge and negative for charge. Zero is a real idle target. Unknown and unavailable have different meanings. Follow the [virtual-device contract](../development/VIRTUAL-DEVICES.md) before connecting physical equipment, and the relevant [appliance execution contract](appliances.md) for readiness, claims and feedback.

**Next:** [Configure OEP](configuration.md), then [read the plan](read-your-plan.md).
