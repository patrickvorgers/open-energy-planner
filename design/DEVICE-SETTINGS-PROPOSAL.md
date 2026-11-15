# Open Energy Planner: proposed device settings

Date: 18 October 2026. English edition 19 October. Research proposal, not an implementation instruction. Confirmed requirements prevail. New fields/defaults below remain proposals.

## Principles

Use HA Energy source links first, prefilling other properties only if reliably available and semantically correct. Energy configuration does not guarantee battery capacity, rating, efficiency or topology. Request missing essential limits. A historical maximum is not a proven hardware limit. The wizard asks what planning needs, not how to control hardware. Prefill editable device names. No extra helpers/scripts/physical command entities/automation checklist. Under CONFIG-11 this is not a mandatory onboarding checklist: start with necessary information for immediate devices. Add the rest later in the same configuration UI.

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
| Cost/internal kWh discharged | Default zero, optional wear rate |
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

## 5. Household demand

Use one total profile. Derive measurements/history using configured routes. Do not count battery losses as household demand. Only ask one annual total-demand estimate when usable history is insufficient. No permission to turn off the entire house. Flexible devices follow later.

We need demand forecasts, not a second mandatory baseload model. Start with a simple annual/historical profile, automatically assess whether learning improves plans, and exclude later separately planned loads from residual demand. Users choose neither algorithm nor a 24-hour baseload table.

## Excluded complexity

No required per-power-step efficiency tables, economic SoC partitions, per-percent/hour penalties, manual terminal SoC, per-device solver settings or technical node/edge wizard. Terminal value is automatic app logic, not a mandatory user field. Later refinements need demonstrated benefit and do not gate the first useful version.
