# Installation settings

Use **Settings > Installation** to describe the physical system OEP may plan. Home Assistant Energy supplies source identities. OEP adds the physical limits, connections and planning choices that Energy does not define.

## Complete the first installation

The first-run wizard has five stages: Home Assistant, connection and household, solar production, batteries, and review. Complete the detected installation, inspect the review, then choose **Save installation**. Merely advancing a stage does not save the whole installation.

If Energy is incomplete, correct it in Home Assistant and refresh discovery. Do not add replacement entity IDs in an unrelated OEP field. A configured meter may be discoverable before it has usable readings or history.

Large source lists are transferred in complete, bounded parts. The app keeps the
previous complete list if a refresh fails before the new list is published.
If the final acknowledgement is lost, the complete new list may already be
visible. There is no fixed configured-device maximum.
See [hardware sizing and calculation time](planning-performance.md#size-hardware-for-your-installation)
for the effect of a larger installation.

## Connection and household

Confirm **Maximum grid import (kW)** and **Maximum grid export (kW)** for the complete modeled connection. These are aggregate planning limits, not per-phase protection. Use the permitted limits for your equipment and connection rather than copying the example.

Household energy is reconstructed from the same measured boundary:

```text
grid imports + solar production + battery discharge
- grid exports - battery charging
= household consumption
```

OEP can estimate annual household use from a common complete history period. A shorter period is explicitly extrapolated and may have seasonal bias. With insufficient usable history, confirm **Annual whole-home consumption (kWh)** as a startup estimate. Empty history does not mean zero demand.

[![Saved connection and household settings with separate import/export limits and a synthetic annual consumption estimate.](../assets/installation-connection.png)](../assets/installation-connection.png)

*The limits describe the connection. The annual consumption figure describes household scale. They have different units and purposes. All displayed values are synthetic examples.*

## Solar production

Review every detected solar source. Confirm its physical properties and inverter relationship rather than treating a production meter as a complete panel model. OEP needs the correct production boundary and the properties required by the selected forecast route.

Sources sharing an inverter also share its capacity. A standalone solar inverter and a hybrid inverter with a battery have different energy paths. Do not assign an independent full inverter capacity to every source on one shared converter.

Permission to curtail production is explicit. A negative export price alone does not establish that your hardware can stop production. If OEP warns that fixed solar can exceed the export limit, review the ratings and real control capability. The planner does not silently assume a local export limiter.

## Batteries

Energy supplies each battery's usable capacity, state-of-charge source and normalized power links. Missing capacity, SoC or power configuration must be corrected there. OEP needs the battery's charging/discharging limits, reserve, conversion assumptions and connection to a standalone or shared inverter.

[![Battery settings populated with a synthetic battery, its planning reserve and charge/discharge limits.](../assets/installation-battery.png)](../assets/installation-battery.png)

*Usable capacity and SoC define the stored-energy basis. Power limits and reserve constrain what may be planned. Values in this image are examples, not recommended equipment settings.*

Review any suggested efficiency or rating against the manufacturer's information and your physical configuration. A planning reserve is not a battery-management-system protection setting. If the battery is below reserve, the measured value remains real. The optional reserve-recovery policy can prioritize restoring it within the modeled limits.

Enable **Can follow zero grid target** only if your own controller or automation can implement that mode. It is a capability permission, not a promise of physically exact zero grid exchange.

## Choose the planning strategy

Select **Planning strategy** in the installation area selector. The saved **Optimization goal** row shows **Minimize cost** or **Minimize consumption**. Open **Compare optimization goals** for the full explanation, then use **Edit installation** to change the choice.

[![Saved optimization goal and its explanation in Installation settings.](../assets/optimization-goal.png)](../assets/optimization-goal.png)

*The selected goal is saved configuration. Home Assistant acceptance is separate status evidence and does not prove physical execution.*

The new choice requests a replacement plan. Requested, published and accepted strategies can temporarily differ. A failed replacement does not rewrite the old plan or extend its validity. See [how the goals differ](how-oep-works.md#choose-the-optimization-goal).

## Revisit a saved installation

Select the area or source you want to inspect before choosing **Edit installation**. Area changes are locked while editing or saving. Changing which source is displayed does not itself change the configuration.

After saving, check the saved summary and then **Planning**. If the setup cannot proceed, use [troubleshooting](troubleshooting.md). Developer contracts for source ownership, topology and validation remain in [Installation planning](../development/INSTALLATION-PLANNING.md) and [Energy mapping](../development/ENERGY-DASHBOARD.md).

**Next:** [Contract and prices](contract.md).
