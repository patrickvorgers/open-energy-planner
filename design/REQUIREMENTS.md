# Requirements: Open Energy Planner

Version: 0.147

Updated: 5 November 2026

Status: the functional first-release scope is agreed. Technical implementation and release acceptance remain incomplete. The solver experiment, minimal app/HA chain and first validated synthetic planning round exist. Development experiments do not control production hardware.

The user requested a private `patrickvorgers/open-energy-planner` repository before more feature work. See [development foundation](DEVELOPMENT-FOUNDATION.md). The agreed public-source product release remains future work. All maintained documentation, directory/file names, code, tests and UI text must be English.

This document records decisions from the requirements discussion. An assistant proposal is not automatically an accepted requirement. Later explicit user corrections take precedence. The [technical design](TECHNICAL-DESIGN.md) develops these decisions into an implementation, without turning proposals into requirements. The [first-release scope](FIRST-RELEASE-SCOPE.md) defines inclusions, exclusions and release gates. Implementation choices within those boundaries do not need another product interview. Changes to behaviour, scope or user obligations require an explicit decision. A completed experiment or milestone is not a completed product release.

Confirmed project name: **Open Energy Planner**. Confirmed subtitle: **Local energy optimization for Home Assistant.** A public web/GitHub search on 18 October 2026 found no software project with this exact name. This does not establish exclusivity, domain availability or trademark clearance. YAEMS was a brainstorming suggestion only.

Implementation update, 23 October 2026: the [settlement and savings increment](../docs/development/SAVINGS.md) adds immutable required metering, published-price settlement, independent continuous self-consumption states/counters, versioned corrections, inventory movements and a responsive read-only plan/history/benefit overview. Original plans remain unchanged. A simulated reference and estimated reconstruction remain explicitly labelled. This does not establish production execution or causal savings.

## 1. Status and terminology

