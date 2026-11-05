# Virtual device and entity contract

All command entities are read-only proposals from the app. User-owned Home Assistant automations translate them to physical equipment. The app's wizard owns capabilities and limits. This integration has no separate device-setting wizard.

| Virtual device | Entity | Meaning |
| --- | --- | --- |
| Open Energy Planner | Enabled switch | Desired EMS enablement. Pause remains explicit until switched on. |
| Open Energy Planner | Status sensor | Actual operational state. Separate from enable intent. |
| Grid | Control mode | `inactive` or `zero_grid`, selected by the app. |
| Grid | Power target | Active zero-grid means numeric `0 W`. Inactive means `unknown`, `applicable: false`. Arbitrary targets are unsupported. |
| Each battery | Control mode | `fixed_power` or `follow_grid_target`. Enum options appear in HA. |
| Each battery | Power target | Signed whole DC watts in fixed mode: positive discharge, negative charge, zero real idle. Following means `unknown`, `applicable: false`. The entity stays enabled. |
| Controllable solar source | Enabled target | Read-only `on`/`off` enum, only when on/off permission is configured. Fixed production has no command entity. |

Without a usable command, on expiry or explicit pause, command entities are `unavailable`. An inapplicable numeric value is never replaced by a stale previous target or a misleading zero. Measurements are separate from command entities. Changes to numeric targets do not recreate devices/entities. Capability removal withdraws its entity and creates an action-required notification about potentially affected automations. A configured battery exposes both its mode and power sensor under the same stable device.

## Zero-grid selection

The wizard asks **Can follow zero grid target** for each battery. Enable only when the user's physical automation/controller can implement it, whether on the battery, its hybrid inverter or another local controller. This is permission/capability, not an always-on strategy and not a claim that Energy metadata reveals physical control support.

The current app enables following only for permitted batteries in a validated optimal/feasible interval with both expected grid import and export within `1e-7 kWh` of zero. This is an execution choice preserving the independently validated battery flows, losses, SoC limits and economic outcome. Nonzero grid charging/export remains fixed-power control. A zero net total created by simultaneous import/export is not sufficient. The grid owns the authoritative goal. There is no separate battery grid setpoint, designated physical executor or cell-balancing mode. Other batteries can retain fixed commands. Fast fluctuations, controller coordination and physical protection stay in HA/local equipment. Subsequent measured SoC informs replanning.

This bounded policy does not claim a general robust-control optimization or guarantee physically exact zero. Broader following decisions at binding physical limits remain product work. Both independent numerical and actual-HA transition tests protect the supported behavior.

## Settings and links

Devices open the app-owned configuration UI, using its ingress/explicit UI address rather than the restricted TLS synchronization listener. Power entities use HA's power device class, W and zero display decimals. Modes/solar proposals use enums. No command sensor is treated as a cumulative Energy meter or given an energy statistic class. The status connection-type attribute is diagnostic evidence, not a control setting.

## Home Assistant planning groups

On Home Assistant 2026.10 or newer, one app connection owns native configuration
subentries for Solar, Batteries, Consumption and cycles, Electric boilers and
EV charging. General EMS and Grid devices remain directly under the connection.
A group appears only when it has configured virtual devices. Multiple devices
share a group. These are configuration groups, not hub devices, physical topology,
areas or labels.

The app's installation and appliance roles determine membership. Display names
and weather sensitivity do not change the group. Forecast-only heat pumps and
air conditioners remain Consumption and cycles. All platforms for one virtual
device share its subentry. The native entity-platform registration receives the
subentry ID. Existing device associations move in place, preserving device IDs,
entity unique IDs, entity IDs, user names, areas, disabled settings and labels.

Configure membership in the app, not through a second HA editor. Catalogue
withdrawal uses the existing entity/device cleanup. A group is retired only after
its role is no longer configured and neither registry contains members. A
connection outage retains the last accepted catalogue and its groups. Unloading
or reloading the integration does not remove its groups.

HA's native Delete action on a subentry deletes its virtual registrations.
It is not an ungrouping action. The integration automatically reloads once when
a deleted group is still configured in the app, recreating its groups and devices
through normal platform setup. If the app is unreachable, recovery completes when
synchronization resumes. Remove devices in the app to withdraw them permanently.
Preservation of manually deleted registry metadata is not guaranteed.

## Device display names

The HA integration uses the linked HA device name as the default solar/battery
device name, honoring that physical device's `name_by_user` before its default
`name`. A battery therefore uses `Test battery`, rather than the directional
sensor label `Test battery discharge`. Energy sources can also be entities or
imported statistics without an associated HA device. Only for those sources,
use an explicit Energy source name, then the sensor friendly name, entity-registry
metadata when no state exists, a supplied readable name, and finally `Solar` or
`Battery`. Technical model IDs are never used as a default label.
Naming belongs to HA presentation and does not change the app's model IDs.

Users can rename a virtual device using HA's existing device edit button. The
integration updates only the registry's default `name`, never `name_by_user`.
Source renames therefore update defaults while preserving user overrides.
Device registry IDs, entity unique IDs and existing entity IDs remain unchanged.
A display-name change does not recreate devices or break their automation links.
