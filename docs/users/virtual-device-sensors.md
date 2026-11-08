# Use virtual-device sensors

Use the non-diagnostic sensors on OEP's virtual Home Assistant devices to read
planning advice, inspect measurements and connect your own physical-device
automations. This reference explains each sensor by device type, including the
binary sensors used for charging and heating permission. Diagnostic entities are
outside this reference.

Open **Settings > Devices & services > Open Energy Planner**, choose the planning
group and then the virtual device. General **Open Energy Planner** and **Grid**
devices are directly under the connection. The other groups are **Solar**,
**Batteries**, **Consumption and cycles**, **Electric boilers** and **EV charging**.
Only configured devices and supported capabilities appear.

This reference uses the default English entity labels. HA can translate enum labels,
and you can rename devices or entities. The values in backticks are the underlying
states used by automations. Select the actual entity from your installation
rather than assuming an entity ID from its display name.

## Read advice before controlling equipment

A target is a read-only proposal. It does not show that a physical action happened.
Measured sensors describe actual equipment readings. Forecast sensors describe
estimates. Your HA automation or local controller translates accepted targets
into physical actions and retains electrical limits and equipment protections.

| State or attribute | Meaning and automation use |
|---|---|
| Numeric `0` | A real numeric value, interpreted according to the sensor. A battery's fixed `0 W` target means idle. It does not mean a missing target. |
| `unknown` | The entity exists but has no value, for example a battery power target while following the grid. Read its mode and `applicable` attribute. Never convert this automatically to zero. |
| `unavailable` | The entity cannot currently provide its value. Do not retain an old command as current authorization. Use your controller's defined fallback. |
| `applicable: false` | This numeric target is not used in the current mode. It is not an instruction to apply zero. |

An EMS pause, expired plan or missing current interval withdraws ordinary grid,
battery and solar commands. Boiler and EV permission have additional local checks,
explained below. A temporary app connection failure does not by itself prove that
a previously accepted plan has expired. The integration continues to enforce its
validity and freshness boundaries. Restoring the connection does not prove execution.

## Open Energy Planner: general EMS status

**Status**

`active`: a usable plan covers the current interval. `paused`: EMS intent is off. `waiting_for_new_plan`: resumed, but a plan generated after resuming is still needed. `not_ready`: no usable plan or no current interval. `expired`: the plan's validity ended. `stale`: its permitted age ended. **Use:** Show overall readiness and gate an automation. `active` alone is insufficient for an individual device. Check its own target or permission too.


**Status** remains readable without a usable plan. Its attributes give context:

| Attribute | Meaning and use |
|---|---|
| `plan_id` | Identity of the retained plan, if present. Correlate observations with a plan. Its presence does not make an expired plan usable. |
| `strategy` | The retained plan's strategy. Use it to interpret the plan, not as a physical command. |
| `connection_issue` | Current connection problem, if any. Use for connection troubleshooting. |
| `connection_type` | `supervisor` or `standalone`. Describes the connection route. |
| `planning` | App planning-status information. Inspect why a new plan is pending or blocked. |
| `observations` | Integration observation information. Inspect input reporting alongside planning status. |

The **Enabled** switch changes EMS planning intent. It is a separate control, not
physical execution confirmation. The diagnostic plan-generation sensor is not
part of this sensor reference.

## Grid

**Control mode**

`zero_grid` means the current command activates the zero-grid goal. `inactive` means that goal is not active. **Use:** Select whether your local controller should follow the grid goal.

**Power target**

`0 W` in `zero_grid` mode. `unknown` with `applicable: false` in `inactive` mode. Arbitrary nonzero grid targets are not supported. **Use:** Supply the AC grid-boundary setpoint to a controller when applicable. It is not measured grid power.


**Power target** attributes are `applicable`, `mode` and `measurement_side`.
`mode` is `follow_grid_target` when the grid goal is active, otherwise
`fixed_power`. `measurement_side: grid_ac` identifies the AC grid boundary.
Without a usable command, `applicable` is false and the command sensors are
unavailable.

The grid goal does not choose a physical executor. Coordinate multiple batteries
or inverters in your own controller so they do not compete for the same setpoint.

## Solar

**Enabled target**

