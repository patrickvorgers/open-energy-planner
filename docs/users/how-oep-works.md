# How OEP works

OEP proposes when your installation should use, store, import or export electricity. It combines what is measured now with forecasts, your contract and the limits you confirmed. A plan describes the model's expected outcome. It is not a record of physical execution.

## Follow one planning round

1. **Measure the starting point.** Home Assistant supplies the configured Energy sources, current battery state and other required measurements.
2. **Look ahead.** Household demand, solar production and electricity prices describe the expected future. Published prices and predicted prices remain distinguishable.
3. **Apply your settings.** OEP uses the installation topology, equipment limits, contract dates, conversion losses and enabled planning permissions.
4. **Calculate and check.** The optimizer proposes a complete schedule. Independent arithmetic checks its energy balances, limits and costs before the app accepts it.
5. **Publish advice.** The integration exposes applicable virtual commands. Your Home Assistant automations and local controllers decide how to operate equipment.
6. **Compare with measurements.** Later observations inform new plans, forecast-quality comparisons and evaluated savings. They do not rewrite the original prediction.

The app performs the calculations. The Home Assistant integration exchanges data and exposes virtual devices. OEP does not need direct access to the Home Assistant database, and the integration does not run the native optimizer.

## Understand the installation boundary

The grid connection joins household demand, solar inverters, batteries and supported appliances. OEP models each battery separately. A shared hybrid inverter differs from separate solar and battery inverters because direct DC charging can avoid an AC conversion round trip.

| Input | Why OEP needs it | Where you check it |
|---|---|---|
| Energy sources and current readings | Establish what exists and the current physical state | Home Assistant Energy, then **Settings > Installation** |
| Equipment ratings and connections | Keep the proposed flows within modeled limits | **Settings > Installation** |
| Household and solar forecasts | Estimate demand and production before making decisions | Installation and appliance settings, then **Insights > Forecast quality** |
| Market prices and contract formulas | Translate electricity flows into expected cost | **Settings > Installation > Price sources** and **Settings > Contract** |
| Appliance permissions and requests | State what may be planned and what needs to be ready | **Settings > Appliances**, then the relevant **Energy** page |

Home Assistant Energy owns meter membership. OEP owns its planning settings. Selecting a source for forecasting does not give OEP permission to control it.

## Choose the optimization goal

The default **Minimize cost** goal minimizes expected economic cost, including effective electricity rates, configured battery wear and the change in stored-energy value.

**Minimize consumption** prioritizes grid import, including a correction for depleted starting battery stock, and then chooses the cheapest result among equally good import results. Export does not cancel import in this objective. The stock correction prevents an empty battery at the end from looking like free energy savings. It is accounting, not a command to refill the battery.

Mandatory reserve recovery and supported appliance service priorities can take precedence over either goal. A lower bill is therefore not the only condition a valid plan must satisfy. See [installation settings](installation.md#choose-the-planning-strategy) for changing the goal and [the technical strategy contract](../development/CONSUMPTION-STRATEGY.md) for the exact priorities.

## Distinguish the three planning scenarios

| Scenario on Planning | Question it answers |
|---|---|
| **Optimizer** | What does the chosen OEP strategy propose under the original inputs? |
| **Battery self-consumption** | What would the same batteries do by storing solar surplus and covering demand without price optimization? |
| **Without battery: Baseline** | What would the same household and solar forecasts cost without battery storage? |

Both battery scenarios compare their expected benefit with the same without-battery baseline. The baseline retains the included EV and thermal loads. It is not a hypothetical household with every appliance switched off.

## Keep time and uncertainty in view

The target planning horizon is 48 hours when the required inputs cover it. Missing coverage can shorten it. Stationary control uses quarter-hour decisions for the first day and hourly groups for the second. The charts present their interval balances as quarter-hour bars. A finer price settlement boundary does not imply an extra physical switching decision.

Home Assistant supplies the installation timezone. Calendar days can contain 23 or 25 hours around daylight-saving changes. Original timestamps, input versions and forecasts stay with each saved plan.

OEP replans as time moves and meaningful inputs change. A new calculation can fail while an older plan remains visible in history. That visibility does not extend its command lifetime. Missing measurements remain unknown rather than becoming zero. Notifications identify the affected source and the next recovery action.

**Next:** [Prepare your installation](before-you-start.md), or [read a complete plan](read-your-plan.md).
