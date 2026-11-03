# Open Energy Planner: proposed device settings

Date: 18 October 2026. English edition 19 October. Research proposal, not an implementation instruction. Confirmed requirements prevail. New fields/defaults below remain proposals.

## Principles

Use HA Energy source links first, prefilling other properties only if reliably available and semantically correct. Energy configuration does not guarantee battery capacity, rating, efficiency or topology. Request missing essential limits. A historical maximum is not a proven hardware limit. The wizard asks what planning needs, not how to control hardware. Prefill editable device names. No extra helpers/scripts/physical command entities/automation checklist. Under CONFIG-11 this is not a mandatory onboarding checklist: start with necessary information for immediate devices. Add the rest later in the same configuration UI.

## DAO and HAEO research

- DAO includes capacity, SoC/limits, charge/discharge curves, DC limits/efficiencies and device-specific output entities/extra control rules. Do not copy output bindings. Its investigated `cycle_cost` charges per kWh for both directions, unlike our equivalent-full-cycle fee. [Battery schema](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/prog/config/models/devices/battery.py).
- DAO solar includes capacity, tilt/orientation/strings, production links and ML settings. Retain our simple orientation/automatic model choice. [Solar schema](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/prog/config/models/devices/solar.py).
- HAEO separates capacity/SoC/directional ratings/efficiencies/terminal value and economic SoC preferences. Retain hard limits. Do not add penalties/partitions. Although its power fields can be optional, our controllable devices need finite applicable bounds. [Battery](https://haeo.io/latest/user-guide/elements/battery/).
- HAEO's separate DC-connected inverter with directional conversion ratings/efficiencies supports our route/device distinction. [Inverter](https://haeo.io/latest/user-guide/elements/inverter/).
- HAEO solar uses supplied forecasts and curtailment, defaulting to permitted curtailment in the inspected documentation. We infer neither capability nor permission without configuration. [Solar](https://haeo.io/latest/user-guide/elements/solar/).
- HAEO grid has directional prices/limits. Our market source is internal and contracts centralized, not selected independently per device. [Grid](https://haeo.io/latest/user-guide/elements/grid/).

## 1. Battery

| Property | Simple setup |
|---|---|
| SoC | Energy suggestion. Choose/correct only if absent/incorrect |
| Actual power | HA source, normalized sign/unit |
| Charged/discharged energy | Energy links for history/evaluation, no user helpers |
| Capacity kWh | Prefill if known, otherwise ask. Match source's 0–100% scale, without subtracting reserve twice |
| Maximum charge W | Actual/reliable HA rating or ask |
| Maximum discharge W | Separate directional rating, plus shared inverter bounds |
| Minimum SoC | Hard limit, prefilled for confirmation if available. No universal manufacturer default claim |
| Maximum SoC | Proposed editable 100% on selected source scale |
| Installation | Own inverter or shared hybrid. Derive connections |
| Grid following | Existing capability choice, without selecting physical controller |
| Losses | Simple installation-type assumptions, editable if known. Numeric defaults remain open |
| Cost/equivalent full cycle | Agreed default zero, optional fee |
| Reserve recovery | Existing choice below minimum |

Unknown hard power limits do not imply infinity. Ask for a rating or prove an applicable shared bound limits the device adequately. Do not ambiguously mix AC and battery DC power under a generic “Battery power” field.

## 2. PV

| Property | Simple setup |
|---|---|
| Production energy/power | Energy links, normalized units and explicit relevant measurement side |
| Installation relationship | Own AC inverter or hybrid DC, within the same installation choice |
| Control capability | Proposed fixed-production default. Choose on/off or limit only if supported |
| Control permission | Only show for configured capabilities. Different from hardware support |
| Total panel rating/approximate orientation | Only required without usable history under the agreed startup rule |

No mandatory forecast sensor, weather/API choice, exact tilt/string/ML settings. Open-Meteo/local yield model provide forecasts. Do not silently use an underestimated observed maximum as a production limit.

## 3. Inverter

| Property | Simple setup |
|---|---|
| Own/shared hybrid | Derived from installation choice |
| Connected PV/batteries | Select configured virtual devices. No technical network drawing |
| Maximum DC→AC | Reliable prefill or ask |
| Maximum AC→DC | Only where route exists. Suggest equal rating only if justified |
| Efficiencies | Correctable route assumptions. Numeric defaults still open |

Hybrid PV/battery flows share the applicable AC bound. PV→battery DC does not automatically use it. Derive internal topology and ask extra DC ratings only if actually limiting. Apply each traversed conversion once. No duplicate total battery/inverter loss.

## 4. Grid connection

| Property | Simple setup |
|---|---|
| Import/export energy and power | Energy sources |
| Maximum import | Reliable total connection bound, otherwise ask one value |
| Maximum export | Separate, even when equal to import |
| Bidding area | Infer unambiguously from HA location, ask only if uncertain |
| Contract | One link to central formulas/bonuses, not copied per device |
| Zero-grid target | Active/inactive, only 0 W, no arbitrary setpoint |

No phase layout, physical protection settings or supplier control signals in this wizard.

## 5. Household demand and DAO baseload

Use one total profile. Derive measurements/history using configured routes. Do not count battery losses as household demand. Only ask one annual total-demand estimate when usable history is insufficient. No permission to turn off the entire house. Flexible devices follow later.

Investigated DAO `Report.calc_weekday_baseload()` assembles residual demand from energy history, subtracting separately modelled consumers, then averages hourly values for the weekday. This path is a historical average, not demand-model ML training. Investigated solar ML predicts PV. [DAO source](https://github.com/corneel27/day-ahead/blob/810392a2cb4ebe76c684102cde24dd2b077e27c6/dao/prog/da_report.py).

We need demand forecasts, not a second mandatory baseload model. Start with a simple annual/historical profile, automatically assess whether learning improves plans, and exclude later separately planned loads from residual demand. Users choose neither algorithm nor a 24-hour baseload table.

## Excluded complexity

No required per-power-step efficiency tables, economic SoC partitions, per-percent/hour penalties, manual terminal SoC, per-device solver settings or technical node/edge wizard. Terminal value is automatic app logic, not a mandatory user field. Later refinements need demonstrated benefit and do not gate the first useful version.

Research used the pinned DAO commit above and HAEO `latest` on 18 October 2026. HAEO documentation may change. No DAO/HAEO installation was modified and neither planner was executed locally for this study.