`on`: the plan permits production. `off`: the plan proposes curtailment by switching production off. Unavailable without a usable current device command. **Use:** Map the proposal to your inverter's supported enable/disable action. This sensor is not a writable switch and does not confirm actual production.


This sensor exists only for a configured solar source with on/off curtailment
permission. A fixed-production source has no command sensor. `on` does not imply
sunlight, a production level or a measured output in watts. No variable-power
solar setpoint is published here.

Its attributes are `mode: on_off`, `applicable` and
`measurement_side: solar_production`. `applicable` indicates that the current
command exists. The **Enabled target** sensor shown in HA's **Sensors** card is
the proposal your automation can follow. The separate **Diagnostic** card contains
configuration evidence rather than additional solar commands.

## Batteries

**Control mode**

`fixed_power`: apply this battery's numeric target. `follow_grid_target`: follow the Grid device's target instead. **Use:** Choose the appropriate branch in your battery/inverter automation.

**Power target**

Whole DC watts at the battery terminals in `fixed_power` mode. Positive means discharge, negative means charge, and zero means idle. In `follow_grid_target`, the value is `unknown` and `applicable` is false. **Use:** Convert the signed DC target to the physical controller's supported units and convention. Do not feed it directly into an AC setpoint without accounting for the different boundary and losses.


For example, `-1200 W` means a proposed charge of 1,200 DC watts, not discharge
and not 1,200 watts of measured grid import. Both sensors become unavailable when
there is no usable current command for that battery.

Both sensors carry these interpretation attributes:

| Attribute | Meaning and use |
|---|---|
| `mode` | The current battery mode. Read it together with the numeric target. |
| `applicable` | For **Power target**, true only in `fixed_power`. For **Control mode**, true when its command exists. |
| `grid_device` | `grid` while following the grid goal, otherwise empty. Points to the authoritative goal, not a second battery setpoint. |
| `measurement_side` | `battery_terminal_dc`. Identifies the battery-side DC power boundary. |

