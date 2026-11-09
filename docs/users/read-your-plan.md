# Read your plan

Open **Energy > Planning** to inspect one original calculation. Begin with its version, generated time and strategy. Then follow the same energy through the three scenarios. The page explains proposed behavior, not proof that physical equipment followed it.

## Check which plan you are looking at

The latest retained plan is the default. **Plan version** lets you inspect history. A historical selection changes the view, not active commands. While the same plan refreshes, the previous complete view can remain visible with an explicit loading status. Choosing a different historical plan clears its predecessor's data until that selection loads.

A valid older plan can remain in history after a later calculation fails. Its presence does not mean its commands are still applicable. Read the page's status and any source warning.

For duration and solver-progress questions, use [Planning performance](planning-performance.md). A first solver candidate is not yet an independently checked published plan.

## Start with the three scenarios

| Chart | Read it as |
|---|---|
| **Optimizer** | The original plan under its selected goal, forecast, installation and contract |
| **Battery self-consumption** | The same installation storing surplus solar and covering demand without price optimization |
| **Without battery: Baseline** | The same household and solar forecasts without battery storage |

The scenarios share a time range, units and kW scale. The value-axis bounds combine visible data across all three charts, so the same bar height represents the same power. Zooming, toggling a series or resizing updates their shared scale. The baseline retains planned EV and thermal loads. Self-consumption aims for zero on the meter within physical limits, rather than guaranteeing it.

[![A populated synthetic Planning view showing energy supply and use, battery state and the boundary between days.](../assets/planning-example.png)](../assets/planning-example.png)

*Read the colored flows together with battery SoC. The example contains actual calculated flows from synthetic inputs. It is a teaching scenario, not a prediction for your household.*

## Read the energy balance

Above zero is supply to the modeled balance: used solar, grid import and battery discharge. Below zero is use: household demand, grid export, battery charge and conversion losses. These signs describe this chart's balance. They are not interchangeable with every device's command sign.

The series together should balance around zero. Bars show mean kW for their covered quarter. Battery lines use the separate SoC percentage axis. Tap or hover for the exact interval. A partial quarter has its actual covered duration rather than a fabricated full-quarter energy amount.

Use **Next 6 hours**, **Next 24 hours** or **Full period** (with its available duration). Day bands and date labels identify calendar boundaries in the installation timezone. On a phone, the legend sits below the chart. Toggle a series to inspect the others. Hidden or missing data must not be interpreted as a measured zero.

[Open the mobile chart example](../assets/planning-mobile.png) to see the stacked cost cards and legend below the same synthetic two-day plan. Open any guide figure at full size when its labels are too small on your screen.

## Read cost before benefit

Every scenario starts with **Expected economic cost**. Both battery scenarios then show **Expected benefit versus without battery** against the same baseline. A negative benefit means the scenario costs more than the baseline. An unavailable comparison stays unavailable.

Open **Cost breakdown** to see **Import cost**, **Export income** and **Bonus income** separately for each scenario. Net electricity cost is import cost minus export income minus bonus income. A negative income increases net costs. Missing amounts remain unavailable rather than becoming zero. The same breakdown shows battery wear and stored-energy value change. Economic cost includes wear and subtracts the value change. Stored-energy value is not cash received.

These amounts and their cost breakdown follow the selected chart period. The latest plan starts at the current quarter. Historical plans start at their original beginning. **Full period** shows the available duration, normally 48 hours for a new plan and less when part has elapsed or coverage is shorter. The caption gives the exact covered period. Empty or unavailable periods do not show a zero cost.

Battery value change uses the stored energy at both ends of that period and the original plan's fixed valuation basis. It retains reserves, conversion losses, future wear and the useful-energy cap. Changing the view does not fetch new prices or optimize another plan. Earlier export still consumes bonus room even when it is outside the selected period. These are alternative scenario expectations, not values to add together. The [Savings guide](savings.md) gives a worked comparison and explains the separate retrospective reference.

## Understand the forecast and price charts

Household demand and available solar describe original forecast inputs before the planner's decisions. Available DC solar and household AC demand use different measurement boundaries. Compare them with conversion and clipping in mind.

The **Price inputs** chart follows the selected latest or historical plan and shows its frozen prices. Newly received prices and changed contract rules appear when a new plan uses them. Without a plan, the same graph is explicitly labelled **Current tariff preview** and uses cached market prices with saved rules. Solid lines identify published prices and dotted lines identify forecast prices. A forecast price is not a later published settlement price. Conditional export bonus revenue can depend on eligibility and remaining allowance, so a preview rate is not a promise that every exported kWh earns it.

The price scale always includes zero. With nonnegative visible prices, its lower
bound is exactly zero. Negative prices receive clearance below the lowest
visible price equal to ten percent of the total axis height. If all visible
prices are negative, zero is the upper bound. Changing the time range or hiding
a series recalculates the scale without changing any prices.

See [contract and prices](contract.md) for formulas, dates and bonus interpretation, and [forecasts](forecasts.md) for how predictions are checked against measurements.

## Inspect an individual decision

Open **Understand this plan** for whole-plan inputs, assumptions and source evidence. Expand **Selected interval details** and move the time selector to inspect the relevant interval.

Check the measured starting stock, forecast inputs, physical limits, proposed command and expected result together. A reached limit is an observation. It is not proof that OEP evaluated every hypothetical alternative or that this limit caused the choice.

Original prices and expected outcomes remain separate from later contract repricing. The detailed energy and command tables preserve the selected original plan. Viewing them never sends a physical command.

## Keep units and commands straight

Power in kW is a rate. Energy in kWh is the amount across an interval. A steady 2 kW for one quarter is 0.50 kWh. OEP retains calculation precision even when a screen rounds the displayed value.

For a fixed stationary-battery target, positive W means discharge and negative W means charge at the battery DC terminal. Zero is a real idle target. A numeric value can be unknown because its mode has no fixed target, or unavailable because no usable command exists. Do not turn either state into zero in an automation.

**Next:** [Understand Savings](savings.md), [inspect forecast quality](forecasts.md#read-forecast-quality) or [resolve a warning](troubleshooting.md).
