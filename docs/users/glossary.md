# Energy and planning terms

Use this reference when a screen or guide uses an unfamiliar energy term. Internal API values and formal calculation contracts remain in the developer references.

| Term | Meaning in OEP |
|---|---|
| kW / W | Power, the rate at which energy flows. 1 kW equals 1,000 W. |
| kWh / Wh | Energy accumulated over time. A steady 2 kW for 30 minutes is 1 kWh. |
| kWp | A solar array's rated peak power under specified reference conditions, not measured present production. |
| SoC | Battery state of charge as a percentage of its configured usable capacity. |
| AC / DC | Alternating/direct current boundaries. Inverter and cell losses mean equal energy cannot be assumed across all boundaries. |
| Import / export | Electricity crossing the grid boundary into/out of the installation. |
| Household demand | Consumption reconstructed from the configured disjoint energy balance, with separately modeled loads handled without double counting. |
| Planning horizon | The future period covered by a particular plan. It can be shorter than the target when input coverage is limited. |
| Original plan | The accepted record of inputs, assumptions, proposed actions and expected costs for one calculation. Later measurements do not rewrite it. |
| Baseline without battery | The future comparison retaining the household and solar forecasts without battery storage. |
| Self-consumption | Battery operation that stores solar surplus and attempts to cover household demand within battery and converter limits, without price optimization. It is a modeled reference. |
| Economic cost | Net electricity cost plus wear minus the change in stored-energy value. |
| Battery value change | Change in estimated stored-energy value, not cash received. |
| Published price / forecast price | A retained market publication versus a prediction of a future market price. |
| Curtailment / clipping | Deliberately reducing production versus production being physically limited by conversion capacity. They are not interchangeable permissions. |
| Virtual command | Advice exposed by the HA integration for a user-owned automation or controller. Publication is not physical execution. |
| Acknowledgement | The integration received/accepted a plan. It does not prove equipment followed it. |
| Zero / unknown / unavailable | A real zero value, an inapplicable or unknown quantity, or a quantity that cannot currently be used. Do not substitute one for another. |
| Forecast quality | Comparison of a fixed prediction with later comparable measurements. |
| Shadow candidate | A forecast model being evaluated without automatically replacing the active model. |
| Measured / estimated / simulated | Retained observations, reconstructed or assumed quantities, and modeled alternative behavior. Read the label before using the result. |

**Continue:** [How OEP works](how-oep-works.md) or [read your plan](read-your-plan.md).