Grid following appears only when configured as a supported capability and selected
by the plan. It is not an always-on battery strategy. See the
[zero-grid contract](../development/VIRTUAL-DEVICES.md#zero-grid-selection).

## Consumption and cycles: forecast consumers

Each selected Energy consumer can expose the following sensors. A weather-sensitive
heat pump or air conditioner is still a consumption device. Forecast membership
does not grant temperature control or physical start permission.

**Forecast power**

Estimated mean watts for the forecast interval containing the current time. Computed from interval kWh divided by interval hours, multiplied by 1,000. Unknown if no interval covers now. **Use:** Display expected load or provide context to your own automation. It is not a measured power sensor or a dispatch target.

**Forecast horizon energy**

Sum of all interval estimates in the published forecast, in kWh. It includes the whole published horizon, not just the remainder after now. **Use:** Understand predicted consumption. Do not use it as a cumulative Energy meter.

**Forecast status**

The readiness states below. Remains readable without a forecast. **Use:** Explain why an estimate is available, in shadow evaluation or blocked. It does not authorize physical execution.


The two numeric sensors are unavailable when no forecast exists or its generation
time is in the future or at least 30 minutes old. They can have a different
availability from the EMS command sensors.

| Forecast status | Meaning |
|---|---|
| `waiting_for_history` | No usable forecast evidence is ready yet. |
| `active` | The individual consumer forecast is active. |
| `shadow_waiting_for_decision_evidence` | The candidate is still gathering evidence before it may affect planning. |
| `history_or_weather_not_ready` | Required history or weather is not ready. |
| `energy_consumer_removed` | The source is no longer a configured Energy consumer. |
| `existing_exclusions_unsupported` | The existing household exclusions cannot be combined with this selection. |
| `consumer_overlaps_household_boundary` | This consumer overlaps the household boundary and would double count energy. |
| `energy_consumer_hierarchy_inconsistent` | The consumer hierarchy does not provide a consistent selection. |
| `household_boundary_missing` | The required household consumption boundary is missing. |

Forecast sensor attributes are `source_entity` (the original Energy meter), `model`
(the forecast model), `forecast_generated_at` (generation time), and
`estimated: true`. Use them to identify the source and interpret the estimate.
They are not physical execution feedback.

## Consumption and cycles: shiftable appliances

Enabling complete-cycle shifting adds these sensors to the consumer's virtual
device. Its forecast sensors remain separate from cycle authorization.

**Task status**

The states below, or `idle` when there is no task. **Use:** Follow the task lifecycle and detect uncertain execution.

**Planned start**

Scheduled start timestamp, or unknown when unscheduled. **Use:** Display the schedule. A timestamp alone does not authorize a physical start.

**Planned completion**

Scheduled start plus the accepted profile duration, or unknown without both. **Use:** Display expected finishing time. It is not actual completion feedback.

**Expected cycle duration**

Profile duration in minutes. Uses the task's frozen profile when present, otherwise the device profile. **Use:** Explain how much work is scheduled.

**Average cycle energy**

Energy of the current device profile, in kWh, including an explicit estimate when that is the available profile. Unknown without a profile. **Use:** Interpret the expected cycle cost/load. This may differ from an already accepted task's frozen profile. It is not a measured completed-cycle total.

**Start request**

`start` while a current unexpired request and usable plan command exist. Otherwise `idle`, or unavailable when no usable plan command exists. **Use:** Use the supported claim-and-feedback blueprint. Reading `start` alone is insufficient authorization.


| Task status | Meaning |
|---|---|
| `idle` | No current task. |
| `pending` | Task accepted and waiting for scheduling. |
| `scheduled` | A start has been scheduled. |
| `requested` | A start attempt has been issued. Physical start is not yet confirmed. |
| `start_not_confirmed` | The confirmation window ended without conclusive feedback. Do not retry automatically. |
| `running` | Actual start has been recorded. |
| `completed` | Completion has been recorded. |
| `cancelled` | Planning was cancelled. This does not prove a physical stop. |
| `deadline_not_achievable` | The task cannot currently meet its deadline. |
| `forecast_not_ready` | Required forecasting/profile evidence is not ready. |

Task evidence remains visible during plan outages. Every cycle sensor carries
`source_entity`, `task_id`, `attempt_id`, `expires_at` and `deadline` attributes.
The expiry describes the recorded attempt. It does not grant a new execution
lease. The claim response supplies the current authorization and its expiry.
Use these identities for correlation, not to fabricate or replay an attempt.

Follow the [appliance execution contract and blueprint](../development/SHIFTABLE-APPLIANCES.md#from-dashboard-intent-to-physical-execution).
It claims the exact attempt once, checks authorization immediately before the
physical action and reports the outcome. A repeated request, restart or lost
confirmation must not start another cycle. Planning cancellation and EMS pause
do not prove that an already running appliance stopped.

## Electric boilers

These sensors apply to the fixed-power resistive water-heater model, not a
heat-pump or room-temperature controller.

**Water temperature**

Current accepted local measurement in °C, or unknown without usable temperature/power evidence. **Use:** Observe actual tank temperature. It is separate from the prediction.

**Requested heating power**

The configured full heater power in W when heating is permitted, `0 W` for an accepted off interval, or unknown when permission cannot be established. **Use:** Read the planned on/off demand. This is not continuously variable heater control.

**Predicted interval-end temperature**

Forecast tank temperature in °C at the end of the current planned interval. Unknown without an accepted matching interval. **Use:** Inspect the expected result of the plan, not a thermostat setpoint.

**Predicted comfort shortfall**

Modelled thermal energy deficit in kWh for the current interval's minimum-temperature comfort requirement. Unknown without an accepted matching interval. **Use:** Recognize predicted comfort shortage. This is thermal energy, not missing electrical meter consumption.

**Heating requested**

Binary `on` only with current accepted local heating permission. Otherwise `off`. **Use:** Use the thermal blueprint to start/stop the physical heater. Missing permission must select the configured stop action.

**Measured heating**

Binary `on` when accepted measured power is at least half the configured heater rating. `off` below that threshold. Unknown without usable measurements. **Use:** Compare actual heater activity with the request. It is a power-based indication, not proof of a completed heating programme.


Thermal numeric sensors carry `plan_id`, `configuration_version` and `issue`
attributes. They identify the retained plan, current configuration and app-reported
readiness problem. Their presence does not make an unknown prediction usable.

Permission is withdrawn for missing ownership or local evidence, a plan/configuration
mismatch, disabled thermal planning or temperature at the configured maximum.
The **Thermal planning enabled** switch and **Minimum water temperature** number
change planning intent. Neither physically switches the heater.
Use the [thermal execution blueprint and safeguards](../development/THERMAL-STORAGE.md#home-assistant-execution),
retaining the thermostat, over-temperature protection and hygiene programme.

## EV charging: vehicles

Vehicle intent and measurements belong to the car. Physical charging permission
belongs to the charge point. These are separate virtual devices.

**State of charge**

Accepted current car measurement in percent, or unknown without usable evidence. **Use:** Compare actual SoC with the requested target. It is not the desired SoC setting.

**Usable battery capacity**

Configured usable capacity in kWh. **Use:** Interpret required battery energy. It is a rating, not current stored energy or a cumulative meter.

**Maximum charging power**

Configured vehicle charging limit in W. **Use:** Interpret the car-side ceiling. The charge point has its own limit too.

**Next departure**

Next resolved departure timestamp, using the installation's HA timezone for daily/one-off intent. **Use:** Understand the charging deadline. It is not a start time.


The separate **Target state of charge**, daily departure and one-off departure
controls change saved intent. See [Prepare EV charging](appliances.md#prepare-ev-charging).

## EV charging: charge points

**Requested charging power**

Current guarded charging target in nonnegative W. Returns `0` when no command is authorized while the entity itself remains available. **Use:** Apply through the EV blueprint according to capability. Zero withdraws charging demand, not proof of physical stop.

**Charging requested**

Binary `on` only when a guarded target is greater than zero. Otherwise `off`. **Use:** Drive the supported start/stop permission flow together with the requested watts. It is not measured charging activity.

**Measured charging power**

Accepted local charge-point measurement in W, or unknown without usable evidence. **Use:** Verify actual intake and compare it with requested power.

**Planning readiness**

The states below. **Use:** Explain the app's readiness decision. Also check current **Charging requested**, which applies local guards.

**Predicted energy shortfall**

Scheduled `shortfall_kwh`, in kWh, or unknown without a schedule. **Use:** See how much required battery energy is predicted to remain unmet at departure. It is not a physical power target.

**Predicted departure state of charge**

Scheduled SoC at departure in percent, or unknown without a schedule. **Use:** Compare the predicted outcome with the vehicle's target.

**Maximum charging power**

Configured charge-point ceiling in W. **Use:** Combine with the vehicle limit when interpreting the target.

**Minimum charging power**

Configured minimum active charging power in W. **Use:** Interpret the nonzero operating range. It does not require charging when permission is off.

**Control capability**

`read_only`: measurement only. `on_off`: start/stop control. `variable`: variable-power control within configured limits. **Use:** Select actions that the configured equipment actually supports. This field grants no permission by itself.


| Planning readiness | Meaning |
|---|---|
| `energy_source_unavailable` | The required Energy source is unavailable. |
| `telemetry_unavailable` | Current telemetry needed for planning is unavailable. |
| `disconnected` | The charge point reports no connected vehicle. |
| `connection_unknown` | Connection cannot be established reliably. |
| `vehicle_unknown` | No vehicle is identified for this connection session. |
| `measurement_only` | The charge point is configured read-only. |
| `ready` | The app has the required planning evidence. This alone does not mean charging is requested. |
| `soc_or_limits_unavailable` | Required vehicle SoC or charging-limit evidence is unavailable. |

EV sensors remain available while their device exists and the coordinator is not
closed. Missing measurements or schedules are therefore commonly `unknown`, while
withdrawn command permission produces `0 W` and **Charging requested** off.
Predictions and app readiness can still describe a retained schedule after current
permission is withdrawn. Never use them as a substitute for current permission.

The local guards require a plugged-in connection, matching vehicle/session
identity, usable SoC below target and a supported power limit. A disconnect,
reached target, missing evidence or invalid plan withdraws permission. Select the
**Connected vehicle** for each new session through the supported control or a
reliable identification automation. A previous session's assignment is insufficient.
Use the [EV execution blueprint](../development/EV-CHARGING.md) to translate the
current request to physical charger actions. This model has no V2G discharge.
