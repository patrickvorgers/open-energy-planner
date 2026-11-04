# Read your plan

Start with the original plan's timestamp and input context. A chart describes the plan that was calculated, not a guarantee of future measurements.

## Know which result you see

**Planning** shows forecasts and proposed flows for a particular horizon. **Savings** evaluates retained evidence and distinguishes expected comparisons from measured history. **Forecast quality** compares original predictions with later measurements. These views answer different questions.

An original plan retains the prices, input versions, limits and expected costs used when it was calculated. Later tariff changes do not rewrite that original calculation. A failed new calculation does not overwrite the previous accepted plan or extend its commands indefinitely.

## Read the balance

The balance uses quarter-hour bars. Above zero means supply to the modeled balance, including grid import and battery discharge. Below zero means consumption, including household demand, grid export and battery charging. A separate battery-state series shows state of charge.

Use **Next 6 hours**, **Next 24 hours** or **Full horizon**. Tap or hover to inspect an interval. The three balance comparisons use the same kW scale. Switching a series or the visible horizon changes the view, not the saved plan.

| Comparison | Meaning |
|---|---|
| Without battery | The same original household and solar forecasts without battery operation |
| Battery self-consumption | A separate reference that stores surplus solar and covers deficits within its limits |
| OEP | The proposed optimized operation under the selected strategy and contract |

For detailed comparison boundaries, use [Savings](../development/SAVINGS.md) and [plan evidence](../development/PLAN-EVIDENCE.md).

## Keep prices and commands clear

Published prices and forecast prices are different inputs. Solid and dotted price lines retain that distinction. Current tariff previews may differ from the prices saved with an older plan. The [contract guide](../development/CONTRACT-PRICES.md) explains formulas, units and effective periods.

For virtual battery commands, positive W means discharge and negative W means charge. Zero W is a real idle request. Unknown means there is no applicable numeric target. Unavailable means the command is not currently usable, for example after pause or expiry. Do not turn either state into zero. The [virtual-device guide](../development/VIRTUAL-DEVICES.md) owns the complete contract.

## Respond to a warning

Read the stated action and affected input before changing configuration. Check the measurement source and time, not just whether an entity exists. Missing history, an absent forecast and a failed connection require different remedies. See [troubleshooting](troubleshooting.md) and the [notification guide](../development/PLANNING-NOTIFICATIONS.md).
