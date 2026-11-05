# Appliances and daily requests

Use **Settings > Appliances** to configure what a device is and what OEP may plan. Use its **Energy** page for today's request. Forecasting, scheduling permission and physical execution are separate choices.

## Choose the right category

| Category | Configure | Use each day |
|---|---|---|
| Consumption and cycles | Energy consumer, weather sensitivity, cycle-shifting permission and any manual fallback | **Energy > Appliances** for cycle evidence, readiness and deadline |
| EV charging | Vehicles, charge points, permitted assignments, power limits and charging assumptions | **Energy > EV charging** for sessions, target and departure |
| Electric boilers | Source, temperature/power measurements, tank model and planning permission | **Energy > Thermal storage** for comfort and pause/resume |

Home Assistant Energy owns consumer meters and their relationships. Do not select a total and one of its child meters as independent loads, or reuse the same consumption source for multiple controlled categories. OEP needs a disjoint household boundary to avoid double counting.

## Schedule a complete appliance cycle

1. Configure the appliance's energy and power measurement in Home Assistant Energy.
2. Select it under **Settings > Appliances** and save its forecast membership.
3. Enable complete-cycle shifting only when the appliance can be started through a suitable automation.
4. Inspect its cycle evidence under **Energy > Appliances**. Complete detected cycles establish a learned profile. A manual duration and energy estimate can serve as an explicitly labelled fallback before enough evidence exists.
5. Load and prepare the appliance, choose the finish date/time and explicitly declare it ready.
6. Inspect task status and the proposed start. A deadline alone does not declare readiness or start equipment.

[![An appliance with a populated cycle profile and explicit finish-time and readiness controls.](../assets/appliance-request.png)](../assets/appliance-request.png)

*Cycle energy and duration explain the work being scheduled. The readiness action declares that this loaded appliance may be planned. The pictured values and device are synthetic.*

The planned start is information. Physical execution needs the supported claim-and-feedback automation flow. Missing start confirmation remains uncertain execution rather than permission to retry. **Cancel planning** removes future planning authorization, but does not prove that the appliance stopped physically. See the [execution contract and blueprint](../development/SHIFTABLE-APPLIANCES.md#from-dashboard-intent-to-physical-execution) before wiring equipment.

## Prepare EV charging

Configure the vehicle's usable battery capacity, SoC source and charging limit separately from the charge point. The point needs its own consumption/power source, connection evidence, allowed vehicles and declared control capability.

A connected plug proves presence, not vehicle identity. Explicitly identify the connected vehicle through the supported Home Assistant control or an automation that genuinely knows it. A previous session's assignment does not authorize a later connection.

Set the desired SoC and departure under **Energy > EV charging**. A one-off departure overrides the daily departure until that instant. Home Assistant's timezone applies. The planner recalculates required energy from the current SoC and reports a shortfall when the target cannot be reached under the admitted limits. V2G is not supported by this charging model.

See [EV charging contracts](../development/EV-CHARGING.md) for supported capability modes, losses, session identity and the automation blueprint.

## Prepare thermal storage

The supported thermal model is a fixed-power resistive electric water heater. Configure it under **Settings > Appliances > Electric boilers** using its electricity source, temperature/power measurements, volume, temperatures, standing losses and expected hourly heat draw.

The hourly draw values are assumptions, not measured hot-water use. This model does not implement heat-pump or room-temperature control. A heat pump can still be an individually forecast consumer without becoming an electric boiler.

Use **Energy > Thermal storage** to inspect temperature, proposed heating and any comfort/service shortfall. Daily comfort requests and pause/resume do not replace the saved physical model. Independent hardware protection remains necessary. See [thermal storage contracts](../development/THERMAL-STORAGE.md).

## Check the outcome

After any daily request, confirm the intended device and saved/requested state. A failure must retain your input. An accepted request is not evidence of physical completion. Use the device's real status and the supported execution feedback to establish what happened.

**Next:** [Read your plan](read-your-plan.md), or [understand evaluated savings](savings.md).