Confirmed automatic published-price increment: prefer the compatible configured official
Home Assistant Nord Pool integration, otherwise use direct Nord Pool retrieval. Use
HA publication availability before requesting tomorrow and display the selected
route and reason. Keep the HA exchange source-independent, supporting only Nord
Pool initially. See [automatic route and timing](../docs/development/PRICE-FORECAST-SELECTION.md#automatic-published-price-route).

Confirmed third price-forecast source: add Jeroen.nl for Dutch market prices.
Keep the executable horizon unchanged. Compare all source pairs using original
matched forecasts and published truth. Continue the existing daily thresholds
and streak policy, including the 48–72-hour valuation bucket where covered.
Support Automatic or a manual source preference per lead bucket. Manual use must
not stop quality collection or override published prices. Missing preferred
coverage falls back immediately. Show actual source, manual preference, independent
automatic recommendation, fallback, issue/receipt times and pair-specific MAE
with sample coverage. Persist settings and evidence in the active database,
preserve old plans and source history, and use the existing shared save controls.
See [price forecast selection](../docs/development/PRICE-FORECAST-SELECTION.md).

Confirmed price-source increment: add the EpexPredictor public API alongside EnergyPriceForecast EU. Every published price remains authoritative. Select forecasts per bidding area and 24-hour lead bucket using matched original forecast MAE over the last 30 completed days, requiring seven settled days/168 matched hours. Switch only for at least 10% and 0.1 ct/kWh improvement on three consecutive daily assessments. Initially prefer EnergyPriceForecast. Use unexpired alternatives immediately when preferred coverage is missing. Prices through 72h serve post-48h battery valuation only. Without later prices retain the trailing-24h proxy. Show actual source, preference, reason and assessment evidence in the app. Keep selection in the price boundary and preserve original plans/evidence. See the [implementation contract](../docs/development/PRICE-FORECAST-SELECTION.md).

- **Confirmed:** explicitly chosen by the user.
- **Open:** requires a decision, specification or technical verification.
- **Research:** a finding about another product, not a product choice or performance guarantee.
- **Superseded:** replaced by a later decision.

A virtual device describes capabilities, source measurements, limits, relationships and desired commands. The HA integration exposes an HA device with appropriate entities. Not every entity must be a sensor. A plan covers a future period. The integration publishes the desired state now. Replanning frequency, planning horizon and maximum plan age are distinct.

## 2. Goals and principles: confirmed

- GOAL-01: Support `minimize cost` (minimum expected net electricity cost) and `minimize consumption` (minimum grid import), respecting every configured constraint.
- GOAL-02: Include production, storage, dynamic prices, contractual rewards, losses and configurable battery wear. Maximum self-consumption is not a separate primary objective under cost minimization.
- GOAL-03: Support multiple users and countries, rather than only the author's installation or the Netherlands.
- GOAL-04: Minimal configuration is a firm design constraint. Reuse available HA information and request only necessary missing information.
- GOAL-05: PV and batteries are the first application, both optional. The model must allow later flexible loads.
- GOAL-06: The EMS retains control. Contractual bonuses do not delegate trading or device control to suppliers.
- GOAL-07: Configuration, storage and planning run locally. Fetching external weather, market prices and price forecasts is permitted.
- GOAL-08: The first release supports one grid connection per installation, with optional multiple PV systems and batteries planned together within connection and route limits. Neither PV nor a battery is mandatory. Multiple grid connections are outside the first scope.
- GOAL-09: Cost minimization may curtail controllable PV and charge from the grid when effective prices after taxes/rewards make this advantageous. Consumption minimization does not prioritize extra imports solely for price profit. Both strategies are in scope.
- GOAL-10: Under consumption minimization, after mandatory reserve recovery, minimize gross imported kWh plus per-battery terminal stock replacement: `max(0, initial_internal_kwh - final_internal_kwh) / grid_to_internal_charge_efficiency`. Include cell and configured converter losses exactly once. Export does not offset import. Surplus stock earns no negative-import credit and cannot mask another battery's deficit. Then minimize economic costs, including prices/bonuses, among equivalent corrected-import plans. Do not use a weighted objective that trades extra primary kWh for profit. Numerical tolerances address arithmetic precision only. Keep actual planned import, hypothetical replacement, primary optimum/quality and financial results distinct. Replacement is accounting, not a required physical refill or measured consumption.
- GOAL-11: Default initial setup to `minimize cost`. Preserve a saved strategy across restart/update. Consumption minimization is an explicit alternative.
- GOAL-12: A user may change strategy. Activate it for commands only with a fully calculated, validated, received and accepted replacement plan. Distinguish requested and active strategy while pending. The old plan retains only its normal validity. Failed calculation does not extend it. Do not reset the reference, battery states, counters or history.
- GOAL-13: Store and display each plan's actual strategy with current/historical plans, transfer and archive metadata. Do not infer an old strategy from current settings or alter saved metadata after a change.
- GOAL-14: Strategy is a persistent configuration choice, not a dynamic HA command. Do not provide a strategy select entity, automation action or automatic switching. Manual changes in the EMS UI follow GOAL-12. Active strategy may be exposed as status.

Terminal-inventory policy is the conservative grid-replacement correction in GOAL-10. Rolling replanning alone does not eliminate end effects. The correction assumes no free post-horizon solar refill. It may conserve more starting stock than hindsight requires, without forecasting beyond available inputs or forcing a final SoC target. Strategy and savings-reference policy remain separate. The fixed self-consumption reference never changes with strategy.

## 3. Architecture and responsibilities: confirmed

| Component | Responsibility |
|---|---|
| EMS app/calculation module | Authoritative device configuration, models, relationships, contract rules, forecasting, planning and command-validity decisions |
| Thin HA integration | HA interface, virtual device/entity lifecycle, measurement forwarding, plan reception and current desired-state/availability publication |
| User-built HA automation | Translate desired state/control mode to physical actions. Handle release and fallback |
| HA and local physical control | Device knowledge, fast control, limiting and physical protection |

- ARCH-01: No brand/protocol-specific physical control in the planner.
- ARCH-02: Forecasting and optimization are separate, independently improvable modules.
- ARCH-03: Market and price-forecast sources use interchangeable internal adapters. Ordinary setup does not require users to select/configure them.
- ARCH-04: No required MQTT dependency. Communicate directly and locally between app and integration.
- ARCH-05: The app owns configuration and the complete setup/editing wizard. The integration has only its one-time app connection, an app-UI link and read-only HA metadata/history adapters. It contains no installation/source/device wizard or optimization. After that one-time installation, it automatically creates, updates and removes virtual devices/entities from the app-owned capability catalogue. Install the integration once through HACS. The app does not install or update integration files itself and requires no writable HA configuration mount. On HA OS, Supervisor automatically authenticates the connection without a pairing confirmation. HA ingress supplies browser authentication. Normal onboarding asks for no verification code, address, token or extra app login. Standalone containers retain explicit pairing consent because they have no Supervisor trust channel. All HA configuration, metadata, measurements and history exchange runs through this integration, not a second app-to-HA client or direct Recorder access. Expose relevant confirmed grid, solar, battery and inverter properties as separate read-only sensors with explicit units, independently of current commands. These describe settings, not measurements.
- ARCH-06: Support an EMS app/container on HA OS and the same calculation module as a separate container for HA Container users. Both use the HA integration.
- ARCH-07: Specify the local API/connection technically. DIST-03 defines joint releases and equal versions for new plan exchange.
- ARCH-08: After one-time HACS installation, HA OS automatically connects the app/integration using the Supervisor-authenticated bootstrap. No verification-code comparison or approval click is required. Credentials are generated and retained internally. HA ingress uses the existing HA login. All HA data exchange still runs through the integration. Local mDNS alone grants no authorization. Standalone containers without Supervisor retain one explicit app confirmation and an advanced manual networking fallback. Persist the HA credential and app certificate identity, reject silent identity replacement and recover the same Supervisor-owned connection automatically after restart or credential loss. Never publish credentials in discovery records or URLs. The default persistent Docker developer environment must exercise actual Supervisor bootstrap and ingress. Its real-Supervisor acceptance is automated. Personal VM deployment is not required, and hardware/host-kernel coverage remains separate.
- ARCH-09: Use HA interfaces/APIs for configuration, current measurements and available history. No direct SQL connection to HA's database. The integration bridges required HA information and manages virtual devices. The EMS database is separate from Recorder.
- ARCH-10: Enforce typed interfaces and allowed dependencies. HA objects, provider formats, ORM objects/sessions and solver types stay within their adapters. Core calculations know no concrete database, transport or physical device. Use cases coordinate input/calculation/storage. Test import boundaries and contracts.
- ARCH-11: Extend modules/capabilities without unnecessary coupling. Prefer composition and small replaceable interfaces. Inheritance requires a genuine is-a relationship and preserved contract. No deep universal device hierarchy is required. Specify Python organization in the design.
- ARCH-12: Separate each device's technical configuration, current state, mathematical contribution and HA projection. Use one generic model per supported type with multiple configured instances, no brand classes. Compose through explicit routes/shared limits. Solver objects remain inside the adapter. This adds no physical drivers or release scope.
- ARCH-13: Use the same physical/economic definitions and device-model classes for optimized and self-consumption scenarios. One `EnergyModelFactory` in the highspy adapter creates separate instances with independent state, variables and counters. Reference policy fixes its actions. Optimization chooses within the same applicable physical limits. No mutable model/results sharing. Independent validation/cost evaluation remains mandatory.
- ARCH-14: Split the highspy adapter into focused classes for devices, routes/inverters, contracts, objectives, assembly, solving and extraction. Use narrow Protocols/composition, no deep inheritance or monolithic solver. Configuration, operational state and bindings are different types. Native bindings/state are isolated per instance and scenario. Another supported device needs configuration, not a copied class. A new type needs its model and scenarios.

## 4. Setup and HA data: confirmed

- CONFIG-01: Guided configuration per virtual device type.
- CONFIG-12: Every editable app configuration page has one shared floating save bar, visible while scrolling on desktop, mobile and HA ingress. Show saved, unsaved, saving and failed-save feedback beside explicit Save and Discard actions. Disable redundant saves. Retain edits on failed writes and version conflicts. Lock inputs while saving or reloading. A read-only evidence refresh must not discard pending edits. Appliance shifting permission is staged configuration. Readiness, cancellation and other explicit operational actions retain their own immediate buttons.
- CONFIG-02: A complete electricity configuration in the connected Home Assistant Energy dashboard is a prerequisite. Reuse all validated grid, solar and battery energy channels. Report missing/incomplete configuration with instructions to correct it in HA. Do not offer a parallel manual Energy setup. The Energy dashboard exclusively owns solar/battery membership: adding, removing or reassigning an Energy source is not available in the app. Reflect source changes automatically, preserve existing device identities/settings where matched, and require only missing physical properties. Block publication against a different household balance or device set. Retain historical plans/settings.
- CONFIG-03: Adapt the app wizard to available HA data and history. Do not ask for already known values. Show detected sources and provenance with an explicit review/edit action. Sensor selectors are dropdowns from the connected HA catalogue, filtered by quantity, device class and supported units. Never require typing entity IDs. Explain every requested field and its expected unit/value.
- CONFIG-04: Energy configuration remains authoritative for existing measurement links. Reuse every configured grid import/export pair and both cumulative battery charge/discharge meters. Reuse the battery's configured SoC, normalized power and usable capacity in kWh. Aggregate all normalized grid power channels. Never silently select just one connection. These links, directions and capacity are read-only in OEP, enforced by the API as well as the UI. Follow valid Energy changes automatically while retaining original definitions/plans. Report missing required Energy settings with instructions to correct Settings > Dashboards > Energy. History availability is a separate readiness condition. Validate units/completeness before following later changes.
- CONFIG-05: Explicitly report unusable/missing required links.
- CONFIG-06: No user templates/helpers are required to return standard measurements to the planner.
- CONFIG-07: Normalize supported units and signs. Ask only where HA information is insufficient.
- CONFIG-08: Reuse relevant HA information, including history/location. Access does not guarantee existence or unambiguous meaning.
- CONFIG-09: Fetch weather from HA location without a required extra weather integration/API key. Disclose this during setup and permit disabling it.
- CONFIG-11: Keep initial onboarding short. Reuse Energy-owned sensors and capacity without another question. Direct missing SoC/actual power/capacity settings back to HA Energy. Ask only for OEP-owned physical limits and relationships. Add advanced settings later through the same model. The integration provides the HA interface. Energy owns source membership and links under CONFIG-04. Simplicity must not silently omit mandatory device limits.
- CONFIG-10: Configure only the EMS and virtual model in the wizard. No final checklist of physical automations or per-device automation examples. The EMS cannot know how the user applies the model to control, dashboards or other uses. The interface meaning remains documented. Physical execution/workflow is not prescribed.

## 5. Device model and relationships: confirmed

- MODEL-01: Model consumption, production and combinations. Storage adds energy content, capacity and losses. The grid is not storage.
- MODEL-02: Describe supported controls explicitly. Measurement/forecast-only devices are allowed.
- MODEL-03: Multiple instances and relationships, including shared power limits.
- MODEL-04: Explicit AC/DC energy routes and route conversion losses.
- MODEL-05: Separate inverter components with PV/battery connections.
- MODEL-06: Offer simple installation choices, such as a battery with its own inverter or PV/battery on a hybrid inverter. Derive routes/limits.
- MODEL-07: No universal DC efficiency benefit. Use actual configured route properties.
- MODEL-08: Separate from energy routes, a control relationship lets a battery follow its grid connection's target. Configure capability with a simple question such as “Can this battery participate in grid control?”. This identifies the goal, not a physical controller/protocol. HA automation decides execution.

### 5.1 PV

- PV-01: Support fixed production, on/off control and configured power limitation.
- PV-02: Technical capability and permission to use it are different.
- PV-03: Curtailment permission is configurable per installation.
- PV-04: Decide curtailment on total cost, including own demand, storage space and bonuses. A negative market/export price alone is insufficient.

### 5.2 Batteries

- BAT-01: First-release batteries support adjustable power. Physical translation belongs to user automation.
- BAT-02: Current measured SoC and battery power are both required for a configured battery.
- BAT-03: Whole-watt commands follow HA convention: positive discharge, negative charge, zero no planned exchange. Apply consistently to normalized measurements, results, entities, charts/examples/tests. This supersedes the reverse convention. Translate physical source signs at ingestion and physical command signs in user automation.
- BAT-04: Desired and actual power are separate.
- BAT-05: Per-battery minimum SoC is a hard planning limit. If already below it, block further planned discharge but retain the measured initial state.
- BAT-06: Mandatory reserve recovery is configurable. When enabled, restore minimum SoC as soon as possible within battery/inverter/grid limits, ahead of both economic and import objectives. Then resume the selected strategy. Without mandatory recovery, the strategy chooses timing. Neither mode permits further discharge below reserve or replaces physical safety.
- BAT-07: Include losses. Configurable wear cost per internal kWh discharged defaults to zero. Apply a positive setting consistently to planning and both savings scenarios. Savings include the difference in wear costs. Show nonzero wear separately as a user-valued estimate. Do not infer it from purchase cost or claim measured/total aging. Unused batteries also age. Stored energy's decision value includes future use, not just historic acquisition cost.
- BAT-12: Multiply internal discharged energy by the configured per-kWh wear rate. Do not divide the rate by capacity or charge the same energy again on charging. Capacity and minimum SoC do not change the rate. Convert battery-terminal discharge through cell discharge efficiency to the internal energy basis. Convert measured AC discharge through inverter, DC and cell discharge efficiencies before applying the same rate. Partial discharge is proportional. This is a linear cost model, not a physical-aging or manufacturer-warranty equivalence claim.
- BAT-08: A configured battery is core equipment. Individually excluding it temporarily is not normal battery operation. Individual exclusions concern additional devices.
- BAT-09: Manual battery takeover pauses the whole EMS. Physical release is handled by automation.
- BAT-10: Replan from measured SoC deviations. Replanning is not safety or fast regulation.
- BAT-11: Treat battery export as available within device/grid/contract limits, without a separate self-consumption-only preference switch. This does not assert every contract permits export.

### 5.3 Grid connection and control

- GRID-01: Separate hard total import and export limits.
- GRID-02: No mandatory per-phase allocation/monitoring in the first planner. HA/physical execution handles fast protection.
- GRID-03: The only dynamic grid target is zero W. Do not support arbitrary import/export targets such as 500 W. The planner chooses whether zero-grid control is active. Price-driven battery charging/discharging uses fixed battery power.
- GRID-04: Publish zero-grid control on the virtual grid device. Users choose the physical battery, hybrid inverter or other controller in HA. The EMS prescribes neither executor nor number of local controllers. Relationships/expected flows inform planning, not a managed physical protocol binding.
- GRID-05: The app chooses fixed power or grid following for each participating battery. Avoid conflicting active commands.
- GRID-06: Battery modes are “Fixed power” and “Follow grid target”. The former uses one signed BAT-03 target. The latter follows the active connection target through the control relationship. The grid owns the authoritative zero-grid command. Follower mode is derived, not an independent second command. Non-applicable fixed power follows GRID-13.
- GRID-07: Model expected battery use while following. Fast corrections remain local.
- GRID-08: Zero battery power and zero grid target have different meanings.
- GRID-09: Distinguish an inactive target from an active zero target.
- GRID-10: Zero-grid control is a supported mode, not the objective itself. Cost minimization may select grid charging/export. Consumption minimization must fit minimum imports. Respect all limits. Physically exact zero is not guaranteed.
- GRID-11: With multiple batteries, the target remains connection-level. Planned modes/flows do not prescribe which physical battery/inverter an automation configures. HA coordinates physical controllers. Requiring one designated local controller was rejected.
- GRID-12: Positive grid power means import, negative means export. Apply to normalized measurements, expected flows and presentation. This does not add arbitrary targets: only active zero is supported. Translate source signs at ingestion. Automation performs physical execution.
- GRID-13: Keep the fixed-power sensor enabled while following the grid. Its numeric value is `None`/HA `unknown`, with `follow_grid_target` explaining it and `applicable: false` in the contract. Zero means real fixed idle. `unavailable` means no usable command or explicit pause. Do not retain an old target or operationally disable the entity in the registry. Automations use numeric fixed targets only in `fixed_power`. The app UI may show “Not applicable”. Measured power stays separate.

Use the agreed modes/signs consistently. Do not name grid following as if it were battery-cell balancing. Coherent publication remains subject to technical validation.

## 6. Entities and configuration changes: confirmed

- ENTITY-01: Automatically create virtual devices/entities. No manual helpers for offered commands.
- ENTITY-02: Entity availability follows capabilities. Non-controllable PV has no on/off command entity.
- ENTITY-03: Removing capability differs from temporarily withholding permission: the former may remove entities, the latter changes behaviour/status.
- ENTITY-04: Before capability removal, show known affected automations and require confirmation before entities disappear.
- ENTITY-05: Synchronize app changes. Dynamically add/update required devices/entities.
- ENTITY-06: Preserve identities across normal configuration/name changes to avoid unnecessary breakage.
- ENTITY-07: Desired state does not prove realization.
- ENTITY-08: Users decide physical response through automations. No mandatory observation-only phase before publishing commands.
- ENTITY-09: Provide one integration-created HA switch for desired EMS enablement. Off explicitly pauses under FAILURE-07. On requests activation/resume under the appropriate rules. It works in dashboards/automations and does not imply active commands.
- ENTITY-11: Persist enable intent. Explicit pause survives ordinary app/integration/HA restarts. Only an explicit user/automation enable ends it. Valid plans, reconnection or recovery do not resume it automatically.
- ENTITY-12: On first completed setup, desired enablement defaults on. Publish normally accepted valid commands so users can inspect virtual plans before linking hardware automations. No initial pause/observation requirement. Until ready, commands remain `unavailable` with a reason. Only automation controls hardware, and this default never overrides a previously saved pause.
- ENTITY-10: Expose actual status separately: at least active, explicitly paused and not ready. Explain missing data, pending validation and other blockers. Do not silently turn a fault into explicit pause. Specify exact codes/reasons in the entity contract.

Open: incompatible meaning/unit migrations. Reference detection cannot guarantee every dynamic template or external consumer is found.

## 7. Forecasting and learning

### 7.1 Confirmed principles

- FORE-01: Work with little/no history using a simple user-confirmed demand profile.
- FORE-15: Scale the startup daily shape using a 365-day cumulative-sum difference of the Energy household balance when available. Use matching start/end references for all channels. Do not require intermediate daily rows to calculate that total. With a shorter common period, extrapolate its total using the actual elapsed days, requiring at least seven days. Show the coverage and distinguish measured annual consumption from extrapolation with possible seasonal bias. Reuse a saved estimate or ask for one kWh/year estimate only when usable history is insufficient. Include direct PV/battery supply to the home. Exclude battery charging and do not equate household demand with grid import. Mark provenance/provisional quality and improve with measured data. Specify the daily/hourly distribution during implementation.
- FORE-02: Automatically build/improve demand forecasts in a separate local module. No mandatory external demand-forecast service.
- FORE-03: Reuse usable HA history. Missing is unknown, never zero.
- FORE-04: Household, residual and individual-consumer model selection uses preserved matching local-day forecast accuracy. Retain a suitable baseline until a challenger demonstrates consistent improvement on complete comparable days. Automatic solar source selection retains its physical eligibility and forward-accuracy policy under FORE-17. Forecast accuracy and financial decision value remain separate quantities.
- FORE-05: Use chronological evaluation without future leakage. Hold planner, limits and other inputs fixed.
- FORE-06: Use the same Home Assistant local-day comparison principle for energy forecasts. Preserve original predictions, assess matching measured intervals and report energy volume and timing errors. Controlled operation cannot establish spontaneous-use accuracy. A lower forecast error does not prove a lower financial cost.
- FORE-07: Archive PV/demand forecasts from installation as received/used, separately from realization. Keep original provider forecasts and local corrections.
- FORE-08: Distinguish receipt time from provider issue time. Verify the latter's availability per source.
- FORE-09: Mark missing data and curtailment. Curtailed PV must not be learned blindly as available production.
- FORE-10: Automate fetching, normalization and model selection. No required complex panel/ML input.

### Individual consumption devices: confirmed next increment

This extends MODEL-02 and FORE-02–07. It records agreed behaviour, not implemented functionality or automatic inclusion in the first release. Individual device membership, measurement links and upstream relationships come from the HA Energy dashboard. The app owns forecast/planning settings. It does not offer a competing device catalogue. Energy-device history can train local models, but a listed device is not necessarily controllable or sufficiently measured.

OEP centralizes configuration of consumption devices, thermal stores, vehicles and charge points under Settings > Appliances. Its shared category navigation opens the existing type-specific editors for Consumption and cycles, Electric boilers and EV charging. Configure source bindings, forecasting membership, characteristics, model assumptions and hard limits there. Retain a single visible save state per editor. Changing categories must not discard dirty settings or interrupt a write. Source registration and upstream relationships remain owned by Home Assistant Energy. Removing a device from OEP retains its historical evidence and is unavailable while its cycle is unresolved.

Energy pages contain monitoring, original plans, execution evidence and explicit daily requests. Energy > Appliances contains readiness, deadlines, schedules and cancellation. Energy > Thermal storage contains measured and predicted heat evidence, comfort requests and planning pause/resume. Energy > EV charging contains sessions, target SoC, daily departure time and one-off departure requests. These operational requests use the existing app intent endpoints and helper-free HA controls. They do not replace device configuration or directly actuate hardware. Device forecasts provides read-only analysis.

New device types reuse this settings-versus-operation boundary, common navigation and SaveBar safeguards. Add only the fields and calculations required by the supported physical model. Do not force thermal storage, complete-cycle devices and EV charging into one solver profile or invent an extensible form engine. Weather-sensitive forecasting alone never establishes temperature-control capability.

Keep the existing appliance category navigation and configuration routes. Every category retains device selection, Add, Edit, removal and the shared Save/Cancel safeguards. Boiler and EV editors show saved configuration as a summary and reveal inputs only after Edit. EV addition explicitly distinguishes vehicles and charge points. Cancel restores the original appliance and confirmed configuration, including after a removal or Add draft. Preserve existing advanced fields, model assumptions, APIs, IDs and archives. Treat identity/source, behavior, forecast method, control capability, planning permission and daily intent as independent dimensions. Household/residual demand remains an aggregation boundary, not another appliance category. Average cycle profiles and their measured cycle evidence belong to the operational appliance detail under Energy. A future climate model needs its own physics and comfort constraints. See [shared appliance structure](../docs/development/APPLIANCE-ARCHITECTURE.md).

The default is forecast-only. Users explicitly permit shifting per device. Forecast-only devices contribute expected consumption without command entities. Weather-sensitive devices, such as heat pumps and air conditioners, may use weather and calendar features. Forecasting their normal consumption does not authorize thermal control or establish comfort constraints.

For cycle-based appliances, learn one average cycle profile per device: energy, duration and power over elapsed cycle time. Programme recognition and separate programme profiles are not required. Use measured completed cycles. Preserve measurement resolution, missing-data status and sample/quality evidence. Never present estimated subhour shape from coarse totals as measured cycle detail. Without usable evidence, report that the profile is not ready rather than silently inventing a learned profile. Model improvement remains subject to chronological comparable evaluation under FORE-04–06.

Reuse the Energy dashboard's device energy and optional power sensor for automatic cycle detection. Do not require manual start/end times or user detection thresholds. Missing power links point to Home Assistant Energy. Insufficient history remains explicit. A user may enter only duration and total kWh as a manual fallback. Label that estimate separately from learned measurements, retain it after learning becomes ready, and preserve the profile frozen into accepted tasks. Bootstrap test data must exercise the same detector with labelled simulated histories, not bypass it with learned profiles. See [automatic learning](../docs/development/APPLIANCE-LEARNING.md).

Use all available paired hourly Recorder history as a separate provisional cycle
estimate. Process the initial backlog in the background with durable bounded
checkpoints and honest progress. Automatically admit a provisional planning
profile after initial completion and two usable hourly runs, unless an explicit
manual fallback or ready fine model takes precedence. Use a 90-day recency
half-life for both automatic models. Keep raw counts, weights, usable coverage
and estimated versus fine measured evidence visible per appliance. Hourly energy
may be spread uniformly into estimated planning quarters, retaining occupied-hour
duration, without claiming recovered subhour measurements. Accepted tasks remain
frozen. Historical estimates never grant shift permission or readiness.

Historical usage timing can inform forecast-only consumption, but never creates a runnable task. For a shiftable device, a user-owned HA automation submits readiness and an absolute finish-before deadline through a device-targeted integration action, without extra helpers. OEP chooses the start time using the average cycle profile within that window. Report an infeasible request explicitly. Do not silently discard the task or relax its deadline. Once started, an active appliance task retains its actual start and remaining cycle during replanning. Explicit user cancellation removes that remaining work even if the physical appliance was running. The user automation owns any physical stop or queue cancellation.

Explicit execution feedback through the integration is mandatory for shiftable devices, without extra helpers. This supersedes optional feedback with app-side power detection as the execution authority. The user-owned HA automation reports device, task and start-attempt identity, event time and one of: started, not started or completed. It may derive these reports from reliable appliance state or sustained measured power. Sending a start command is not evidence that execution started. Low/unknown power alone is not evidence of failure or completion.

Started confirms actual execution: record the actual start, prevent duplicate starts and use the remaining cycle profile in later plans. Not started confirms both that execution did not begin and that this attempt cannot execute later: the automation must cancel or invalidate queued/delayed actions before reporting it. If that guarantee cannot be established, execution remains unknown. A confirmed failed attempt closes only that attempt. The same open task can be scheduled again in a later planning round while its deadline remains feasible. Each replacement attempt has a new identity. Old commands cannot authorize it. If the deadline is no longer feasible, show Deadline not achievable and issue no start command for that task. Unrelated planning continues.

Completed closes the task. Automatic profile learning uses independently detected power/energy history, subject to evidence quality. Detection never substitutes for execution feedback. No feedback within the confirmation window expires the start request and exposes Start not confirmed with an actionable notification. Execution is unknown, never assumed not started. Do not automatically issue another start attempt until explicit feedback resolves the uncertainty. A user may instead explicitly cancel planning at any stage. This closes the task without asserting physical nonexecution, and any later readiness is a separate deliberate submission. Command expiry is not proof that a physical or queued action was cancelled. Missing completion feedback likewise cannot authorize a new cycle. Confirmation windows must expire no later than the latest feasible start, with an explicit allowance for cycle-duration uncertainty. Timing defaults require practical validation.

Repeated identical feedback is harmless. Reject contradictory or out-of-order transitions explicitly without overwriting accepted execution evidence. Delayed started/completed feedback for an unresolved attempt can establish what actually happened even after its request expired, but must not reissue the expired command or affect another task/attempt. Retain event provenance and attempt history across replanning and restart. Delays and short low-power phases must not be mistaken for completion.

Expose each supported individual consumer as a virtual device. Publish relevant available measurements, forecast quality and expected consumption. Shiftable devices additionally expose task status, planned start and the applicable current command. The app owns learning, task state and planning. The thin integration exchanges typed requests/feedback and publishes entities. User automations retain physical control and safety. Explain device forecasts and scheduled consumption in the app's plan view.

Prevent double counting: subtract the selected devices' measured history from the training balance for remaining household consumption, respecting upstream meter overlap. Total planned household consumption is the residual forecast plus separate device forecasts and scheduled task profiles. A task profile replaces, rather than supplements, the corresponding expected device use. Missing exclusion evidence must not silently remove demand or permit duplicate scheduling. Historical plans retain their original device definitions, forecasts, profiles and tasks.

Provide a supported HA automation blueprint for the appliance execution/feedback chain. Users select the virtual OEP device, physical start action and reliable started/completed status triggers through the visual editor. Optional advanced inputs cover cancellation actions and positive evidence that the cancelled attempt cannot execute later. Do not require helpers, handwritten YAML/templates or copying task/attempt identifiers. Use available Energy-owned measurement links rather than requesting them again. Task readiness and its deadline remain a separate integration action usable from user automations.

The blueprint supplies the OEP request trigger, current-request validation before physical start, automatic feedback calls and the confirmation/expiry handling. The app returns a single-use authorization bounded by both plan and attempt expiry. Persistence or response delays cannot extend it. The integration validates the matching authorization after synchronization, and the blueprint rechecks its deadline immediately before an immediate, non-queued physical action. Expired authorization blocks starting but does not prove nonexecution. Task/attempt correlation and durable lifecycle state belong to the app/integration, not volatile blueprint variables. Reject ambiguous or stale status feedback rather than assigning it blindly to the newest task. The default blueprint must not infer not started from a timeout. Without cancellation assurance, report unresolved execution and block retries. A user-selected physical action does not make delayed third-party execution cancellable.

Use a simulated dishwasher in the real-Supervisor Docker developer environment as the reference acceptance installation. Exercise the real Energy discovery, history/profile ingestion, app configuration, task action, planner, HA virtual device, blueprint, simulated physical actions and mandatory feedback. Verify UI results and durable history. No production appliance is controlled. Include successful completion, confirmed failure followed by replanning, deadline infeasibility, delayed start cancellation, missing/late/duplicate feedback and restart recovery. A fixture-only state-machine test is not proof of this complete chain. Keep synthetic evidence explicitly labelled and report full-chain acceptance as pending until the required runtime path exists and has actually been exercised.

### 7.2 Local solar model with general weather inputs

- FORE-11: Use a local learned PV forecast with general weather and available HA production history. No separately configured HA solar-forecast integration is required. Use HA location across the European target area. This is target functionality, not an already validated model.
- FORE-12: Default to Open-Meteo weather/irradiance. Automatically use HA location, with no weather-provider selection/manual weather entry/extra forecast integration. Keep the adapter replaceable. Prefer irradiance over sunshine duration alone. Location/weather alone do not identify unknown plant size/yield.
- FORE-13: With insufficient history, begin with a simple weather-based estimate and minimal installation data, reliably prefilled where possible. No detailed panel model or ML settings. Learn from history/new measurements. Automatic solar model activation requires forecast accuracy evidence from matched forward comparisons under FORE-17. More data does not guarantee improvement. Show provisional quality. Minimum inputs are in FORE-14. Implementation must validate the initial calculation, without claiming accuracy for an unknown system.
- FORE-14: When usable production history is absent, require only nominal total panel Wp/kWp and approximate orientation, e.g. east/south/west/east-west, after reusing reliable HA data. Assume other properties and refine with measurements. No exact tilt/azimuth/panel type/shading/roof partition is mandatory. Retain assumptions with input/configuration for historic explanations. Choose/test numeric defaults during implementation, with no accuracy guarantee.

Product decision, 20 October 2026: use pvlib-python's PVWatts DC model as the physical solar-yield baseline, driven by Open-Meteo weather and the minimal installation inputs in FORE-14. This selects the implementation direction, not a validated dependency version or proven forecast accuracy. The weather source supplies the 48-hour coverage. The physical model itself has no fixed forecast horizon. Keep conversion losses and shared inverter limits in the installation model without double counting. Local historical learning and activation remain governed by FORE-03–09. The learning algorithm and acceptance thresholds still need implementation evidence.

- FORE-16: Reuse the separately configured solar production sources in HA Energy as onboarding suggestions. Support multiple solar installations/inverters, preserving each source's identity and production history. Build forecasts and learned corrections per independently measured installation, then aggregate without double counting a total and its constituent meters. Associate sources with their configured inverter and shared limits. A source is not necessarily one physical inverter. Compare model output and measurements at the same boundary, with no mandatory DC measurement sensor. Missing history for one source does not disable usable history for another.
- FORE-17: Support PVWatts, an optional supported HA production forecast, ML and Automatic per measured solar source. Initially support only Core Forecast.Solar. Provider adapters normalize configuration and forecast data to shared contracts, permitting future adapters without provider logic in the UI or calculations. A linked Forecast.Solar configuration owns exact panel Wp, azimuth, tilt and group names. Follow complete HA updates automatically and lock those fields in OEP. Retain accepted geometry and ownership during unavailable, stale or incomplete discovery. Only fresh complete discovery explicitly confirming no link releases manual editing, retaining geometry and showing an informational notice. Relinking automatically adopts HA geometry and explains replacement of manual values. A first unreadable link never invents geometry. Physical inverter identity, AC rating, efficiency and hybrid settings remain independently configured. Provider model limits never supply physical ratings. Preserve immutable configuration provenance and unsaved drafts. Automatic may admit machine learning after a successful chronological historical forecast assessment against PVWatts without waiting for new captured days. The independent captured-source comparison, including Home Assistant, uses the lowest mean absolute hourly energy error across the same three previously captured, complete local-day windows within thirty days. A linked actual-curtailment sensor filters learning and accuracy intervals using original observation times. Without a link, and before its first valid observation, otherwise eligible production is assumed unrestricted. After that observation, true and unknown control states exclude affected intervals. Persist the original first observation and checked coverage across restart. Proposed commands and prices never establish actual control. Use HA timezone and complete UTC Recorder hours. Never reconstruct unrecorded forecasts or treat missing measurements as zero. Archive original HA forecasts, honest observation/refresh/unknown-issuance times and selection evidence. An unavailable or ineligible choice falls back explicitly to PVWatts. Post-inverter AC cannot establish hybrid available DC, curtailment or unknown shared routes. No financial improvement is implied by accuracy ranking.

Implementation update: the first solar increment implements a pvlib/PVWatts baseline
and per-source regularized gain-regression candidates with the actual HA source
editor/Recorder path. Automatic solar source selection uses matched forward accuracy under
FORE-17. Economic shadow comparisons remain retained research evidence. A fitted
correction alone does not replace active forecasts. See the
[implemented boundaries](../docs/development/SOLAR-FORECASTING.md).

Research, 18 October 2026: Open-Meteo provides European coverage, irradiance, temperature and historical weather/forecasts. Validate model inputs, intended access/distribution terms and startup strategy. No local accuracy or financial gain has yet been demonstrated. Existing HA forecasts may later provide an optional input/comparison.

Sources: [forecast](https://open-meteo.com/en/docs), [historical forecasts](https://open-meteo.com/en/docs/historical-forecast-api), [access terms](https://open-meteo.com/en/pricing). Free hosted access is for non-commercial use. A data licence is not unlimited API access. Quarter-hour output may be interpolated rather than native throughout Europe/horizons. Panel configuration alone cannot reconstruct an old forecast archive. Training on realized weather and evaluation against forecasts actually available then are different.

## 8. Prices, contract rules and bonuses: confirmed

- PRICE-01: Fetch bare day-ahead prices from an internally chosen market source, without required supplier HA integrations.
  Use automatic daily refresh at 13:05 Europe/Amsterdam. Retry missing/incomplete data every 30 minutes until complete, with one additional 16:05 correction check. Startup reuses cache and retrieves missing expected coverage. Fetch timing is independent of plan computation.
- PRICE-12: Infer bidding area from HA location only when reliable/unambiguous, otherwise ask the user to select it once. Country is not always bidding area. Keep the selected area as an installation setting, display it and allow later correction. Do not require an extra setup question when the area is reliably inferred.
- PRICE-02: Separate source adapter, contract evaluation and planner.
- PRICE-03: Configurable import/export formulas replace mandatory maintained country tax profiles. These formulas define base prices only. Bonuses are separate rule components and separately visible financial amounts.
- PRICE-04: Version formulas/conditions with effective dates.
- PRICE-04a: Select applicable historical rules by contract/effective period, including plans spanning periods. Reference reused immutable versions instead of copying all rules per plan. Corrections for the same effective period create new versions with effective/recorded times. Re-evaluation may use corrections, while original plans/rules/results remain distinguishable. Merely matching contract/period without preserving original rule text is insufficient.
- PRICE-13: Reprice stored plan flows after contract corrections without re-optimizing. Preserve the original and create a separate valuation version referencing plan, rule versions, initial/counter states and calculation time. Mark the contract update. Distinguish original expected, repriced expected and realized costs. A repriced old plan is not automatically optimal under corrected rules. Automatic execution follows PRICE-15.
- PRICE-14: Default to the latest successful cost evaluation, labelled `Contract updated` with calculation time. Keep original expected cost in details. Plan/strategy/input versions remain unchanged.
- PRICE-15: Automatically re-evaluate affected costs/savings/plan valuations in the background after corrections, propagating bonus-volume/cap changes chronologically. Publish successful versions only. Preserve original expectations/earlier evaluations. Never historically re-optimize.
- PRICE-05: Preview formulas at positive, zero and negative market prices before applying.
- PRICE-06: Separate bonus rules compose an extra-rate formula, price/time conditions and chronological period counters. The user decision of 20 October 2026 removes configurable energy-route eligibility. Bonus volume is aggregate grid export, without PV/battery source attribution. An optional paid-window restriction prevents planned grid export together with battery discharge while quota remains at the segment start, allowing local self-consumption. Exhaustion releases later segments until a reset or cap increase restores quota.
- PRICE-07: Equivalent visual rule builder and text editor. No arbitrary executable scripts.
- PRICE-08: Accept only prevalidated constructs exactly translatable to the planning model. No hidden approximation.
- PRICE-09: Do not expose permitted-energy-route selection in bonus configuration. Apply supported bonus conditions/counters to aggregate grid export. Contracts requiring source-dependent exclusions are outside exact supplier coverage. Disclose this limitation rather than presenting the simplified rule as exact settlement. Physical route modelling remains required for energy balances and conversion losses.
- PRICE-10: Actual counters are separate from candidate plans. Calculating/replanning consumes no real bonus volume.
- PRICE-11: Calendar and contract years are distinct. The first dated bonus activation starts its initial allowance without moving the supplier anniversary. Later versions of the same bonus retain accumulated volume. Apply contractual ordering/counter rules, rather than letting the solver allocate bonus volume arbitrarily.

Specify grammar/functions, units, currencies, timezone boundaries and supported taxation constructs. Initial counters follow UI-15. Reconstruction must be tested. Explicit tested blocks do not imply every international contract is supported. Zonneplan, NextEnergy and Budget Energie are research examples, not embedded supplier logic. Verify bonus bases, accrual and effective terms for implemented examples. Source-dependent supplier exclusions remain a disclosed unsupported condition under PRICE-09.


Import costs, export revenue and bonus income are separate financial components. Bonus-period counters and dated rule versions preserve contractual limits and changes.

## 9. Price forecasts and horizon: confirmed

- HORIZON-01: First release uses an external price forecast targeting 48 hours from planning time.
- HORIZON-02: Published prices override forecast prices. Retain interval provenance.
  Compose a provider-neutral UTC quarter-hour price timeline, retaining published/forecast kind, native resolution and original source versions. Repeat an hourly source price across four quarters without interpolation or fabricated precision. Preserve partial horizon edges and genuine source boundaries. New published prices replace predictions in new planning inputs. Original historical plans retain their original inputs. Hourly forecast repetition is not quarter-hour prediction detail.
- HORIZON-03: No local market-price model training in the first release. Local demand/PV learning remains included.
- HORIZON-04: No required DAO/DAP forecasting service.
- HORIZON-05: Internally chosen replaceable source. No normal API-address entry.
- HORIZON-06: If the extra forecast is missing, use available published prices and explicitly show `Shortened horizon`. Missing prices are not zero.
- HORIZON-07: Include terminal stored-energy value so the horizon boundary alone does not empty batteries.
- HORIZON-08: Keep exactly 48 elapsed hours from the quarter-aligned plan start. Use quarters for at least 24 hours, extending to the next UTC clock hour before switching to hourly control blocks. The fine section spans 24h to 24h45m. Clip the final block to the horizon. A whole-hour start gives 96 + 24 = 120 control groups. Other quarter starts give 121 to 123. Preserve quarter-hour billing, source provenance, exact contract/bonus boundaries, prices and energy accounting within each control group. Coarse planning does not average tariffs or introduce extra within-hour power decisions. Keep shortened-horizon fallback. See [price timeline and control grid](../docs/development/PRICE-TIMELINE-AND-CONTROL-GRID.md).
- HORIZON-09: Terminal value reflects expected later avoided effective imports or net sales within losses/limits, not acquisition cost alone. Both scenarios use the same automatic method/information without an extra setting. A 48-hour view informs use/sale opportunities but forecast prices are uncertain and do not replace terminal value. Avoid double-counting planned flows. Validate the function/window independently.

Decided: resolution switch after 24 hours and valuation basis. The configured installation's terminal function is `bounded-average-v1`: one automatically derived net rate and bounded useful volume per battery, using up to 24 later price hours or an explicitly labelled trailing-window proxy. [Method, assumptions and tests](../docs/development/TERMINAL-VALUATION.md). Open: minimum price coverage, final provider access and empirical decision-quality validation. A repeated price profile is not a forecast beyond available coverage.

## 10. Planning, execution and deviations: confirmed

- PLAN-01: Replan periodically and on meaningful triggers. Coalesce updates and choose automatic timing defaults. No solver/timing expertise required.
- PLAN-02: Not every fluctuation warrants a calculation.
- PLAN-03: Transfer the complete accepted replacement plan, not only its current command.
- PLAN-16: Activate only after full reception/validation, as one logical replacement. No partial-plan command publication. Rejection/incomplete transfer retains only a still-usable old plan. It cannot revive an invalid one. Check structure, configuration/entity compatibility and usable time coverage. Optimization/model validation stay in the app. No second solver/model in HA.
- PLAN-17: Use one sequential calculation/delivery/acknowledgement stream. Finish or explicitly fail delivery before the next. Coalesce pending requests. Add delayed-message ordering protection only when actual transport/recovery requires it, not as a mandatory elaborate protocol. Archive version identity is still required independently.
- PLAN-04: The integration executes schedule transitions and publishes desired state now. Automation processes no future times. Expose the original generation timestamp and ID of the plan behind current virtual commands on the EMS device. Polling and schedule transitions retain that timestamp. Newly accepted plans update it even with unchanged commands. Make it unavailable without active commands, including pause, expiry or revocation. A disconnected but still executable cached plan retains its timestamp. This describes proposed execution, not physical feedback.
- PLAN-05: An unchanged desired value need not trigger merely because a plan arrived.
- PLAN-06: Desired state holds until changed. Automations need not process per-command duration. Plan validity is internal.
- PLAN-07: A 15-minute recalculation interval is neither a 15-minute horizon nor validity limit.
- PLAN-08: Forecast errors/fluctuations are normal replanning inputs, not automatic execution faults/alerts.
- PLAN-09: Fast zero-grid regulation is outside the planner. Interpret tracking relative to mode and physical limits.
- PLAN-10: Standardized availability/deviation feedback. Ordinary HA source entities require no automation-created feedback sensors.
- PLAN-11: The app chooses participation of additional devices and publishes status. Keep available measurements in the energy balance.
- PLAN-12: Legitimate price-quarter transitions remain allowed. Add switching/stability restrictions only if practical tests show current replanning is insufficient. Fast physical grid fluctuations are not planner switches.
- PLAN-13: A validated plan may start immediately within a price quarter. Unchanged commands cause no extra change. Plan only the remaining part of the current billing interval from current measured state.
- PLAN-14: Automatically bound ordinary recalculation and excessive command changes. Coalesce updates instead of recalculating/publishing every sensor update. Exact delays/thresholds remain open. Do not delay revocation of a known infeasible command.
- PLAN-15: Replan normally on each UTC quarter boundary. Material measured deviations or new forecast/price inputs may request an earlier round, at most once per five minutes. Initial readiness, explicit configuration/resume and hard safety invalidation remain immediate. Do not treat ordinary measurements or their old freshness deadlines as solver triggers. Existing scheduled commands continue independently. See [workspace and cadence](../docs/development/EMS-VISUALIZATION.md) for the named initial deviation thresholds and command validity policy.

Open: thresholds, delays, notification policy, explicit execution actions and future-period behaviour of additional unplanned devices.

## 11. Failure, pause and validity: confirmed

| Situation | Behaviour |
|---|---|
| Temporary app/connection loss | Continue the last usable plan |
| Backup restored | Do not command from restored history. Remain `unavailable` until a fresh validated plan from current core measurements/valid prices |
| Ordinary restart | Resume a saved plan only after coverage, age, configuration and current-data checks. Request an updated plan |
| No current coverage | Affected commands become `unavailable` |
| Maximum age exceeded | `unavailable`, even with coverage |
| Known change invalidates a command | Revoke without waiting for expiry/age |
| Required core data missing | No new plan. Old one only while still usable |
| Additional price forecast missing | Published-price shortened horizon with terminal value |
| Manual battery takeover | Pause whole EMS. Automation releases physical control |

- FAILURE-01: Derive required core data from configuration. No battery means no required SoC.
- FAILURE-02: Non-controllability differs from a missing forecast and may immediately invalidate commands.
- FAILURE-03: Unchanged commands do not prove failure. Use coverage, age and known conditions.
- FAILURE-04: Automation may react to `unavailable` with chosen fallback. Integration does not take over hardware.
- FAILURE-05: Restored plans are historical only. Receive current required measurements/prices and calculate/validate a new plan before commands become available. HA automation handles fallback meanwhile. Specify restore detection versus ordinary restart.
- FAILURE-06: Ordinary restart may resume a compatible covered not-too-old saved plan if current required measurements do not invalidate it. Check before availability. Restored/archive measurements are not current. Request a new plan afterward. Until successful resume/new acceptance, commands are unavailable. This does not require recalculation for every ordinary restart. Restore rules differ.
- FAILURE-09: Ordinary resume requires saved enable intent on. Saved pause keeps commands unavailable. Insight-only planning follows FAILURE-07. Explicit enable follows FAILURE-08.
- FAILURE-10: Log problems/diagnostics in the responsible component. Add a visible HA notification for serious faults requiring user action, with problem, control/planning impact and concrete remedy. Temporary self-healing unavailability or explicit pause alone is not an actionable fault. Deduplicate ongoing alerts/logs. Do not log normal fluctuations as errors. Specify categories, levels and HA mechanism during implementation. Logs are separate from the plan/evaluation archive.
- FAILURE-13: Expose active blocking conditions through a notification center on every authenticated app content page. Place its bell and active count in the desktop sidebar and mobile header. Hide it when fresh status confirms no active notices. Selecting a notice opens the responsible recovery page, which shows the full explanation, planning impact and concrete action. Other pages retain the compact center. Distinguish expected waiting from faults and calculation in progress from successful recovery. Keep both views until fresh status confirms recovery, then remove both. Failed reads must show status as unavailable or explicitly mark retained notices as stale. Preserve authentication and unsaved-draft navigation safeguards. Log blocker creation, reason changes and successful recovery once per transition. Include elapsed blocked time after successful publication. Keep history validation diagnostics bounded and separate from financial evidence. The app never invents counters or bypasses safety validation to clear a notice.
- FAILURE-14: Each responsible module owns notification causes, impact, recovery advice and evidence of resolution. Use a shared presentation and collection contract with stable cause/resource keys. Deduplicate a propagated cause without merging independent problems. Keep severity separate from blocked operations. Distinguish an addressed cause from confirmed restored operation. Unknown, failed or malformed source reads retain last-known evidence as stale while healthy sources update independently. The frontend renders and navigates without interpreting domain cause codes. Users may close the popup but cannot dismiss an unresolved cause. Share lifecycle tests and keep cause-specific tests with their owners. Both notification views must show the affected sensor, device, source or rule when known, the actual reason, operation impact and remedy. Resource identity is actionable information, not optional technical detail. Do not expose arbitrary provider diagnostics or credentials.

Measurement admission: A valid current Home Assistant state remains usable when its value and original report timestamp do not change. Zero export, a battery resting at minimum SoC, an unplugged vehicle's unchanged SoC and a stable tank temperature are normal. Without explicit evidence, do not infer disconnection from state age or claim physical connectivity. Require current, authenticated integration captures and retain unavailable/unknown, identity, number, unit, range, chronology and counter-reset validation. Preserve original sensor timestamps. Explicit device sample timestamps are also provenance and never an age-only reason to reject a current valid HA state. Integration report expiry and command lifetime remain independent. This does not introduce automatic partial planning or silently omit configured devices.
- FAILURE-11: Rotate/delete technical logs under a bounded policy. Do not treat plans, measurements, forecasts, config/rule versions, evaluations or dependencies as rotatable logs. Choose concrete log size/count/age limits during implementation.
- FAILURE-12: App lifecycle logs show starting, started, stopping and stopped. Every physical app diagnostic line, including multiline messages and tracebacks, has the same explicit UTC timestamp including milliseconds, level and logger format. Ordinary measurement/forecast/price updates must not starve publication: publish a still-usable active result, then calculate the newest coalesced input. Only missing/unusable core input, changed configuration/strategy, expired input/first interval or battery dispatch outside measured reserve/capacity block publication at INFO level and preserve newer readiness/invalidation. Unexpected failures remain ERROR with a full traceback. Started confirms application initialization. Stopped follows graceful resource cleanup. Use Supervisor/container stdout rather than a second log archive. Hard termination or power loss cannot guarantee a final stop message.
- FAILURE-07: Explicit pause makes commands unavailable and exposes `paused` separately from failure. Automation chooses fallback. Integration prescribes no physical fallback command. Keep insight-only planning with current inputs when possible under normal calculation limits, marked inactive for control. New plans do not make paused commands available. Expected advantage while paused is not realized savings. Resume follows FAILURE-08.
- FAILURE-08: After pause, calculate a new plan from current required data/prices, then validate/fully receive/accept under PLAN-16 before availability. Do not activate old saved or paused insight plans without this round. Failures/missing data retain unavailable/fallback. Do not reset independent reference SoC/counters.

Open: maximum age, persistence/restore detection, data freshness/validity thresholds, precise release/pause states, immediate invalidation versus tolerated transient absence, and explicit revocation during connection loss. Restart/restore behaviour is agreed.

## 12. Solver and quality: confirmed

- SOLVER-01: Translate supported contract blocks to an in-memory native HiGHS model through highspy's algebraic interface. MILP is selected. Continuous cases may solve as LP.
- SOLVER-02: Distinguish feasible from proven optimal within tolerance.
- SOLVER-03: A validated feasible plan may be used without optimality proof, with clear status.
- SOLVER-04: Independently check energy balances, limits, counters and costs before publication.
- SOLVER-05: Automatically use extra compute where practically worthwhile. No user tuning requirement.
- SOLVER-06: Mentioned runtimes are estimates unless measured, not benchmarks/requirements.
- SOLVER-07: Direct highspy/native HiGHS is the sole production stack. Preserve modular classes, explicit physical/economic contracts and independent validation. Do not add a production solver selector or parallel adapter. Current performance and native platform admission require representative measurements and release checks. The [selection rationale](SOLVER-SELECTION.md) defines the boundary.
- SOLVER-08: Prefer modern actively improved solver/model/Python components, assessing releases, useful changes, bug handling, compatibility, correctness, performance, licences and delivery. Not age/commit count alone. Ship jointly tested versions and regress updates.
- SOLVER-09: Update internal components only in a joint EMS release, not user-selectable libraries. Comparisons are pre-release tests, not old-plan recalculation. Preserve original inputs/versions/results. A new round creates a new plan.
- SOLVER-10: Bundle working Python/native runtime. Users need not compile. Prefer compatible wheels. Any source build is controlled/pinned in release CI, not an assumed speed improvement.
- SOLVER-11: Measure the whole round/relevant phases before optimizing. Incremental updates/warm starts are optional only after same-input full-rebuild comparison, independent validation and practical gain. Every round remains a new plan. No leaked prior state/constraints.
  Planning performance insight: Insights provides a visual daily calculation history, defaulting to today in the Home Assistant timezone with a day picker. A stacked, time-scaled duration chart and equivalent keyboard-accessible list open a calculation detail dialog, full-screen on phones. Planning links to this history. Retain observed first financial candidate time, subsequent calculation, native preparation/service timings, actual time budget, strategy, interval count and publication outcome. A native candidate is not yet an independently validated published plan. Unknown first-candidate times use total-only bars. Details show bounded original financial candidate and lower-bound observations. Explain remaining possible improvement in currency and percentage when defined. This is model quality, never percentage completed, a bill or realized savings. Never invent historical progress or infer a causal diagnosis from duration alone. Telemetry faults cannot block validated publication or change solver settings.
- SOLVER-12: Include supported decision-dependent costs, revenues, bonuses, wear and inventory value in the correct objective. Expose grid cash, wear and inventory separately. Objective value is not automatically a bill. Consumption strategy prioritizes gross import. Hard limits cannot be bought off with penalties. Example peak/comfort/startup costs do not add release scope.
- SOLVER-13: Bound/measure native mathematical thread pools, setting budgets before imports. One solver thread does not constrain every library. Distinguish resident and private memory. No extra user tuning or permanent one-thread product rule follows.

Open: new model/dependency acceptance, budgets/tolerances/priorities, time-limited results, capacity detection/reference hardware. Infeasibility does not authorize relaxing hard constraints.

## 13. Visual understanding and comparison

- UI-01: Visually explain planned control. A long decision list is not the main view.
  Do not duplicate interactive charts with full visible lists when they already provide the same detail access. Preserve keyboard and assistive access to those details. When a large read-only result list is needed, use compact aligned rows and 25-row Previous/Next pagination with a visible page number, following Activity history. Preserve record identity, responsive values and focus return. Paging must not truncate related charts or change their selected period. Pending, empty and failed reads remain explicit. Apply this pattern to future large lists without replacing task-specific editors or charts.
- UI-20: Provide factual evidence for the whole original planning period first, with forecast charts/totals, recorded per-source model/quality and planned totals, followed by an optional selected-interval drill-down. Explicitly state time bounds/duration and whether kWh covers that interval or the whole horizon. Prices remain rates per kWh. Include original effective prices/provenance, prepared bonuses/counters, planned device flows and limits, initial/reserve state, losses, wear and terminal-value assumptions. Preserve missing evidence and feasible-versus-optimal status. A reached limit is an observation, not a proven cause. Do not invent source-to-battery attribution on a shared AC bus or substitute a shadow/current model for the recorded forecast. Reading evidence must not reoptimize or change commands/history. See [plan evidence](../docs/development/PLAN-EVIDENCE.md).
- UI-21: A separate future explanation layer compares explicit alternatives on the same frozen original input and strategy, with independently checked outcomes. State tested quantity, feasibility, objective change and proof status. Only optimality or sufficient bounds justify a claimed disadvantage. Distinguish equal alternatives and inconclusive results. Layer 1 must not imply these comparisons have occurred. This increment implements Layer 1 only.
- UI-02: Distinguish planned, forecast and measured on a comparable timeline.
- UI-03: Show future expected and retrospectively evaluated benefit against self-consumption without price optimization on the same installation.
- UI-05: Default reference is `Self-consumption without EMS`: charge surplus PV, discharge to cover demand, no price-driven grid charging/battery export, same capacity/power/losses/reserve. Zero grid is best effort. Export overflow/import deficits. Without storage, direct self-consumption and surplus export. This is simulation, not measured alternate reality.
- UI-05a: Reference PV produces/exports surplus even when export costs money. No price-driven curtailment, grid charging or battery export. Retain physical/self-consumption rules and use this reference for both strategies.
- UI-05b: Estimate uncurtailed available PV with the yield model under physical/weather limits. The reference retains it. Permitted EMS controls may reduce/stop it. During curtailment, low measured production must not replace available reference PV. Preserve provenance/quality under STORAGE-09 and mark it as estimated.
- UI-06: Show comparable EMS/reference plans with separate flows, battery trajectories and cash costs/revenues under the same external conditions/rules/installation. Report expected versus retrospective advantage. Continuity follows UI-12–15. Valuation follows HORIZON-09 and remains independently validated. Compare future self-consumption and OEP from the same measured battery stock and opening bonus budgets frozen in the plan, using the same start, horizon, installation, tariffs and external forecasts. Show the expected extra saving from choosing OEP now, including remaining battery value and negative outcomes. Historical measurement gaps must not block this forecast. The continuous historical reference and its evaluated savings remain independent. Preserve existing archived expectations.
- UI-06a: Distinguish original expected plan cost, actual EMS cost from realization, and simulated reference cost under the same retrospectively available conditions/rule versions. Never substitute predicted EMS cost for actual retrospective cost. Mark simulation/estimates. Do not claim measured counterfactual or causal proof. Separate cash/wear/inventory under UI-10. Version model/reference policy with results. Updates do not automatically overwrite/recompute historical plans/evaluations.
- UI-07: Include remaining battery energy value using the same method, separately from grid cash. It is not realized cash revenue. Exact method remains open.
- UI-08: The app supplies the full visual overview without user dashboard construction, reachable through one HA sidebar entry for plans, savings and adding/editing devices. Thin-integration summary entities remain reusable in personal dashboards. Select details and test HA OS/Container access during implementation.
- UI-09: Retain recorded activation and control identity independently of full-plan retention. Permit opening retained original plans with their strategy, inputs and expectations. Advice is not proof of execution. Kept originals are never recalculated with hindsight. Full versions follow STORAGE-16 rather than permanent retention of every recalculation.
- UI-10: Show actual, simulated battery self-consumption and simulated without-battery daily costs with independent coverage. Financial calculations remain chronological quarter intervals. Display complete, partial and unavailable scenarios independently. Compute each benefit only on matching accepted intervals, without subtracting unmatched scenario totals or extrapolating missing coverage. Keep cash, wear and inventory value distinct.
- UI-11: Evaluate completed HA local days and retain provisional results during three complete recovery dates after day end. Finalize at local midnight D+4, including partial/unavailable scenarios and their missing ranges. Preserve compact daily results, quality, energy, battery endpoints/value, installation, contract, method and independent continuation permanently. Finalized days are immutable and later corrections affect only provisional days. Existing history receives an upgrade recovery window before detail can expire. Current-day results are not represented as completed-day savings.
- UI-12: Maintain a continuous reference with its own SoC. Date selection filters stored results instead of restarting/synchronizing the reference. Independent bonus counters are also required where applicable.
- UI-13: Start comparison when required data first exist, same time and measured initial SoC. Thereafter states evolve independently. No battery removes the SoC condition. Hardware changes follow UI-16.
- UI-14: Same contract terms, separate actual/simulated flow-based counters per scenario. Caps may be reached at different times. Initial counters follow UI-15.
- UI-15: Both scenarios inherit the same bonus volumes accrued before EMS start, inferred reliably from history/rules or a confirmed initial balance. Do not grant a fresh budget mid-period.
- UI-16: After hardware changes, continue both scenarios with the new installation without resetting comparison period, independent SoCs or counters. Preserve past configuration/plans/results. Only added/replacement batteries initialize both scenarios with the same known energy. Treat energy introduced/removed/capacity-state corrections as installation transitions rather than savings. Use ordinary setup/measurements, no comparison wizard. Test postings.
- UI-17: Write off removed stored energy and book actually introduced energy at zero EMS acquisition cost as separate external inventory movements. Added capacity alone creates no energy. Exclude movements from grid flows, bonuses, cycles and savings. Correct value change using the same valuation. Zero acquisition cost is not zero economic value. Keep quantity/reason with the transition, no extra wizard.
- UI-18: Show financial difference in EUR/configured currency and gross-import kWh difference under both strategies for the same reference/period. Positive means lower cost/import. Display negatives too. Lower cost may mean more import. Do not net export against gross imports. Separate expected/evaluated values and cash/wear/inventory.
- UI-04: Identify costs derived from measurements versus simulated reference costs. The unexecuted alternative is not measured. Keep expectation and retrospective benefit separate. Do not add overlapping plan benefits.

Open proposal: shared timeline for prices, flows, SoC and modes with selected-period explanations. Exact charts/layout/comparisons remain design work.

## 14. Historical storage and reconstruction

- STORAGE-01: Reuse unchanged versioned data with deltas/periodic full snapshots, avoiding unnecessary full input/plan duplication.
- STORAGE-02: Retained original snapshots and immutable configuration versions keep exact values. Full-plan and temporary settlement-detail retention follow STORAGE-16. Lossless compression and shared-block collection must preserve every retained reference.
- STORAGE-07: Every saved plan/evaluation references exactly used measurements, forecasts, prices, rules, configuration and initial scenario states. Reuse unchanged versions and preserve originals after correction. Contract/effective-period matching alone is insufficient.
- STORAGE-03: Reconstruct saved content/references, not re-optimize with current data. Preserve numeric values/timestamps/units/modes/metadata without lossy rounding.
- STORAGE-04: Record base/input versions. Deltas express interval additions/removals. Identify intervals by absolute time/duration, not moving-array position.
- STORAGE-05: Atomically commit content/references, version formats and retain old readers. Equality-check reconstruction and lossless migrations.
- STORAGE-06: Exact stored-plan reconstruction is not a demand for identical solver reruns. Preserve inputs/method versions/original outputs under STORAGE-13.
- STORAGE-08: Store only compact measurements needed for visualization/learning/savings, not all HA sensor history. Ingest from HA but do not rely on indefinite HA retention. Specify minimal sets/resolution.
- STORAGE-09: Distinguish measured energy, estimated available curtailed PV, calculated wear and simulated reference. Keep separate import/export per applicable billing interval. Net daily totals/power samples are insufficient. Respect bonus windows/meter boundaries. Derived series retain provenance/quality. Gaps are not zero.
- STORAGE-10: Select data needed for benefit calculations and forecast evaluation at ingestion. Temporary settlement quarters and detailed historical plan projections may expire only after a verified finalized daily report and continuation are committed. Preserve separate control ownership for Forecast Quality. Do not archive every raw HA sample/log.
- STORAGE-11: Retain the chronological quarter-hour evidence needed during provisional-day recovery. Permanently preserve compact daily energy, costs, missing/estimated coverage, battery stock/value endpoints and configuration/method provenance. Finalized reports must remain readable without full old plans or indefinite HA retention.
- STORAGE-12: Recover missing grid volumes from fine HA history where possible. Usable hourly totals may be distributed linearly to quarters as explicitly marked estimates, not exact measurements. Preserve separate directions/boundaries/hour sums. Avoid duplicating measured volume. With partial quarters distribute only a consistent residual across gaps. Report conflicts. Costs retain estimated quality.
- STORAGE-13: Preserve exact content and hashes of retained original plans. Permanently retain finalized daily scenario outcomes, coverage, monetary/energy breakdowns and independent physical/financial continuation. Full historical quarter replay is supported only while temporary evidence remains. Daily totals cannot reconstruct intentionally expired originals. See STORAGE-16.
- STORAGE-14: Never rewrite original measurements or immutable rule/configuration versions. Provisional daily reports may be recalculated with current validated prices and corrections until closure. Finalized reports preserve their calculation/evidence versions and cannot be silently changed by later corrections.
- STORAGE-15: Exactness concerns saved resolution/content, not physical fluctuations within an aggregate. Preserve finer inputs where rule/control/reference behaviour requires them. Estimated quarters remain estimated.
- STORAGE-16: Automatically retain all complete plans for 24 hours, then one first publication per UTC quarter until age three days, one per UTC hour until age fourteen days, and one per HA local calendar day thereafter. Always protect active state. Before thinning an existing plan, verify and preserve independent calculation context and every logical activation identity. Collect only unreferenced shared blocks. Expire temporary Savings detail only behind a verified finalized daily checkpoint. Permanently retain compact daily results and control ownership. Large JSON may use verified lossless compression with existing plain-JSON read support and bounded resumable backfill. Kept originals retain exact numeric values, timestamps and hashes. Rollback to an older incompatible binary requires a consistent matching backup. This agreed fixed retention supersedes indefinite retention of every plan/evaluation quarter and does not alter HA Recorder retention or unrelated model/source archives.
- STORAGE-17: Use HA OS backup for app data and HA integration configuration together, no separate EMS scheduler/UI. Include app and HA in selection. Prepare a consistent database/config/version/reference state. Copying an active file arbitrarily is insufficient. Specify online consistency or temporary-stop procedure and test restore/app linkage without relying only on an old container IP.
- STORAGE-18: Standalone-container users use host/container backups. Document consistent complete database/config/version/reference backup, volumes, consistency procedure and post-restore storage/HA-link checks. No EMS backup scheduler. A container outside Supervisor is not automatically part of HA backup.
- STORAGE-19: External storage is a required configurable option, not contingent on SQLite performance. SQLAlchemy belongs in the app storage adapter. Support/test SQLite locally and PostgreSQL, MariaDB or MySQL externally for equivalent behaviour, migrations/reconstruction. Not every dialect or direct HA database access.
- STORAGE-20: Exactly one active backend. Changing with existing data requires an explicit SQLAlchemy transfer of all OEP tables (including forecast-selection evidence) that preserves configuration/history and rebuilds the target tables, overwriting their previous content. The app restarts exclusively on the verified target and no longer requires the source database. Never silently fail over to an empty local archive. Document external database backup/restore rather than assuming HA backs it up. Retain plan/failure rules on storage faults.

Assess compact measurement layouts against the current shared schema with representative interval data, retention, indexes and complete application workloads. Measurement-only fixtures do not establish a whole-system history budget.

Open: supported database/driver versions, snapshot cadence, backup consistency/restore checks/procedures and budgets. HA OS handles HA/app backup. External databases require appropriate backup. Stored history retention is decided. Build visualizations from saved data. Permanent chart images are not the primary data store.


## 15. Research and evidence boundaries

The [device-settings proposal](DEVICE-SETTINGS-PROPOSAL.md), based on DAO/HAEO research on 18 October, is not a new set of confirmed fields/defaults. Accepted decisions prevail.

| Subject | Finding and limitation |
|---|---|
| HA solar forecasts | Energy exposes linked series, without guaranteed issue time, uncertainty, full horizon or original historic forecasts |
| HA Energy configuration | Useful links, with optional/version-dependent fields. Presence alone is not full device knowledge |
| HA references | `search/related` finds known references. An empty result does not prove absence of consumers |
| DAO 2026.10.0.rc1 horizon | External forecasts and hourly steps extend beyond published prices, correcting the earlier 2026.9.1 finding |
| DAO historic costs/savings | Investigated reporting reads hourly HA statistics by SQL, combines own `values`, HA data and sometimes `prognoses`. Consolidation can copy grid flows to `values`. `calc_saving_cost()` compares with/without a battery by removing its net contribution and valuing both exchanges. This differs from our same-battery self-consumption reference |
| DAO plan storage | Investigated path stores series in `prognoses`. `DBmanagerObj.savedata()` updates by variable/time. This is not immutable full-plan version storage. The finding does not deny other logs/images |
| HAEO | Supplied series may be extended by pattern repetition, which is not an independent market forecast |
| EpexPredictor | BSD-3-Clause local candidate. Optional ENTSO-E key, with own Dutch load-forecast input disabled in investigated code. No demonstrated quality difference with/without key. Not selected after choosing an external first-release forecast |
| Energy Price Forecast EU | External candidate with limited private free access and separate product/business terms. HA-client MIT does not license the model/API data |
| Forecast quality | Published project metrics are not directly comparable. No winner or local training/physical validation was established by that research |

Primary research references are mutable, not pinned implementation versions:

- [HA Energy API](https://github.com/home-assistant/core/blob/dev/homeassistant/components/energy/websocket_api.py), [solar types](https://github.com/home-assistant/core/blob/dev/homeassistant/components/energy/types.py), [reference search](https://github.com/home-assistant/core/blob/dev/homeassistant/components/search/__init__.py).
- [DAO pricing](https://github.com/corneel27/day-ahead/blob/2026.10.0.rc1/dao/prog/config/models/pricing.py), [planner](https://github.com/corneel27/day-ahead/blob/2026.10.0.rc1/dao/prog/day_ahead.py), [HAEO forecasts](https://haeo.io/latest/user-guide/forecasts-and-sensors/), [EpexPredictor](https://github.com/b3nn0/EpexPredictor).
- [Energy Price Forecast EU private access](https://energypriceforecast.eu/en/private-pro/), [business access](https://energypriceforecast.eu/en/api-for-business/), [quality](https://energypriceforecast.eu/en/forecast-quality/).
- [Zonneplan](https://www.zonneplan.nl/energie/zonnepanelen), [NextEnergy](https://www.nextenergy.nl/zonnebonus).

## 16. Superseded proposals

Mandatory observation-only phase. Local EpexPredictor as the default. 72-hour default. Complete centrally maintained country tax profiles. Mandatory phase allocation. Alerts for every normal deviation. Repeat triggers for unchanged commands. And retaining obsolete capability entities without impact review were not accepted or were replaced by the requirements above.

## 17. Next technical work

Extend coherent HA publication and authenticated communication from DEV-30 to full installation/capability/source onboarding, automatic HA OS/Container connection and UI. Validate remaining contract blocks, partial billing intervals, terminal value and expected grid-following use in the selected highspy stack. Specify freshness/age/minimum horizon and final data-source access terms. Implement storage/consistent restore and hardware-state/value postings under UI-16 without restarting comparison. Validate available-PV estimation/training selection during curtailment under UI-05a/b.

Solar forecasting, startup profiles, historic counter initialization, permanent history and equal component releases are decided functions, not unanswered product choices. Technical implementation and product acceptance remain work. Do not silently treat open details as decisions or an experiment as the whole product.

## 18. Development and quality: confirmed

- DEV-01: Python for calculation module/integration. Browser technology remains an implementation choice.
- DEV-02: VS Code with automatic Dev Container provisioning, following HA's low-step onboarding. Open repository, reopen in container, automatic setup, documented run/debug/test tasks. Document host prerequisites such as Docker/extension.
- DEV-02a: The isolated Supervisor lab provisions usable simulated appliance examples through ordinary app APIs and shipped HA blueprints. Include weather-sensitive heat-pump forecasting, synthetic quarter-resolution dishwasher/laundry profiles, two cars sharing two charge points, an electric boiler and the documented Zonneplan bonus example. Enable OEP's HA sidebar entry once. Preserve custom configuration, history, tasks, user edits and later removals. Do not submit tasks, invent model-quality evidence or control physical equipment during bootstrap.
- DEV-03: Install pinned runtimes/system/project/test dependencies, relevant editor extensions and required UI-test browsers through one repeatable setup entry point. Repeating preserves local developer config/data. Review version changes. Python follows supported HA/solver compatibility, not DAO by default.
- DEV-04: Separate local HA test installation with app/integration, example configuration and simulated measurements/devices. Choose Core/Supervisor test arrangements during implementation. No production credentials or automatic physical control.
- DEV-05: Ruff lint/format and supplementary CodeRabbit review. External activation is separate from the requirements decision.
- DEV-06: Meaningful pytest coverage by default for planner, contracts, sources/forecasts, storage/reconstruction, communications and entities, including boundaries/faults and bug regressions derived from requirements.
- DEV-07: Playwright for important UI/end-to-end flows: onboarding, config changes, plan display, pause and history-loss confirmation. Verify visible behaviour/data exchange rather than page opening only. No UI framework is implied.
- DEV-20: Responsive phone-usable wizard/plan from first release. Playwright tests representative mobile/touch and desktop flows for reachable controls/notices, readable/interactable plans and complete configuration without clipping. Choose viewports/browser matrix during implementation. Emulation is not physical-phone/HA Companion validation.
- DEV-08: Same local/CI checks/configuration and documented commands. Clean Dev Container builds/runs checks without undocumented steps. Specify workflow/coverage/browser matrix within DEV-09 gates.
- DEV-09: Ruff, format, pytest and applicable Playwright must pass for the exact merged change. Repository rules should block missing/failing required checks. CodeRabbit neither replaces them nor independently permits merging. Private account plan limitations must be explicit, not claimed as enforced protection.
- DEV-10: Automatically check balances/hard SoC/power/grid/shared-inverter/AC/DC limits with known small solutions and varied loss/negative-price/bonus cases. Feasibility does not prove economic optimality. Test both where provable.
- DEV-11: Separate predesigned pytest planner/solver scenarios from unit/integration/UI tests. Record fixed inputs, initial states, setup/rules/horizon, expected status and independently justified outcomes. This is mandatory regression coverage.
- DEV-21: Begin with cheap-charge/expensive-discharge, negative prices, a capped bonus and energy retained beyond the horizon. Research DAO/HAEO/EMHASS for coverage inspiration only. Never copy their scenarios, code, fixtures or expected outputs exactly. Design independent inputs/oracles. Trace device/loss/limit/route/counter/time/error/status coverage. A snapshot/structure pass, skip or absent oracle is not proven scenario coverage. See [scenarios](PLANNER-SCENARIOS.md) and [coverage](TEST-COVERAGE.md).
- DEV-22: Keep device, route, inverter, contract, factory, extraction and validation responsibilities modular. Preserve independent scenario expectations. Test component/scenario isolation, binding ownership, independent finance, solver statuses, faults and timeouts. The maintained app has no Pyomo dependency. Emulation does not establish native ARM support.
- DEV-23: Measure input preparation, build, transfer/update, extraction, validation and total time where separable. Honestly label combined measurements. Performance claims need repeated representative cold/warm runs, sample count, p95/max, memory/status/gap. Small trials set no production budget. Cross-platform comparisons test validity/cost within justified tolerances. Exact schedules only if unique.
- DEV-24: Retain one selected production solver adapter and independent numerical oracles. Store comparative experiment results in their PR or CI artifacts. Do not maintain an alternative runtime merely to retain comparison history.
- DEV-25: Compare solver or interface changes using identical independently specified inputs, options and outcome expectations. Record repeated timing and native memory measurements separately for Windows/Linux AMD64. Include 48-hour mixed-grid cases and explicit coverage limits. Comparative performance does not establish complete contract/topology or native ARM support.
- DEV-26: Test the selected stack in pinned Linux with the same independent cases, image/resource identity and separate platform results. Memory counters differ. Absent private-commit information remains unknown. ARM emulation does not replace native CI.
- DEV-12: Derive expected outcomes independently, e.g. hand calculation/exhaustive small-model enumeration, never unsupported current solver output. Check cost components, flows, final SoC/counters and justified tolerances. With tied optima test optimal value/invariants, exact trajectory only if unique/explicitly required. Oracle changes need substantive justification, not snapshot regeneration. Infeasible/time-limited cases verify status and validity of any candidate.
- DEV-13: Replanning is a new plan with new input. No separate mandatory daylong repeated-optimization suite. Focus integration tests on replacement/state transfer/publication with fixed input/clock: newest measurements reach planning, independent reference SoC/counters persist, new commands replace old transitions, unchanged values do not retrigger. Optimization correctness stays in independent scenarios. This supersedes the daylong simulation requirement.
- DEV-14: Partial/rejected replacements never activate or partially command. Retain a valid old plan, or become unavailable when unusable. Verify complete successful replacement.
- DEV-15: One repository, separate app/integration/UI/tests/docs directories, supporting cross-component development/tests and both distribution structures. One repository does not mean one installer.
- DEV-16: Focused mismatch tests block new plans, retain usable loaded plans without extending validity and restore exchange when versions match. No supported mixed-version matrix.
- DEV-17: AMD64 is primary. Before experimental ARM publication, native ARM CI must build/start and run required scenarios/HA integration, not merely compile. Check dependency availability. Choose runner/practical hardware later.
- DEV-18: Direct highspy is selected. Continually check model/dependency changes against independent outcomes/boundaries/time/memory/install/licences, composition/multiple instances/isolation/imports. Preserve historical comparisons, not multiple production adapters. ARM gates unchanged.
- DEV-19: Bundled solver/runtime needs no payment, registration, activation or key. Verify/retain distribution rights/notices before selection. This does not choose the project's licence or external-data terms.
- DEV-27: Independently prove AC and hybrid DC cases, losses once per route/direction, separate/shared AC limits, local load/export, grid charging and DC charging bypassing AC. Reject excess or fictitious energy. See the [regression contracts](SOLVER-REGRESSION-MIGRATION.md).
- DEV-28: Multiple own-inverter batteries share one grid with independently justified import/export/load/local limits, SoC, reserves, losses and wear. Internal transfer is not grid billing or source-specific bonus eligibility. Different batteries may charge/discharge concurrently. Forbid simultaneous charging/discharging of the same battery and simultaneous external import/export. Apply aggregate-export bonus quota consistently. Test horizons, infeasibility and corrupted candidates.
- DEV-29: Hybrid PV/battery plus a separate AC battery must independently validate DC/AC routes, hybrid shared AC direction/rating, DC bypass, cell/route losses, internal transfers and mutation rejection. Preserve compatible financial and physical expectations. This does not establish arbitrary installation scaling or full product readiness.
- DEV-30: The synthetic app/HA chain must exchange complete authenticated JSON plans with real HA framework devices/entities, timers, complete replacement, unchanged-command suppression, unknown/zero/unavailable states, pause, fresh resume, expiry and automation capture without physical hardware. Setup and process smoke remain separate checks. This does not establish physical execution or backup restore.
- DEV-31: Freeze configuration, measurement, forecast and price versions/hash for each planning round. Run isolated native highspy and independently validate physics, limits, SoC and costs before storage/publication. Whole watts are constraints, not unchecked rounding. Superseded, stale, missing, invalid or storage-failed candidates never replace the accepted plan. Real HA receives HTTP plans from the separate app without importing highspy. Preserve independent numerical expectations across model changes.

## 19. Distribution and licence: confirmed

- DIST-01: Offer integration through HACS with `custom_components/<domain>/` and metadata. Custom-repository installation may precede a separate non-guaranteed default-catalogue application.
- DIST-02: HA OS app uses a custom Supervisor app repository, not HACS. Supply metadata/repository description and document both installations as one onboarding route. HA Container gets the same calculation module separately plus HACS integration. Registry/layout are implementation choices.
- DIST-03: App/integration share one tested release number and source release, but users install/update them separately. New plan exchange requires exact version equality. No supported cross-release matrix or automatic joint update implied.
- DIST-04: During mismatch reject new plans, retain only a loaded usable plan without age/coverage extension, otherwise unavailable/user fallback. Show installed versions/update need. Recheck a new plan after equality. This neither supports mixed-version new exchange nor guarantees old storage readability after update.
- DIST-05: Release only when both same-source artifacts build, mandatory checks/joint tests pass and both are available before users are told to update. Failed build/test/publication of either blocks release. Stage artifacts as needed. Separate installation remains.
- DIST-06: Initially support/test latest stable HA only. Record actual tested version. No older/beta/dev matrix or assumption that a new HA release is already compatible. Do not auto-update users' HA. Specify untested-version behaviour later.
- DIST-07: AMD64 primary. ARM64/aarch64 only explicitly experimental after packages and native CI pass. Native CI does not replace real target-hardware correctness/performance validation. Full support requires that evidence. Contributors may provide hardware. Equal initial platform support is superseded.
- DIST-12: Same source/model/dependency release across architectures, appropriate hashed packages, retained Python/solver/base/digest identities. Publish only qualified manifest variants. No architecture branches/automatic parity. Check ABI/Python/libc as well as CPU.
- DIST-13: Pinned Python/highspy without Pyomo/APPSI/user compilation. Experiment uses Python 3.12.14 bookworm/glibc, image/package hashes, AMD64 and ARM emulation checked. ARM remains gated. HiGHS/highspy MIT notices must remain. Experiment image is not final HA app packaging.
- DIST-08: Publish the entire app/integration/UI as open source under intended AGPL-3.0-only subject to dependency review. Keep licences/notices. Source publication does not license external weather/market/forecast data.
- DIST-09: Distributed modified versions and relevant modified network-service versions must provide corresponding source under the selected reciprocal licence. Commercial use is allowed. Exact combination scope needs review.
- DIST-10: Project app/integration/UI/tests use AGPL-3.0-only with dependency compatibility checks. Contributions use the same licence while authors retain copyright. Dependencies keep their terms. Include licence/copyright/contribution terms and release source. Apply actual distribution/network obligations, not a blanket demand to publish every private edit. Future dependencies still need review.
- DIST-11: First-release UI is English only: wizard, plans, statuses, notices and default virtual names. User names and HA's own language follow user settings. Repository documentation/names are also entirely English by the user's 19 October instruction. No parallel second-language version is required.

Distribution references checked 17 October: [HACS requirements](https://www.hacs.dev/docs/publish/integration/), [app repository](https://contributors.home-assistant.io/apps/guides/repository/). HACS does not install Supervisor apps.

DAO research, 17 October: its inspected Dev Container runs `script/setup` → `scripts/setup_environment.py`, preparing Linux venv, app/tests, dependency integrity, built assets and separate preview config without overwriting existing edits. This is an onboarding reference, not a DAO dependency or requirement to copy its stack. Its later developer web page launches VS Code. Actual provisioning remains local.

References: [HA developer environment](https://contributors.home-assistant.io/core/development/environment/), [app testing](https://contributors.home-assistant.io/apps/guides/testing/), [DAO Dev Container](https://github.com/corneel27/day-ahead/blob/main/.devcontainer/devcontainer.json), [setup](https://github.com/corneel27/day-ahead/blob/main/script/setup), [provisioning](https://github.com/corneel27/day-ahead/blob/main/scripts/setup_environment.py).

- UI-19: Use a responsive workspace with clear Planning, Savings and Installation settings navigation. Interactive locally served time charts show energy, prices, battery state and expected commands, with keyboard-accessible inspection, toggles and shared time windows. Keep forecast, original advice, recorded results and simulated reference explicit. See [chart research and design](../docs/development/EMS-VISUALIZATION.md). One Price inputs graph shows the original prices saved with the selected plan, including historical plans. Only when no plan is available, show an explicitly labelled preview from cached market prices and saved dated contract/bonus rules, independently of measured bonus-volume history. Show potential total export rates before the volume cap, with exact price/time eligibility and explicit unknown coverage. New prices and contract changes never overwrite the selected plan's archived prices. Preserve plan prices and their source classification. Daylight bonus eligibility uses whole minutes, rounding sunrise up and sunset down. Preserve old plan evidence and prevent false chart gaps at its precise boundaries.

### Reference and input visualization clarification

All expected balance charts display aligned fifteen-minute bars, irrespective of
internal bonus or control boundaries. Preserve energy through duration-weighted
aggregation, retain gaps and label partial horizon coverage. Keep exact prices,
bonus eligibility, costs, SoC timestamps and original interval inspection unchanged.
The without-battery, self-consumption and OEP charts share identical kW bounds and
ticks from their joint visible signed stacks. Recalculate on time-window, plan,
legend and size changes. SoC remains on its own fixed percentage axis.

UI-20 also requires two expected reference balances: default without battery versus
OEP, and future battery self-consumption beside OEP. Both future battery
scenarios start from the same measured stock and opening bonus budgets. Keep the
separate continuous historical evaluated-savings policy unchanged and explicitly labelled.
Use identical frozen external forecasts, physical converter limits and original
price inputs. Show the reference above the optimized balance and the factual
explanation below. Available solar/household forecasts and spot-market, import and
export prices must be readable over the complete horizon, not only in the selected
interval. Distinguish available DC forecasts from clipped/converted production.
Conditional bonuses are separate from base rates. No-battery expected benefit
includes storage and optimization, excludes investment, and uses the plan's
recorded bonus budget. It is not continuous historical savings.

The planning page shows all three scenario balances without a comparison selector.
Each graph identifies its original expected economic cost, net grid cash, wear and
inventory change. Self-consumption and OEP also show expected benefit versus the
same no-battery baseline. Negative benefit remains negative. These are horizon
expectations, not summed overlapping savings. All planning graphs share the
6-hour, 24-hour and full-horizon selection.

A configured app opens on Planning after a root/HA-app refresh. Unfinished first
setup still opens the wizard. Test environments use live market/Open-Meteo
sources and quarter-varying simulated energy meters with matching native
five-minute/hourly Recorder history. Do not resample real source data to invent
quarter variability or reset original plan archives.


Contract configuration implementation: the app-owned [Contract page](../docs/development/CONTRACT-PRICES.md#contract-page) edits dated import/export formulas and separate bonus versions, previews negative/zero/positive prices, reports measured remaining budgets and confirms missing baselines using HA meter provenance. Current database revisions drive source selection and background revaluation. Original plans and expectations remain immutable. No supplier-origin exclusions or tax/subscription engine were added.


### Automatic forecast model selection implementation

FORE-04–06 use retained local-day accuracy for household and device models.
Separate measured appliances before training the household remainder. Keep
probable use in planning even before a task exists. Concrete acknowledged work
replaces a bounded share of that expectation, preserving additional use.
Independent per-plan attribution and physical validation prevent duplicate load.
The three Planning balance charts show individual expected and scheduled device
contributions in the bars, with one identity-bound legend entry per device.
Expected use is striped and scheduled use is solid. Tooltips distinguish both,
and the device's legend switch toggles both contributions together.
Physical execution still requires readiness
and permission. See [model selection](../docs/development/MODEL-SELECTION.md) and
[shiftable appliances](../docs/development/SHIFTABLE-APPLIANCES.md).

### Quarter-resolution forecasting increment

FORE-01/11/16 and UI-20: preserve genuine quarter simulation measurements. Avoid
uniform hourly replication where physical or bounded temporal projection supplies
within-hour estimates. Preserve hourly energy, shared inverter limits, UTC/DST
continuity, original archives and model-version isolation. Mark the resolution
estimate honestly. Neither hourly weather nor hourly price forecasts becomes
measured quarter evidence. See the demand and solar forecasting guides.

### Confirmed phasing: forecast-only consumers before shiftable appliances

First implement Energy-owned individual-consumer discovery, user selection and per-device weather sensitivity, history-based local prediction, residual-household composition without double counting, UI/HA forecast visibility and consistent planning-benefit evaluation. Phase 1 issues no physical commands and requires no task feedback or blueprint. With insufficient device history, retain the existing total-household forecast rather than removing unknown demand. The [demand forecasting guide](../docs/development/DEMAND-FORECASTING.md#individual-consumer-forecasting) records the implementation and evidence limits.

Only afterward implement the shiftable-appliance task, cycle-profile and mandatory feedback/blueprint requirements above. Use a simulated heat-pump consumer to exercise phase 1 in the real-Supervisor Docker developer environment. The simulated dishwasher end-to-end execution specification remains phase-2 acceptance work.


### Phase-2 implementation boundary

The appliance increment implements explicit Energy-owned shift permission, completed-cycle profile learning, durable task/attempt state, joint solver scheduling, mandatory execution feedback, helper-free HA actions and the supplied execution blueprint. See [shiftable appliance contracts and acceptance](../docs/development/SHIFTABLE-APPLIANCES.md) for exact behavior, defaults and remaining validation boundaries. Source implementation is distinct from passed full-chain acceptance. No production appliance is controlled by development tests.

### Helper-free appliance dashboard intent

A permitted shiftable Energy device exposes native HA Finish date (date) and Finish time (time)
and Ready for planning (button) controls. The app persists the draft deadline.
Setting it never submits readiness or changes accepted work. Pressing Ready
submits one task through the authenticated integration. Controls are unavailable
while work is unresolved. The app independently rejects concurrent submissions
and deadline edits. Draft edits do not invalidate or trigger plans. See the
[colored execution flow](../docs/development/SHIFTABLE-APPLIANCES.md#from-dashboard-intent-to-physical-execution)
for dashboard/automation input, app optimization, claimed physical start and
mandatory feedback boundaries.

### Explicit withdrawal of appliance planning

Cancel planning is available for every open task (any task that is neither cancelled nor completed).
Withdrawal removes future OEP start authorization and remaining scheduled work.
It never infers physical stop, nonexecution or completion. Preserve the original
attempt and measurements. The integration publishes Cancelled plus a correlated
cancellation event. The user-owned automation decides what physical action, if
any, follows. Late feedback cannot reactivate the cancelled task. A fresh explicit
readiness submission creates new work. OEP never retries cancelled work itself.


UI presentation: Home Assistant installation timezone is authoritative and is not
editable in OEP. Calendar contract/bonus dates use local-midnight date pickers.
Execution deadlines retain time input. Annual bonus reset is calendar year or
original contract anniversary. Visible option labels start with a capital letter
and use spaces. Internal enum identifiers remain stable. Monetary displays use
exactly two decimals without rounding calculation/storage inputs. Every displayed
percentage uses exactly one locale-aware decimal, including SoC, targets, axes,
tooltips, forecast errors and explanatory examples. Percentage inputs show one
decimal while idle and reveal their exact draft value while editing. Rounding
must never change stored values, drafts, calculations or API precision. Unknown
percentages remain unavailable. OEP-owned labels, titles, captions and structured
status summaries use a colon and a space between fragments, with the next text
fragment capitalized. Full stops and middle dots never serve as separators.
Chart x-axes omit redundant Home Assistant timezone and hover instructions.
Keep time ticks, date/time tooltips and explicit UTC calendar captions.
Preserve sentence punctuation, abbreviations, decimals, formulas, URLs, entity
identities and user/provider names. See
[contract presentation](../docs/development/CONTRACT-PRICES.md#calendar-dates-reset-choices-and-presentation).


A contract has at most one active bonus rule at any instant. Select the latest
rule whose effective start has been reached, across all bonus identities. A newer
rule supersedes the previous rule exactly at its start. Future rules do not apply
early and rules never stack. Preserve historical definitions, frozen plan inputs
and counter provenance. Reject ambiguous equal effective dates across identities.

Export bonus price eligibility uses an optional immutable comparison of a bounded price expression and an exact Decimal EUR/kWh threshold. All six comparisons are supported before float conversion. No epsilon or executable rule engine is used. The UI hides disabled conditions. Rate, time window and chronological quota remain separate responsibilities.

Price rules expose only an effective start. A rule applies until its replacement starts. The last rule never expires independently. Internal half-open interval boundaries are derived from adjacent starts and validated for continuous coverage. No tariff is invented before the first configured start.

Contract changes require an explicit save after preview. Drafts must be visibly unsaved. Keep save actions visible while editing. Show complete dated price rules as expandable rows, with the saved rule active today open and older/future rules collapsed. The price chart must show the total export rate including applicable bonus, separately from base export. This indicative line ignores volume caps and preserves original plan evidence.

The contract editor hides the bidding-area input when the Home Assistant country identifies one area and displays that automatic value as information. Multi-area countries offer only their country-specific zones, with an explicit empty initial choice and guidance to check the grid operator or energy supplier. Unknown countries and saved country/area mismatches show a blocking explanation, never silently rewrite stored rules. Preview and save also reject a zone outside a recognized HA country. Source coverage remains a separate check. Coordinate-based boundary lookup is not implemented.

After a 600 ms typing pause, the editor validates the complete draft through the existing authenticated preview endpoint. Examples appear directly below the formulas and optional bonus inside their shared price rule. Saving remains explicit and is allowed only for the validated current revision. New edits immediately clear examples and disable save. Delayed responses cannot validate newer edits. Manual Preview rates runs the same check immediately. Validation failures do not schedule retry loops or create contract revisions.

Each unsaved bonus rule also previews its current reset-period budget: maximum, actual export, remaining kWh, reset date and measurement time. This sums all required Energy Dashboard export counters, excluding import and battery counters. The existing authenticated snapshot/history protocol fetches missing annual history for the latest draft after saved-rule evidence takes priority. Draft rules and derived baselines remain in a bounded in-memory preview cache, expire after five idle minutes and never activate planning or write contract/counter revisions. The browser refreshes a pending history preview every three seconds. Changed drafts invalidate old requests and incomplete history remains explicitly unknown. Stored confirmations remain authoritative. Current-period figures do not predict future exports. Future-rule displays identify the measured period. Actual saving still initiates the ordinary durable baseline flow.


The Contract page has one list of complete **Price rules**. Each effective date activates the import formula, export formula and optional bonus together. A replacement copies the preceding rule and supersedes it without resetting bonus usage. Disabling an existing bonus emits a zero-rate continuation with its existing internal counter identity. Re-enabling it retains that identity and annual period. The user never names a Bonus rule ID. Existing source identities remain internal evidence rather than being rewritten.

Each complete price rule stores two independent dates: **Contract starts**, the original start with that supplier, and **Price rule starts**, when these terms take effect. A replacement within the same contract inherits its contract start. A supplier switch can use a new contract start. Price rule starts must be on or after that row's Contract starts, enforced by both UI and domain validation. Contract-anniversary reset uses that row's contract start. Opaque bonus counter IDs are generated per supplier contract. Changed terms retain the ID and used volume, while a new supplier contract starts its own budget. Its first counter period excludes exports before the contract start, including when the annual reset is 1 January. Future contracts are not assigned fabricated measured volumes: their not-yet-started budget is separate from current measured evidence, and planning starts the new period at zero at the actual boundary. The editor pre-fills an older revision's missing contract start from its earliest rule for explicit user confirmation. It does not modify the stored revision during loading. Existing independently dated price/bonus definitions are displayed on their union of boundaries, preserving each effective value until an explicit save. Original archives/plans retain their original content and version identities. Internal tariff and bonus compilation stay separate.

The single-contract editor asks for neither a contract name nor technical contract/bonus IDs. OEP assigns a stable internal identity to new configuration and preserves identities already present in saved rules, counters and archives. Changing a price-rule effective date or tariff within the same supplier contract does not create a new counter identity. A different contract start does. Display headings use readable rule dates rather than internal IDs.

The Price rule and Export bonus information buttons provide keyboard-accessible, mobile-readable explanatory dialogs with separate Zonneplan and NextEnergy tabs. Each value is a separate monospace line, followed by its explanation on the next line. Bonus examples use explicit price conditions together with their time windows and volume caps. Do not present formula-only alternatives as equivalent to the complete bonus rule. Sources are recorded in the documentation. Examples do not edit or save configuration and are explicitly not maintained supplier presets.

## 20. EV charging: confirmed

- EV-01: Support several explicitly identified vehicles sharing charge points and several points in one installation.
- EV-02: Each vehicle has its own HA SoC source, usable capacity, maximum AC power, target SoC and departure.
- EV-03: Each point has its own Energy consumer, measured power, connected source, power limits and measuring, on/off or variable-power capability.
- EV-04: Reuse HA metadata. Never guess the car.
- EV-05: Connected identified cars are scheduled automatically, jointly with PV, storage and other devices.
- EV-06: Use a daily departure with an optional one-off date/time, always in the HA timezone.
- EV-07: Native virtual controls require no extra helpers.
- EV-08: Missing or ambiguous evidence withholds authorization.
- EV-09: Recalculate remaining need using actual SoC.
- EV-10: No V2G. Physical actions remain user-owned HA automations.
- EV-11: If service is limited, maximize achievable useful charge before the configured economic strategy and expose predicted SoC/shortfall. Keep hard physical limits.
- EV-12: Use documented power-dependent default loss assumptions, without a mandatory user loss field.
- EV-13: Preserve original plan inputs and independent validation.
- EV-14: Exercise simulated cars, native controls and physical-action blueprints in the real-Supervisor Docker developer environment.

See [EV charging](../docs/development/EV-CHARGING.md) for the implemented command grid, bounds, objective order and evidence contract.

## 21. Forecast quality: confirmed

- FQ-01: Provide a separate forecast-quality view for a selected Home Assistant calendar day. Freeze the latest pre-midnight household, individual-device and per-source PV forecast references for that day independently of solver success. Intraday plans never replace this baseline. Never rebuild old forecasts using newer models, measurements or knowledge.
- FQ-02: Pin component forecast references when the household forecast is built. Retain each original forecast's version, issue time, model, source bindings and measurement boundary. Bind measured evidence to the HA entity identity, registry device identity and source unit. Plans without retained source bindings explicitly report unavailable evidence.
- FQ-03: Evaluate the full local calendar day only after its end and Recorder delay, requiring every hourly forecast and measurement to be complete and comparable. Aggregate quarter predictions to the measured hourly boundary without inventing quarter actuals. Preserve 23/25-hour DST days. Missing, incomplete, future and incompatible evidence remains null rather than zero. Missing midnight capture is unavailable rather than backfilled from an intraday plan.
- FQ-04: Retain measured cumulative hourly endpoints in immutable daily blocks with versioned receipt evidence. Use the app-owned active SQLAlchemy database and existing demand namespace. Preserve old block and receipt versions when later observations differ. Include SQLite, PostgreSQL, MariaDB and MySQL persistence and cross-backend migration verification.
- FQ-05: Retrieve the recent three days of hourly evidence through the existing native HA HistoryBridge and single trusted integration connection. Allow ten minutes for Recorder completion and retry failed evidence retrieval after fifteen minutes. This bounded recent retrieval does not promise arbitrary historical backfill for older selected days.
- FQ-06: Show predicted and measured day totals, signed/absolute daily error and relative daily error. Retain hourly graphs, paired coverage, exclusions, MAE, signed bias, RMSE and WAPE to expose timing errors. All accuracy metrics require an eligible full day. Zero measured energy has no relative percentage. These metrics do not change model promotion or selection policy.
- FQ-07: Keep the selected original forecast's model separate from current active and candidate model readiness, coverage, evaluation evidence and selection reasons. Display existing automatic-selection evidence without introducing a new forecasting model or policy.
- FQ-08: Controlled-load timing changes and curtailed, hybrid, shared-inverter or clipped PV observations are not independent forecast-accuracy proof. Preserve their original predictions and available measurements for inspection, label comparability limits and withhold unsupported accuracy scores. Distinguish available DC production from measured AC or DC generation.
- FQ-09: Provide responsive interactive Chart.js graphs and readable interval tables with a day selector, frozen source labels and explicit gaps. Keep forecast errors, simulated financial decision windows and measured savings visibly distinct. Preserve old plan archives and their read-only evidence endpoint. A forecast comparison does not establish causal EMS savings.

### Physical reference availability

- The historical continuous self-consumption reference must advance from contiguous measured
  evidence independently of prices, contract coverage and bonus-budget availability.
- Missing financial evidence must leave physical chart bars and reference SoC
  available. Show an explicit financial issue and unknown costs/benefit, never an
  assumed zero bonus or an invented historical budget.
- Preserve independent stock across restarts, contract changes and backend transfer.
  Do not reset it to current measured SoC. Missing physical evidence still blocks
  chronological advancement and retains the last valid checkpoint.
- Historical unavailable SoC may be reconstructed across at most one hour only
  with equal valid bounding SoC readings and both charge/discharge cumulative
  meters proving idle. Label the reconstruction estimated. Missing or conflicting
  evidence remains missing. Unit-conversion roundoff below one micro-Wh must not
  permanently block capture. Report the first blocked source and boundary.

Historical bonus-budget recovery must be automatic for captured settlement
intervals. All configured physical export meters contribute to one used volume.
A later confirmed seed must support older intervals through contiguous measured,
reset-aware export evidence. It must never require resetting the independent
battery reference or changing that confirmation. Missing older periods must be
requested from native HA history using their own contractual reset dates.
Changed meter membership, incomplete coverage and inconsistent anchors remain
unknown. Historical recovery must preserve configuration, original plans,
measurements and current planning confirmations on every database backend.


### Central bonus opening and one-time setup

The Contract page presents one active supplier-contract bonus budget independently
of dated price rule editors. Show the budget-period start, opening used export
in kWh and its measurement date, origin, export since that opening, total used,
remaining quota and latest measurement date. Changed price terms retain the same
period usage. Annual reset and supplier contract changes acquire their own
period evidence automatically. Users do not repeat setup for every price rule.

Opening origin is explicitly automatic Home Assistant history, manual confirmation,
or unknown for existing records without stored evidence. Unknown origin must not
invalidate or relabel a preserved opening. Details identify all configured grid
export meters, the opening/current readings and the calculation. Automatic origins
retain the history period, boundary anchor, row count, statistics endpoints and
content digest. They do not claim the digest independently authenticates HA.

Only unavailable budgets offer manual confirmation. A user enters total exported
kWh since the budget boundary through the displayed HA measurement date. The date
is supplied by the matching HA readings. It is not a freely editable historical
instant without matching meter evidence. Confirmed values and original archives
remain intact. Changes to readings/configuration while confirming require refresh.

## 22. Generic thermal storage: confirmed development increment

This later user-approved increment extends the original PV/battery scope. It does
not retrospectively change the historical first-release agreement or establish
release acceptance. Model a thermal store through its physical capabilities,
measurements and constraints. The first actuator is an on/off resistive electric
water boiler. Weather-sensitive heat-pump and air-conditioner consumption
forecasts remain forecast-only unless a later explicit thermal-control increment
defines their additional physics and control obligations.

- THERMAL-01: Support up to four independently identified thermal stores in one
  installation, initially electric water boilers with fixed rated on/off heater
  power. Configure effective water volume, ambient temperature, heat-loss
  coefficient and comfort/maximum temperatures. Derive effective heat capacity as
  `volume_litres * 4.186 / 3600` kWh per degree Celsius. This is a single mixed
  water-temperature model, without stratification, heat-pump COP, space heating
  or modulating heater control. Resistive heating uses unit thermal efficiency.
- THERMAL-02: Require an explicit nonnegative 24-hour local-clock heat-draw
  profile before enabling heating. Interpret each value as thermal kWh per hour,
  using the Home Assistant installation timezone and actual interval duration.
  Repeated DST hours use the same configured clock-hour rate. Skipped hours
  contribute no duration. Identify this profile as an assumption, not measured
  hot-water demand or an ML prediction. Electrical meter history alone must not
  be presented as a measured heat-draw profile. Apply exponential standing losses
  and constant-rate heating/draw integration over each active interval.
- THERMAL-03: Reuse the Home Assistant Energy consumer meter and require fresh
  temperature and electrical-power reports from explicitly selected HA entities.
  A `water_heater` may supply its current measured temperature. Never substitute
  its target temperature for an observation. Preserve the oldest contributing HA
  report time as provenance. A valid unchanged HA state remains usable in a new
  integration capture. Missing, stale, invalid, out-of-order or unavailable captures
  do not become zero. Integration captures expire after five minutes. Restart requires new telemetry.
- THERMAL-04: Keep electrical ownership disjoint from EV charging and shiftable
  appliance cycles, including when ownership changes after configuration. Remove
  the boiler's ordinary electrical forecast exactly once before adding planned
  heater consumption. Only change this household boundary when both the excluded
  component prediction and baseline, and all retained residual components, are
  fresh and cover the entire planning horizon. Otherwise retain the ordinary
  household baseline and withhold thermal work.
  An unrelated consumer with insufficient history must not suppress a ready
  boiler forecast. Retain that consumer's measured contribution in the remaining
  household baseline. Do not activate an incomplete composite model.
- THERMAL-05: Treat the minimum temperature as a comfort target, with explicit
  unmet thermal-energy deficits when it is unreachable. Keep temperature between
  zero and its configured maximum as hard bounds under every price or strategy.
  Represent unserved heat draw explicitly so an impossible draw cannot make the
  shared electrical plan infeasible. Permit unserved draw only when the modeled
  store is exhausted. Do not defer physical withdrawals to preserve later comfort.
  Passive loss and draw continue during heater-start lead time. Preserve ongoing
  measured heating across ordinary rolling rounds. First minimize total unserved draw, then
  prove the minimum duration-weighted comfort deficit. Then minimize ending
  heat-stock deficit against `max(initial_temperature, minimum_temperature)`.
  Freeze both optima before optimizing the remaining appliance/EV service and
  selected energy strategy. A rolling horizon alone is not permission to consume
  tomorrow's stored heat. Report any remaining final-stock deficit separately.
- THERMAL-06: Schedule heaters jointly with PV, stationary batteries, EV charging
  and appliances on the existing shared electrical bus and hard grid/converter
  limits. Use one physical on/off command per UTC quarter, shared across refined
  price, bonus and EV-stop segments. Do not backdate heating, draw or cooling into
  the elapsed interval before the observation/dispatch boundary. Include heater
  electrical energy once in physical balances, tariffs and comparison evidence.
- THERMAL-07: Independently validate every proposed thermal trajectory before
  publication. Recompute heat balance, fixed-power commands, electrical energy,
  readiness, quarter grouping, device identities, temperature limits and physical
  comfort deficits without trusting solver slack or native model expressions.
  Rejected, interrupted or unproved priority results cannot authorize a new plan.
- THERMAL-08: Keep all thermal calculations in the app. Expose helper-free virtual
  HA controls for enabling planning and setting the comfort minimum, plus separate
  requested heating, measured heating, planned power, predicted temperature and
  comfort-shortfall entities. Physical switching remains in user-owned HA
  automations, supported by a supplied blueprint. Off, unknown, unavailable,
  expired or mismatched permission must select the stop action. Do not queue late
  starts. Native thermostat, overheating protection and hygiene programmes remain
  authoritative. OEP is neither their replacement nor a safety controller.
- THERMAL-09: Configure electric boilers under Settings > Appliances with explicit
  save/cancel controls and source selection. Keep technical configuration out of
  Energy > Thermal storage. That operational page provides comfort and planning
  pause/resume intents, visible current evidence and interactive planned heater-power
  bars and predicted-temperature lines for the whole plan, including shortfall
  and final-stock status. Keep measured temperature/power separate from predictions
  and requested heating. A plan or request does not prove physical execution.
- THERMAL-10: Persist immutable thermal settings and original plan evidence in the
  existing active SQLAlchemy database. Preserve prior configuration, identities
  and history during deployment and backend transfer. Test SQLite, PostgreSQL,
  MariaDB and MySQL storage, independent thermal arithmetic, joint-device limits,
  malformed evidence and fail-closed command withdrawal. Verify relevant desktop,
  mobile and HA blueprint flows in the existing persistent Docker developer environment
  with simulated boilers. Simulation is not evidence of physical boiler safety.

See [thermal storage implementation](../docs/development/THERMAL-STORAGE.md) for
the current bounds, equations, virtual entities and acceptance evidence.

## Activity, availability and notification events

- ACT-01: Modules publish actual activities and their outcomes to one central
  activity collector. Retain seven days of history across restarts. Default to
  today in the HA timezone and allow filtering by activity type.
- ACT-02: Modules publish known work for the next six hours, including changes
  and cancellations. Do not predict ad-hoc replans. Record them when executed.
- ACT-03: Keep one ID per attempt. Distinguish completed usable results, failures,
  interruptions without definitive outcomes, deliberate cancellation and skipped
  work. Show the first usable success after failure as recovered. Empty or invalid
  results cannot establish recovery. Record actual requests, not idle polls.
- ACT-04: Price activities identify the actual adapter and market. Failure details
  explain whether retained prices, weather, forecasts or models remain usable and
  the actual impact. Domain owners retain the existing validity policy.
- ACT-05: Modules publish current availability independently of activity results.
  Business logic owns notifications. The UI subscribes to them. Neither the
  collector nor the UI manufactures a notification from an activity failure.
- ACT-06: New and reconnecting subscribers receive an authoritative snapshot and
  subsequent changes without a subscription gap. Replay missed events when
  retained, otherwise reset to a fresh snapshot. Other modules can subscribe.
- ACT-07: Restart retains history and marks unfinished executions interrupted.
  Current health begins unknown and producers rebuild actual state and schedules.
  The collector never independently retries work. Preserve the separate database
  maintenance recovery mechanism while the app is unavailable.
- ACT-08: Activity titles include a safe human subject or captured work period
  where useful across all producers. Keep titles on one line with the complete
  name available through disclosure. Context never changes an execution's ID or
  outcome. Financial failure explanations distinguish archived plan valuation
  from active device commands and provide the actual next action.
- ACT-09: Where a relevant data screen exists, provide a clearly named read-only
  link from activity details. A plan link opens that exact retained plan without
  activating it or falling back to another version. Measured-savings details
  identify per-attempt progress, the stopping interval, known waiting cause and
  actual continuation or user action. Preserve separate execution evidence.

See [activity and module events](../docs/development/ACTIVITY-EVENTS.md) for the
implemented producer inventory, storage limits, API and UI behavior.
